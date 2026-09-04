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
from livekit_agent_simulator.evals.runner import (
    _judge,
    _parse_llm_json,
    _repair_truncated_json,
    judge_goals,
    judge_run,
)
from livekit_agent_simulator.evals.types import CriterionScore, JudgmentResult, parse_judgment_payload


def test_resolve_http_from_config():
    r = resolve_judge(
        JudgeConfig(base_url="http://localhost:8080/v1", api_key="sk", model="gpt-4o-mini"),
        sim_api_key="ignored",
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
        sim_api_key="g",
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
            sim_api_key=None,
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
    r = resolve_judge(JudgeConfig(), sim_api_key="g")
    assert r.ready and r.mode == "http"
    assert r.model == "m1"
    assert r.api_key == "sk_env"


def test_resolve_gemini_legacy():
    r = resolve_judge(JudgeConfig(model="gemini-2.5-flash"), sim_api_key="gkey")
    assert r.ready and r.mode == "gemini"


def test_resolve_http_missing_key():
    r = resolve_judge(JudgeConfig(base_url="http://x/v1"), sim_api_key="g")
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
    assert "Caller" in p["transcript"] and "hi" in p["transcript"]
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


def test_relevancy_promotion_flags_human_review():
    """fail→pass promotion via self-labeled-irrelevant criteria must not be
    trusted blindly — flag for human review (issue #99)."""
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
    assert out.needs_human_review is True


def test_parse_judgment_ungrounded_met_criterion_is_not_trusted():
    """A criterion the judge marks met=True but cites no evidence for is a
    hallucination risk (issue #99): a topically-related-but-wrong agent
    reply must not be able to pass just because the judge asserts it did,
    with no quoted transcript evidence backing the claim."""
    j = parse_judgment_payload(
        {
            "verdict": "pass",
            "criteria": [
                {"criterion": "asked for company name", "met": True, "relevant": True, "evidence": ""},
            ],
        }
    )
    assert j.criteria[0].met is False
    assert j.needs_human_review is True


def test_parse_judgment_grounded_met_criterion_is_trusted():
    j = parse_judgment_payload(
        {
            "verdict": "pass",
            "criteria": [
                {
                    "criterion": "asked for company name",
                    "met": True,
                    "relevant": True,
                    "evidence": "Agent said: 'Could you say company name again, please?'",
                },
            ],
        }
    )
    assert j.criteria[0].met is True
    assert j.needs_human_review is False


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


def test_parse_conversation_feedback_preserved():
    """The real-reviewer feedback list must survive the LLM-JSON parse and to_dict round-trip."""
    j = parse_judgment_payload(
        {
            "verdict": "fail",
            "score": 62,
            "notes": "review",
            "conversation_feedback": [
                {
                    "issue": "Two questions stacked in one turn",
                    "severity": "high",
                    "agent_line": "緊急連絡先の氏名と、折り返しの時間帯を教えてください",
                    "why": "A human caller hears two asks at once and loses track of which to answer.",
                },
                {
                    "issue": "Relative date converted to absolute without confirmation",
                    "severity": "medium",
                    "agent_line": "来月の1日は2026年9月1日ですね。",
                    "why": "Without current-date context this reads as a hallucination.",
                },
            ],
        }
    )
    assert len(j.conversation_feedback) == 2
    f = j.conversation_feedback[0]
    assert f.issue == "Two questions stacked in one turn"
    assert f.severity == "high"
    assert "緊急連絡先" in f.agent_line
    assert "human caller" in f.why.lower()
    d = j.to_dict()
    assert "conversation_feedback" in d
    assert d["conversation_feedback"][0]["severity"] == "high"
    assert "agent_line" in d["conversation_feedback"][0]


def test_parse_conversation_feedback_empty_omitted():
    """No feedback list → to_dict must not emit a misleading empty array."""
    j = parse_judgment_payload({"verdict": "pass", "score": 90})
    assert j.conversation_feedback == []
    assert "conversation_feedback" not in j.to_dict()


def test_parse_free_style_review_round_trip():
    """Generic free-style review fields must survive parse → to_dict."""
    j = parse_judgment_payload(
        {
            "verdict": "fail",
            "score": 62,
            "overall_summary": "The call achieved its goal but with notable friction.",
            "strengths": [{"point": "Clear greeting"}],
            "issues": [
                {
                    "title": "Two questions stacked in one turn",
                    "severity": "Critical",
                    "evidence": "緊急連絡先の氏名と、折り返しの時間帯を教えてください",
                    "impact": "The caller loses track of which to answer.",
                    "recommendation": "Ask one question at a time.",
                }
            ],
            "missing_checks": [{"item": "No call-back time was agreed"}],
            "language_naturalness": [{"issue": "Rushed pacing on turn 3"}],
            "final_assessment": {
                "goal_achievement": "7/10",
                "understanding": "8/10",
                "conversation_flow": "5/10",
                "clarity": "6/10",
                "user_experience": "5/10",
                "conclusion": "Workable but rough.",
            },
        }
    )
    assert j.overall_summary.startswith("The call achieved")
    assert j.strengths == ["Clear greeting"]
    assert len(j.issues) == 1
    assert j.issues[0].title == "Two questions stacked in one turn"
    assert j.issues[0].severity == "Critical"
    assert j.issues[0].recommendation == "Ask one question at a time."
    assert j.missing_checks == ["No call-back time was agreed"]
    assert j.language_naturalness == ["Rushed pacing on turn 3"]
    assert j.final_assessment["goal_achievement"] == "7/10"

    d = j.to_dict()
    assert d["overall_summary"] == j.overall_summary
    assert d["issues"][0]["evidence"].startswith("緊急")
    assert d["issues"][0]["recommendation"] == "Ask one question at a time."
    assert d["final_assessment"]["user_experience"] == "5/10"


def test_parse_free_style_legacy_aliases():
    """Tolerate old field names (works / issue / agent_line / improvement)."""
    j = parse_judgment_payload(
        {
            "verdict": "maybe",
            "works": [{"point": "Politeness"}],
            "issues": [
                {
                    "issue": "Repeated re-ask",
                    "severity": "Major",
                    "agent_line": "Could you repeat the address?",
                    "why": "Already answered twice.",
                    "improvement": "Honor the earlier answer.",
                }
            ],
        }
    )
    assert j.strengths == ["Politeness"]
    assert j.issues[0].title == "Repeated re-ask"
    assert j.issues[0].recommendation == "Honor the earlier answer."


def test_parse_free_style_empty_fields_omitted():
    j = parse_judgment_payload({"verdict": "pass", "score": 90})
    assert j.overall_summary == ""
    assert j.issues == []
    assert "issues" not in j.to_dict()
    assert "strengths" not in j.to_dict()


def test_relevancy_preserves_free_style_review():
    """Relevancy rewrite must not drop the free-style review content."""
    j = parse_judgment_payload(
        {
            "verdict": "fail",
            "score": 40,
            "overall_summary": "summary",
            "issues": [{"title": "t", "severity": "Major", "evidence": "e"}],
            "criteria": [
                {"criterion": "A", "met": True, "relevant": True, "evidence": "ok"},
                {"criterion": "B", "met": False, "relevant": False, "evidence": "n/a"},
            ],
        }
    )
    out = apply_relevancy(j)
    assert out.verdict == "pass"
    assert out.overall_summary == "summary"
    assert len(out.issues) == 1


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


# ---------------------------------------------------------------------------
# Tolerant parsing of truncated / malformed LLM JSON (see runner.py)
# ---------------------------------------------------------------------------


def test_repair_truncated_summary_string():
    """The exact failure from a real run: overall_summary cut mid-word, no closing brace."""
    bad = (
        '{\n  "verdict": "fail",\n  "score": 38,\n  "confidence": "high",\n'
        '  "needs_human_review": true,\n  "critical_failure": true,\n'
        '  "overall_summary": "The agent made polite progress, but the flow never '
        'advanced and the call ended without confirmation. The conversation i'
    )
    repaired = _repair_truncated_json(bad)
    assert repaired is not None
    j = _parse_llm_json(bad)
    assert j.verdict == "fail"
    assert j.score == 38
    assert j.overall_summary.startswith("The agent made polite progress")
    assert j.overall_summary.endswith("The conversation i")


def test_repair_unterminated_string_with_backslash():
    j = _parse_llm_json('{"verdict":"fail","notes":"foo\\')
    assert j.verdict == "fail"
    assert j.notes == "foo"


def test_repair_truncated_key_dropped():
    """Regression: judge returned JSON truncated mid-KEY (before `: value`), e.g.
    a Gemini judge cutting off at `"needs_huma`. The dangling key must be dropped
    so the repaired object parses (real run surfaced `verdict: error` on a pass)."""
    j = _parse_llm_json(
        '{\n  "verdict": "pass",\n  "score": 92,\n  "confidence": "high",\n  "needs_huma'
    )
    assert j.verdict == "pass"
    assert j.score == 92


def test_repair_nested_object_missing_braces():
    j = _parse_llm_json(
        '{"verdict":"fail","final_assessment":{"goal":"7/10","conclusion":"rough'
    )
    assert j.verdict == "fail"
    assert j.final_assessment.get("conclusion") == "rough"


def test_repair_nested_array_missing_bracket():
    j = _parse_llm_json('{"verdict":"fail","issues":[{"title":"t","severity":"Major"')
    assert j.verdict == "fail"
    assert len(j.issues) == 1
    assert j.issues[0].title == "t"


def test_repair_missing_closing_brace_after_clean_prefix():
    j = _parse_llm_json('{"verdict":"pass","score":90')
    assert j.verdict == "pass"
    assert j.score == 90


def test_repair_trailing_comma_in_array():
    j = _parse_llm_json('{"verdict":"pass","strengths":["a","b",]')
    assert j.verdict == "pass"
    assert j.strengths == ["a", "b"]


def test_repair_json_fence_and_prose_prefix():
    j = _parse_llm_json('Sure! Here is the JSON:\n```json\n{"verdict":"maybe"}\n```')
    assert j.verdict == "maybe"


def test_repair_trailing_prose_after_object():
    j = _parse_llm_json('{"verdict":"fail","score":40} I think it failed because...')
    assert j.verdict == "fail"
    assert j.score == 40


def test_repair_does_not_mangle_already_valid_json():
    j = _parse_llm_json('{"verdict":"pass","score":90}')
    assert j.verdict == "pass"
    assert j.score == 90


def test_repair_rejects_unrecoverable_input():
    assert _repair_truncated_json("not-json at all") is None
    assert _repair_truncated_json("") is None
    assert _repair_truncated_json("42") is None
    j = _parse_llm_json("not-json")
    assert j.verdict == "error"
    assert "non-JSON" in j.notes
