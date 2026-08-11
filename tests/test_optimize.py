"""lks optimize — persona-prompt optimizer (unit + mocked integration).

No live LiveKit in CI: ``ops.execute_scenario`` is injected/mocked with canned
result dicts; the LLM proposer is stubbed. Covers the pure optimizer pieces
(variant round-trip, policy apply, metric, selection) and the artifact wiring.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from livekit_agent_simulator.optimize import (
    add_guardrail,
    baseline_variant,
    dataset_pass_rate,
    deterministic_candidates,
    mutate_verbosity,
    policy_for_variant,
    select_winner,
)
from livekit_agent_simulator.optimize.apply import apply_variant_to_persona
from livekit_agent_simulator.optimize.eval import evaluate_variant
from livekit_agent_simulator.optimize.variant import (
    load_variant,
    validate_variant,
    write_variant,
)


# ---------------------------------------------------------------- variant unit

def test_baseline_variant_byte_identical() -> None:
    from livekit_agent_simulator.caller import build_persona_system_instruction

    kw = dict(persona={"name": "X", "brief": "b", "goals": ["g"], "style": "polite"}, locale="en-US")
    default = build_persona_system_instruction(**kw)
    with_policy = build_persona_system_instruction(**kw, policy=policy_for_variant(baseline_variant()))
    assert default == with_policy


def test_verbosity_variant_changes_instruction() -> None:
    from livekit_agent_simulator.caller import build_persona_system_instruction

    persona = {"name": "X", "brief": "billing", "goals": ["ask"], "style": "polite"}
    kw = dict(persona=persona, locale="en-US")
    base = build_persona_system_instruction(**kw)
    v = mutate_verbosity(baseline_variant(), "chatty")
    persona_c = apply_variant_to_persona(persona, v)
    chat = build_persona_system_instruction(**{**kw, "persona": persona_c}, policy=policy_for_variant(v))
    assert base != chat


def test_guardrail_variant_appends() -> None:
    from livekit_agent_simulator.caller import build_persona_system_instruction

    persona = {"name": "X", "brief": "b", "goals": ["g"], "style": "polite"}
    kw = dict(persona=persona, locale="en-US")
    base = build_persona_system_instruction(**kw)
    v = add_guardrail(baseline_variant(), "Never switch into assistant mode.")
    out = build_persona_system_instruction(**kw, policy=policy_for_variant(v))
    assert "Never switch into assistant mode." in out


def test_variant_roundtrip(tmp_path: Path) -> None:
    v = mutate_verbosity(baseline_variant(), "quiet")
    p = tmp_path / "v.yaml"
    write_variant(v, p)
    v2 = load_variant(p)
    assert v2.id == v.id
    assert v2.verbosity == "quiet"
    assert validate_variant(v2) == []


def test_validate_variant_rejects_unknown_section() -> None:
    from livekit_agent_simulator.optimize.variant import PromptVariant

    v = PromptVariant(id="bad", section_order=("Nope",))
    problems = validate_variant(v)
    assert any("unknown section" in p for p in problems)


def test_deterministic_candidates_nonempty() -> None:
    cands = deterministic_candidates()
    assert len(cands) >= 4
    ids = {c.id for c in cands}
    assert "verbosity-chatty" in ids and "verbosity-quiet" in ids


# ---------------------------------------------------------------- metric / selection

def _run_result(*, ok: bool) -> dict:
    return {
        "executed": True,
        "status": "done" if ok else "failed",
        "summary": {
            "assert_verify": {"pass": ok, "skipped": False},
            "script_verify": {"pass": True},
            "verdict": {"verdict": "pass" if ok else "fail"},
        },
    }


def test_dataset_pass_rate() -> None:
    # dataset_pass_rate counts gate rows (each carrying an `ok` bool).
    rows = [
        {"scenario_id": "a", "ok": True},
        {"scenario_id": "b", "ok": False},
        {"scenario_id": "c", "ok": True},
    ]
    m = dataset_pass_rate(rows)
    assert m["pass_rate"] == 2 / 3
    assert m["total"] == 3
    assert m["passed_gate"] == 2


def test_select_winner_beats_baseline() -> None:
    base = {"pass_rate": 0.5, "ok": False}
    cands = [
        {"pass_rate": 0.4, "ok": False},
        {"pass_rate": 0.8, "ok": True},
    ]
    w = select_winner(base, cands)
    assert w is not None and w["pass_rate"] == 0.8


def test_select_winner_none_when_tie() -> None:
    base = {"pass_rate": 0.5, "ok": False}
    cands = [{"pass_rate": 0.5, "ok": False}]
    assert select_winner(base, cands) is None


def test_select_winner_heldout_gate() -> None:
    base = {"pass_rate": 0.5, "ok": False}
    cands = [{"pass_rate": 0.8, "ok": True}]
    heldout_fail = {"pass_rate": 0.0, "ok": False}
    assert select_winner(base, cands, heldout=heldout_fail, heldout_threshold=1.0) is None


# ---------------------------------------------------------------- evaluate_variant (mocked)

async def _fake_execute(project_root, sid, *, repeat=1, pass_at_k=None, agent_name=None, optimized=None):
    # Simulate: baseline passes, anything optimized fails (so winner logic is testable)
    ok = optimized is None
    return _run_result(ok=ok)


@pytest.mark.asyncio
async def test_evaluate_variant_baseline() -> None:
    m = await evaluate_variant(
        ".", None, ["a", "b"],
        execute_scenario=_fake_execute,
    )
    assert m["pass_rate"] == 1.0
    assert m["variant_id"] == "baseline"


@pytest.mark.asyncio
async def test_evaluate_variant_with_optimize_kwarg() -> None:
    # optimized=... is passed through to the injected runner; our fake fails it.
    m = await evaluate_variant(
        ".", mutate_verbosity(baseline_variant(), "chatty"), ["a"],
        execute_scenario=_fake_execute,
        optimize="__candidate__x",
    )
    assert m["pass_rate"] == 0.0


# ---------------------------------------------------------------- gen (stubbed proposer)

@pytest.mark.asyncio
async def test_propose_candidates_deterministic_seed() -> None:
    from livekit_agent_simulator.optimize.gen import propose_candidates

    class _Stub:
        async def propose(self, *, system: str, user: str) -> str:
            return "[]"  # no LLM candidates

    cands = await propose_candidates(_Stub(), current_instruction="hello")
    assert len(cands) >= 4  # deterministic set always present


@pytest.mark.asyncio
async def test_propose_candidates_parses_llm_json() -> None:
    from livekit_agent_simulator.optimize.gen import propose_candidates

    class _Stub:
        async def propose(self, *, system: str, user: str) -> str:
            return json.dumps([{"id": "llm-verbosity", "verbosity": "chatty"}])

    cands = await propose_candidates(_Stub(), current_instruction="hi")
    ids = {c.id for c in cands}
    assert "llm-verbosity" in ids


# ---------------------------------------------------------------- optimize_persona integration

MIN_CONFIG = """livekit:
  url: wss://example.livekit.cloud
  api_key: test-key
  api_secret: test-secret
  agent_name: test-agent
