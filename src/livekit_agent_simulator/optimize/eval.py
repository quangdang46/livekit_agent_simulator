"""Variant evaluation — per-run gate → dataset pass rate → winner selection.

The metric is the same CI gate lks already uses (``evaluate_run_result`` /
``build_suite_report``), so a candidate's pass rate is directly comparable to
``lks execute-all``. Unit is ONE scenario: ``execute_scenario``'s ``ok`` embeds
pass@k, and ``build_suite_report`` counts soft-judge rows as passed when not
strict — identical to the existing suite semantics.
"""

from __future__ import annotations

from typing import Any, Iterable

from .variant import PromptVariant
from ..suite import evaluate_run_result


def dataset_pass_rate(results: list[dict[str, Any]], *, strict_judge: bool = False) -> dict[str, Any]:
    """Aggregate per-scenario gate rows into {pass_rate, ok, total, passed_gate}.

    Each row must carry ``ok`` (a gate result). ``strict_judge`` only affects
    per-run gating done by the caller; aggregation here just counts ``ok``.
    """
    total = len(results)
    passed = sum(1 for r in results if bool(r.get("ok")))
    return {
        "pass_rate": passed / total if total else 0.0,
        "ok": passed == total,
        "total": total,
        "passed_gate": passed,
        "matrix": results,
    }


async def evaluate_variant(
    project_root: str,
    variant: PromptVariant | None,
    scenario_ids: list[str],
    *,
    execute_scenario: Any,
    strict_judge: bool = False,
    repeat: int = 1,
    pass_at_k: int | None = None,
    agent_name: str | None = None,
    optimize: str | None = None,
) -> dict[str, Any]:
    """Run a variant over a scenario set (via injected execute_scenario) → dataset metric.

    ``execute_scenario`` is injected so tests can mock it; production passes
    ``ops.execute_scenario``. ``optimize`` = the saved artifact name to apply
    (optional; ``None`` runs the builtin persona). Returns {variant_id,
    pass_rate, ok, total, passed_gate, per_scenario: [...]}.
    """
    per_scenario: list[dict[str, Any]] = []
    for sid in scenario_ids:
        kwargs: dict[str, Any] = dict(
            repeat=repeat, pass_at_k=pass_at_k, agent_name=agent_name
        )
        if optimize is not None:
            kwargs["optimized"] = optimize
        result = await execute_scenario(project_root, sid, **kwargs)
        gate = evaluate_run_result(result, strict_judge=strict_judge)
        per_scenario.append(
            {
                "scenario_id": sid,
                "ok": gate["ok"],
                "gate": gate["gate"],
                "hard_reasons": gate["hard_reasons"],
                "soft_reasons": gate["soft_reasons"],
            }
        )
    metric = dataset_pass_rate([r for r in per_scenario], strict_judge=strict_judge)
    return {
        "variant_id": variant.id if variant is not None else "baseline",
        "pass_rate": metric["pass_rate"],
        "ok": metric["ok"],
        "total": metric["total"],
        "passed_gate": metric["passed_gate"],
        "per_scenario": per_scenario,
    }


def select_winner(
    baseline: dict[str, Any],
    candidates: Iterable[dict[str, Any]],
    *,
    heldout: dict[str, Any] | None = None,
    heldout_threshold: float = 1.0,
) -> dict[str, Any] | None:
    """Pick the best candidate that strictly beats baseline AND passes held-out.

    Returns the winning candidate eval dict, or None (no winner → keep baseline).
    """
    best: dict[str, Any] | None = None
    best_rate = baseline["pass_rate"]
    for cand in candidates:
        if cand["pass_rate"] <= best_rate:
            continue  # must strictly beat baseline
        if heldout is not None:
            if heldout.get("pass_rate", 0.0) < heldout_threshold or not heldout.get("ok", False):
                continue  # generalization check failed
        if best is None or cand["pass_rate"] > best["pass_rate"]:
            best = cand
            best_rate = cand["pass_rate"]
    return best
