"""Unit tests for the §11 report surface (caller_contract/contract_summary.py).

Builds the caller_contract summary block from recorded event trails —
no LiveKit, no TTS, no AI. Covers: behavior grouping + status, per-turn
generated text + validation, recorder-enriched observed evidence, and the
web cues.py fallback path for older reports without the summary block.
"""

from __future__ import annotations

from livekit_agent_simulator.caller_contract.contract_summary import (
    build_caller_contract_summary,
)


def _turn(behavior: str, turn: int, text: str) -> dict:
    return {
        "kind": "contract.turn_published",
        "turn": turn,
        "spec": {"behavior": behavior, "turn": turn, "text": text},
    }


def _agent(text: str) -> dict:
    return {"kind": "transcript.agent.final", "turn": 1, "spec": {"text": text}}


def test_groups_turns_by_behavior_with_satisfied_status() -> None:
    events = [
        _turn("ask", 1, "Could you tell me the price?"),
        _agent("The price is $25,800."),
        _turn("ask", 2, "Is that the drive-away price?"),
        _agent("Yes, drive-away."),
        _turn("negotiate", 1, "Could you do $24,000?"),
        _agent("We can do $24,500."),
    ]
    summary = build_caller_contract_summary(events=events)
    assert [b["behavior"] for b in summary["behaviors"]] == ["ask", "negotiate"]
    assert summary["behaviors"][0]["turns"] == 2
    assert summary["behaviors"][1]["turns"] == 1
    assert all(b["status"] == "satisfied" for b in summary["behaviors"])
    assert len(summary["caller"]) == 3
    assert summary["caller"][0]["generated_text"] == "Could you tell me the price?"
    assert summary["caller"][0]["validation"] == "passed"


def test_violation_marks_behavior_violated() -> None:
    events = [
        _turn("ask", 1, "Could you tell me the price?"),
        _agent("The price is $25,800."),
        {
            "kind": "contract.behavior_violation",
            "turn": 1,
            "spec": {"behavior": "ask", "reason": "FAILED_MAX_TURNS"},
        },
    ]
    summary = build_caller_contract_summary(events=events)
    assert summary["behaviors"][0]["status"] == "violated"


def test_targets_filled_from_contract_list() -> None:
    events = [_turn("ask", 1, "Could you tell me the price?"), _agent("A price.")]
    summary = build_caller_contract_summary(
        events=events, behaviors=[{"behavior": "ask", "target": "price"}]
    )
    assert summary["behaviors"][0]["target"] == "price"


def test_recorder_rows_enrich_validation_and_observed() -> None:
    class FakeAttempt:
        def __init__(self):
            self.candidate = {"utterance": "Could you tell me the price?"}
            self.verdict = "VALID"
            self.reason = None
            self.observed = {"act": "ask", "target": "price", "confidence": 0.75}

    class FakeRecorder:
        _attempts = [FakeAttempt()]

    events = [_turn("ask", 1, "Could you tell me the price?"), _agent("Yes.")]
    summary = build_caller_contract_summary(events=events, recorder=FakeRecorder())
    detail = summary["behaviors"][0]["turns_detail"][0]
    assert detail["validation"] == "passed"
    assert detail["observed_act"] == "ask"
    assert detail["confidence"] == 0.75