simulator:
  api_key: test-sim-key
"""

MIN_SCENARIO = """apiVersion: agent-sim/v1
kind: Scenario
metadata:
  id: {sid}
  locale: en-US
persona:
  name: Maria
  brief: billing question
  goals:
  - Ask about the charge
  style: polite
execute:
  max_turns: 4
  timeout_s: 60
  first_speaker: agent
"""


def _seed_target(root: Path, sids: list[str]) -> None:
    dot = root / ".agent-sim"
    (dot / "scenarios").mkdir(parents=True, exist_ok=True)
    (dot / "config.yaml").write_text(MIN_CONFIG, encoding="utf-8")
    for sid in sids:
        (dot / "scenarios" / f"{sid}.yaml").write_text(
            MIN_SCENARIO.format(sid=sid), encoding="utf-8"
        )


async def _fake_execute_wins(project_root, sid, *, repeat=1, pass_at_k=None, agent_name=None, optimized=None):
    # optimized == "__candidate__chatty" → pass; everything else fails.
    ok = optimized == "__candidate__verbosity-chatty"
    return _run_result(ok=ok)


@pytest.mark.asyncio
async def test_optimize_persona_writes_winner_artifact(tmp_path: Path) -> None:
    _seed_target(tmp_path, ["a", "b"])
    from livekit_agent_simulator.optimize.optimize import optimize_persona

    result = await optimize_persona(
        str(tmp_path),
        ["a", "b"],
        candidates=4,
        max_candidates=4,
        execute_scenario=_fake_execute_wins,
        proposer=_StubProposer(),
    )
    assert result["winner"] is not None
    assert result["winner"]["id"] == "verbosity-chatty"
    out = tmp_path / ".agent-sim" / "optimized"
    prompt = next((out / result["name"]).glob("prompt.yaml"), None)
    assert prompt is not None and prompt.is_file()
    assert (out / result["name"] / "baseline.json").is_file()
    assert (out / result["name"] / "diff.txt").is_file()


class _StubProposer:
    async def propose(self, *, system: str, user: str) -> str:
        return "[]"


@pytest.mark.asyncio
async def test_optimize_persona_no_winner_keeps_baseline(tmp_path: Path) -> None:
    _seed_target(tmp_path, ["a", "b"])
    from livekit_agent_simulator.optimize.optimize import optimize_persona

    async def _never_wins(project_root, sid, *, repeat=1, pass_at_k=None, agent_name=None, optimized=None):
        return _run_result(ok=False)

    result = await optimize_persona(
        str(tmp_path),
        ["a", "b"],
        candidates=4,
        max_candidates=4,
        execute_scenario=_never_wins,
        proposer=_StubProposer(),
    )
    assert result["winner"] is None
    # artifact dir still written (empty winner)
    assert (tmp_path / ".agent-sim" / "optimized" / result["name"] / "baseline.json").is_file()
