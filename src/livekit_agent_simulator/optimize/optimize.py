"""Optimizer orchestration + artifact writing.

Runs the baseline, proposes candidates (deterministic + LLM), evaluates each
over the dataset, selects a winner that strictly beats baseline AND passes the
held-out gate, and writes ``.agent-sim/optimized/<name>/`` artifacts the user
reviews + commits. Never a runtime dependency.
"""

from __future__ import annotations

import difflib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._backend import OptimizeProposer, proposer_for
from .apply import apply_variant_to_persona, policy_for_variant
from .eval import evaluate_variant, select_winner
from .gen import propose_candidates
from .variant import PromptVariant, write_variant


def _compose_instruction(
    project_root: str,
    scenario_id: str,
    variant: PromptVariant | None,
) -> str:
    """Compose the persona SI a scenario would use under a variant (pure)."""
    from ..config import load_config
    from ..scenario import parse_scenario

    cfg = load_config(project_root)
    scenario = parse_scenario(cfg.scenarios_dir / f"{scenario_id}.yaml")
    policy = policy_for_variant(variant)
    persona = apply_variant_to_persona(scenario.persona, variant)
    from ..caller import build_persona_system_instruction

    return build_persona_system_instruction(
        persona=persona,
        locale=scenario.effective_locale(),
        context=scenario.context if isinstance(scenario.context, dict) else {},
        script_steps=scenario.script_steps,
        first_speaker=scenario.run_spec.first_speaker,
        policy=policy,
    )


def _stage_variant(project_root: str, v: PromptVariant, *, name: str) -> str:
    """Write a candidate variant into a temp .agent-sim/optimized/<name>/prompt.yaml.

    Returns the artifact name so the runner can apply it via ``optimized=<name>``
    (the same shipped runtime seam a user later uses for the winner).
    """
    from ..config import load_config

    cfg = load_config(project_root)
    out_dir = cfg.optimized_dir / name
    out_dir.mkdir(parents=True, exist_ok=True)
    write_variant(v, out_dir / "prompt.yaml")
    return name


def _remove_stage(project_root: str, name: str) -> None:
    """Remove a candidate staging dir after evaluation (not part of the artifact)."""
    import shutil

    from ..config import load_config

    cfg = load_config(project_root)
    shutil.rmtree(cfg.optimized_dir / name, ignore_errors=True)


async def optimize_persona(
    project_root: str,
    scenario_ids: list[str],
    *,
    held_out: str | None = None,
    candidates: int = 4,
    max_candidates: int = 6,
    strict_judge: bool = False,
    repeat: int = 1,
    pass_at_k: int | None = None,
    agent_name: str | None = None,
    name: str | None = None,
    profile: str | None = None,
    environment: str | None = None,
    execute_scenario: Any = None,
    proposer: OptimizeProposer | None = None,
) -> dict[str, Any]:
    """Run the optimizer over a dataset; returns result + writes artifacts.

    ``execute_scenario`` / ``proposer`` are injectable for tests; production
    defaults to ``ops.execute_scenario`` and the configured judge backend.
    ``profile`` selects a named caller profile for every run in the loop.
    ``environment`` selects a named LiveKit environment for every run in the loop.
    """
    from .. import ops

    run_scenario = execute_scenario or ops.execute_scenario
    proposer = proposer or proposer_for(project_root)

    heldout_ids = [held_out] if held_out and held_out not in scenario_ids else []
    train_ids = [s for s in scenario_ids if s != held_out]

    baseline = await evaluate_variant(
        project_root, None, train_ids,
        execute_scenario=run_scenario, strict_judge=strict_judge,
        repeat=repeat, pass_at_k=pass_at_k, agent_name=agent_name,
        profile=profile, environment=environment,
    )

    current_si = _compose_instruction(project_root, train_ids[0], None) if train_ids else ""
    variants = await propose_candidates(
        proposer,
        current_instruction=current_si,
        max_candidates=max_candidates,
    )

    evaluated: list[dict[str, Any]] = []
    for v in variants[:candidates]:
        stage = _stage_variant(project_root, v, name=f"__candidate__{v.id}")
        try:
            ev = await evaluate_variant(
                project_root, v, train_ids,
                execute_scenario=run_scenario, strict_judge=strict_judge,
                repeat=repeat, pass_at_k=pass_at_k, agent_name=agent_name,
                optimize=stage, profile=profile, environment=environment,
            )
        finally:
            _remove_stage(project_root, f"__candidate__{v.id}")
        ev["variant"] = v
        ev["diff"] = _diff_instruction(project_root, train_ids, v)
        evaluated.append(ev)

    heldout_metric: dict[str, Any] | None = None
    winner: dict[str, Any] | None = None
    if heldout_ids:
        heldout_metric = await evaluate_variant(
            project_root, None, heldout_ids,
            execute_scenario=run_scenario, strict_judge=strict_judge,
            repeat=repeat, pass_at_k=pass_at_k, agent_name=agent_name,
            profile=profile, environment=environment,
        )
    if evaluated:
        winner = select_winner(
            baseline,
            evaluated,
            heldout=heldout_metric,
            heldout_threshold=1.0,
        )

    result = _write_artifacts(
        project_root,
        name=name,
        baseline=baseline,
        evaluated=evaluated,
        winner=winner,
        heldout=heldout_metric,
        dataset=scenario_ids,
    )
    return result


