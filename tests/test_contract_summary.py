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


def test_truncated_turns_are_surfaced_not_left_inside_a_data_blob() -> None:
    """A run that lost turns must not read like one that did not.

    The flow runtime publishes `agent_turn_truncated` on `voice_ai.flow`. Before
    this it landed inside a generic `data.message` payload, which is exactly
    where the truncation was invisible for a day on 2026-09-30: the evidence
    existed in the log and nothing summarised it.

    `missing_text` is carried, not reduced to a boolean — a bare flag is the
    shape `confidence` had for two hours, a name implying information it does
    not carry.
    """
    from livekit_agent_simulator.caller_contract.contract_summary import (
        build_caller_contract_summary,
    )

    events = [
        {
            "kind": "contract.agent_turn_truncated",
            "spec": {
                "turn": 3,
                "node_id": "n1",
                "spoken_len": 40,
                "instructed_len": 60,
                "missing_len": 20,
                "missing_text": " the building's address?",
            },
        },
        {
            "kind": "contract.agent_turn_truncated",
            "spec": {"turn": 5, "node_id": "n2", "missing_len": 9, "missing_text": " how many?"},
        },
    ]
    summary = build_caller_contract_summary(events=events)

    assert summary["turns_truncated"] == 2, summary
    detail = summary["truncated_turns_detail"]
    assert [d["turn"] for d in detail] == [3, 5], detail
    assert detail[0]["missing_text"] == " the building's address?", (
        "the MISSING TAIL is the evidence — a count alone says a run was lossy "
        "without saying what was lost"
    )


def test_a_clean_run_reports_zero_truncations_not_a_missing_key() -> None:
    """Zero, not absent.

    A consumer checking `turns_truncated` must not have to distinguish "the key
    is missing because the summary is old" from "this run lost nothing".
    """
    from livekit_agent_simulator.caller_contract.contract_summary import (
        build_caller_contract_summary,
    )

    summary = build_caller_contract_summary(events=[])
    assert summary["turns_truncated"] == 0
    assert summary["truncated_turns_detail"] == []
