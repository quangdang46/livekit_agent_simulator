"""P1.C — PassCriteria multi-judge parse + aggregate (no live LLM)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from livekit_agent_simulator.evals import aggregate, runner
from livekit_agent_simulator.scenario import parse_scenario


def test_parse_judges_and_mode(tmp_path: Path):
    p = tmp_path / "mj.jsonl"
    p.write_text(
        "\n".join(
            [
                '{"apiVersion":"agent-sim/v1","kind":"Scenario","metadata":{"id":"mj","locale":"en-US"}}',
                '{"kind":"Persona","spec":{"name":"A","brief":"caller","goals":["g"]}}',
                '{"kind":"Execute","spec":{"max_turns":3}}',
                '{"kind":"PassCriteria","spec":{"mode":"majority","judges":['
                '{"id":"task","criteria":["Task completed"]},'
                '{"id":"tone","criteria":["Polite tone"]}'
                "]}}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    s = parse_scenario(p)
    assert s.pass_criteria_mode == "majority"
    assert len(s.pass_judges) == 2
    assert s.pass_judges[0]["id"] == "task"
    assert any("task" in c for c in s.pass_criteria)


def test_parse_builtin_judge(tmp_path: Path):
    p = tmp_path / "bj.jsonl"
    p.write_text(
        "\n".join(
            [
                '{"apiVersion":"agent-sim/v1","kind":"Scenario","metadata":{"id":"bj","locale":"en-US"}}',
                '{"kind":"Persona","spec":{"name":"A","brief":"caller","goals":["g"]}}',
                '{"kind":"Execute","spec":{"max_turns":2}}',
                '{"kind":"PassCriteria","spec":{"judges":['
                '{"id":"tc","builtin":"task_completion"}'
                "]}}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    s = parse_scenario(p)
    assert s.pass_judges[0]["builtin"] == "task_completion"
    assert any("builtin:task_completion" in c for c in s.pass_criteria)


@pytest.mark.asyncio
async def test_aggregate_all_majority_any(monkeypatch: pytest.MonkeyPatch):
    async def fake_judge(
        backend: Any,
        criteria: list[str],
        turns: list,
        tools: list,
        *,
        goals_met=None,
    ):
        text = " ".join(criteria)
        if "PASS" in text:
            return {"verdict": "pass", "score": 90}
        return {"verdict": "fail", "score": 10}

    monkeypatch.setattr(runner, "_judge", fake_judge)
    judges = [
        {"id": "a", "criteria": ["PASS me"]},
        {"id": "b", "criteria": ["fail me"]},
    ]
    from livekit_agent_simulator.config import JudgeConfig

    cfg = JudgeConfig(base_url="http://example/v1", api_key="k", model="m")
    all_v = await runner.judge_run_multi(cfg, "g", judges, "all", [], [])
    assert all_v["verdict"] == "fail"
    maj = await runner.judge_run_multi(cfg, "g", judges, "majority", [], [])
    assert maj["verdict"] == "fail"
    any_v = await runner.judge_run_multi(cfg, "g", judges, "any", [], [])
    assert any_v["verdict"] == "pass"

    both = [
        {"id": "a", "criteria": ["PASS"]},
        {"id": "b", "criteria": ["PASS too"]},
    ]
    all_ok = await runner.judge_run_multi(cfg, "g", both, "all", [], [])
    assert all_ok["verdict"] == "pass"
    assert all_ok["passed_count"] == 2


def test_aggregate_all_errors_is_error():
    out = aggregate.aggregate_judges(
        [
            {"verdict": "error", "notes": "HTTP 401"},
            {"verdict": "error", "notes": "timeout"},
        ],
        "all",
    )
    assert out["verdict"] == "error"