def _diff_instruction(project_root: str, scenario_ids: list[str], variant: PromptVariant) -> str:
    if not scenario_ids:
        return ""
    base = _compose_instruction(project_root, scenario_ids[0], None)
    cand = _compose_instruction(project_root, scenario_ids[0], variant)
    return "\n".join(
        difflib.unified_diff(base.splitlines(), cand.splitlines(), lineterm="")
    )


def _write_artifacts(
    project_root: str,
    *,
    name: str | None,
    baseline: dict[str, Any],
    evaluated: list[dict[str, Any]],
    winner: dict[str, Any] | None,
    heldout: dict[str, Any] | None,
    dataset: list[str],
) -> dict[str, Any]:
    """Write .agent-sim/optimized/<name>/ artifacts; return the result summary."""
    from ..config import load_config

    cfg = load_config(project_root)
    slug = name or f"optimize-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    out_dir = cfg.optimized_dir / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    candidate_dir = out_dir / "candidates"
    candidate_dir.mkdir(exist_ok=True)

    for ev in evaluated:
        v = ev.get("variant")
        if v is not None:
            write_variant(v, candidate_dir / f"{v.id}.yaml")

    baseline_json = {
        "name": slug,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_scenario_ids": dataset,
        "baseline": {
            "pass_rate": baseline["pass_rate"],
            "ok": baseline["ok"],
            "total": baseline["total"],
            "passed_gate": baseline["passed_gate"],
            "per_scenario": baseline["per_scenario"],
        },
        "candidates": [
            {
                "id": (e.get("variant").id if e.get("variant") else "?"),
                "pass_rate": e["pass_rate"],
                "ok": e["ok"],
                "per_scenario": e["per_scenario"],
            }
            for e in evaluated
        ],
    }
    if heldout is not None:
        baseline_json["held_out"] = {
            "scenario_ids": heldout.get("variant_id") and [],
            "pass_rate": heldout["pass_rate"],
            "ok": heldout["ok"],
            "per_scenario": heldout["per_scenario"],
        }
    (out_dir / "baseline.json").write_text(
        json.dumps(baseline_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    diff_parts: list[str] = []
    for ev in evaluated:
        v = ev.get("variant")
        diff_parts.append(f"=== candidate {v.id if v else '?'} (pass {ev['pass_rate']:.0%}) ===")
        diff_parts.append(ev.get("diff") or "(no diff)")
        diff_parts.append("")
    (out_dir / "diff.txt").write_text("\n".join(diff_parts), encoding="utf-8")

    if winner is not None:
        write_variant(winner["variant"], out_dir / "prompt.yaml")

    return {
        "name": slug,
        "dir": str(out_dir),
        "winner": {
            "id": winner["variant"].id,
            "pass_rate": winner["pass_rate"],
            "baseline_pass_rate": baseline["pass_rate"],
        }
        if winner is not None
        else None,
        "baseline_pass_rate": baseline["pass_rate"],
        "candidate_pass_rates": [
            {"id": e.get("variant").id if e.get("variant") else "?", "pass_rate": e["pass_rate"]}
            for e in evaluated
        ],
        "files": {
            "prompt.yaml": (out_dir / "prompt.yaml").exists(),
            "baseline.json": (out_dir / "baseline.json").exists(),
            "diff.txt": (out_dir / "diff.txt").exists(),
        },
        "held_out": {"pass_rate": heldout["pass_rate"], "ok": heldout["ok"]}
        if heldout is not None
        else None,
    }
