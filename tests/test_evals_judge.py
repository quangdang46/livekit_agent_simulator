"""Unit tests for evals package (no live network)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from livekit_agent_simulator.config import JudgeConfig
from livekit_agent_simulator.evals.evidence import build_evidence_packet
from livekit_agent_simulator.evals.presets import expand_criterion, expand_judge_group, list_presets
from livekit_agent_simulator.evals.relevancy import apply_relevancy
from livekit_agent_simulator.evals.resolve import resolve_judge
from livekit_agent_simulator.evals.runner import _judge, judge_goals, judge_run
from livekit_agent_simulator.evals.types import CriterionScore, JudgmentResult, parse_judgment_payload


def test_resolve_http_from_config():
    r = resolve_judge(
        JudgeConfig(base_url="http://localhost:8080/v1", api_key="sk", model="gpt-4o-mini"),
        google_api_key="ignored",
    )
    assert r.ready and r.mode == "http"
    assert r.base_url == "http://localhost:8080/v1"
    assert r.endpoint_type == "openai"


def test_resolve_http_anthropic_api():
    r = resolve_judge(
        JudgeConfig(
            base_url="http://localhost:8080/v1",
            api_key="sk",
            model="m",
            endpoint_type="anthropic",
        ),
        google_api_key="g",
    )
    assert r.ready and r.mode == "http" and r.endpoint_type == "anthropic"


def test_backend_for_anthropic():
    from livekit_agent_simulator.evals.backend import backend_for
    from livekit_agent_simulator.evals.backends.http_anthropic import HttpAnthropicBackend
    from livekit_agent_simulator.evals.resolve import ResolvedJudge

    b = backend_for(
        ResolvedJudge(
            model="m",
            temperature=0.0,
            base_url="http://x/v1",
            api_key="k",
            google_api_key=None,
            mode="http",
            endpoint_type="anthropic",
            ready=True,
        )
    )
    assert isinstance(b, HttpAnthropicBackend)


def test_anthropic_extract_text():
    from livekit_agent_simulator.evals.backends.http_anthropic import _extract_anthropic_text

    text = _extract_anthropic_text(
        {"content": [{"type": "thinking", "text": "…"}, {"type": "text", "text": '{"verdict":"pass"}'}]}
    )
    assert text == '{"verdict":"pass"}'


def test_resolve_http_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JUDGE_BASE_URL", "http://gw/v1")
    monkeypatch.setenv("JUDGE_API_KEY", "sk_env")
    monkeypatch.setenv("JUDGE_MODEL", "m1")
    r = resolve_judge(JudgeConfig(), google_api_key="g")
    assert r.ready and r.mode == "http"
    assert r.model == "m1"
    assert r.api_key == "sk_env"


def test_resolve_gemini_legacy():
    r = resolve_judge(JudgeConfig(model="gemini-2.5-flash"), google_api_key="gkey")
    assert r.ready and r.mode == "gemini"


def test_resolve_http_missing_key():
    r = resolve_judge(JudgeConfig(base_url="http://x/v1"), google_api_key="g")
    assert not r.ready


def test_presets_expand():
    assert "task_completion" in list_presets()
    text = expand_criterion("builtin:accuracy")
    assert "tool" in text.lower() or "Accuracy" in text
    g = expand_judge_group({"id": "t", "builtin": "task_completion", "criteria": []})
    assert len(g["criteria"]) == 1


def test_evidence_packet():
    p = build_evidence_packet(
        [{"turn": 1, "user_text": "hi", "agent_text": "hello"}],
        [{"kind": "tool.end", "turn": 1, "spec": {"name": "book", "error": None}}],
    )
    assert "CALLER: hi" in p["transcript"]
    assert "book" in p["tool_spans"]


def test_relevancy_filters_irrelevant_fails():
    raw = JudgmentResult(
        verdict="fail",
        score=40,
        criteria=[
            CriterionScore("A", met=True, relevant=True, evidence="ok"),
            CriterionScore("B", met=False, relevant=False, evidence="n/a"),
        ],
    )
    out = apply_relevancy(raw)
    assert out.verdict == "pass"


def test_parse_judgment_maybe():
    j = parse_judgment_payload(
        {
            "verdict": "maybe",
            "score": 55,
            "confidence": "low",
            "needs_human_review": True,
            "criteria": [{"criterion": "x", "met": True, "relevant": True, "evidence": "e"}],
        }
    )
    assert j.verdict == "maybe"
    assert j.confidence == "low"
    assert j.needs_human_review


@pytest.mark.asyncio
async def test_judge_run_with_mock_backend(monkeypatch: pytest.MonkeyPatch):
    class MockBackend:
        async def complete_json(self, *, system: str, user: str) -> str:
            return json.dumps(
                {
                    "verdict": "pass",
                    "score": 95,
                    "confidence": "high",
                    "criteria": [
                        {
                            "criterion": "greets",
                            "met": True,
                            "relevant": True,
                            "evidence": "AGENT: hello",
                        }
                    ],
                    "notes": "ok",
                }
            )

    monkeypatch.setattr(
        "livekit_agent_simulator.evals.runner.backend_from_config",
        lambda cfg, key: MockBackend(),
    )
    cfg = JudgeConfig(base_url="http://x/v1", api_key="k", model="m")
    out = await judge_run(cfg, "g", ["agent greets"], [], [])
    assert out["verdict"] == "pass"
    assert out["confidence"] == "high"


@pytest.mark.asyncio
async def test_goals_met_skips_without_judge():
    out = await judge_goals(None, "g", ["buy milk"], 1, [])
    assert out["verdict"] == "skipped"


@pytest.mark.asyncio
async def test_judge_once_parse_error():
    class BadBackend:
        async def complete_json(self, *, system: str, user: str) -> str:
            return "not-json"

    out = await _judge(BadBackend(), ["c"], [], [])
    assert out["verdict"] == "error"
