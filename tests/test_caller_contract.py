"""P0-1: Caller Contract schema tests.

Covers schema validation, UNKNOWN/ERROR mapping, generation-identity
staleness predicate, and golden JSON fixtures under
tests/fixtures/caller_contract/.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from livekit_agent_simulator.caller_contract import (
    BehaviorContract,
    CandidateUtterance,
    ContractConstraints,
    EndedBy,
    EvaluatorVerdict,
    FailureReason,
    GenerationIdentity,
    ObservedAct,
    ValidationResult,
    Verdict,
    contract_json_schema,
    is_current,
    to_dict,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "caller_contract"


# ---------------------------------------------------------------------------
# BehaviorContract / ContractConstraints validation
# ---------------------------------------------------------------------------


def test_behavior_contract_valid() -> None:
    contract = BehaviorContract(
        behavior="negotiate",
        target="price",
        constraints=ContractConstraints(max_turns=3, max_budget=30000),
    )
    contract.validate()  # must not raise


def test_behavior_contract_rejects_empty_behavior() -> None:
    contract = BehaviorContract(behavior="")
    with pytest.raises(ValueError, match="behavior must be a non-empty string"):
        contract.validate()


def test_constraints_rejects_zero_max_turns() -> None:
    with pytest.raises(ValueError, match="max_turns must be >= 1"):
        ContractConstraints(max_turns=0).validate()


def test_constraints_rejects_non_positive_max_duration() -> None:
    with pytest.raises(ValueError, match="max_duration_s must be > 0"):
        ContractConstraints(max_duration_s=0).validate()


def test_constraints_action_space_not_vocabulary_space() -> None:
    """Contract uses forbidden_intents/must_not, never allowed_topics."""
    c = ContractConstraints(forbidden_intents=["financing"], must_not=["end_call"])
    assert not hasattr(c, "allowed_topics")
    assert not hasattr(c, "forbidden_topics")


# ---------------------------------------------------------------------------
# GenerationIdentity / staleness predicate
# ---------------------------------------------------------------------------


def test_generation_identity_valid() -> None:
    ident = GenerationIdentity(behavior_id="b1", turn_id=0, generation_id=0)
    ident.validate()


def test_generation_identity_rejects_empty_behavior_id() -> None:
    with pytest.raises(ValueError, match="behavior_id must be a non-empty string"):
        GenerationIdentity(behavior_id="", turn_id=0, generation_id=0).validate()


def test_generation_identity_rejects_negative_turn_id() -> None:
    with pytest.raises(ValueError, match="turn_id must be >= 0"):
        GenerationIdentity(behavior_id="b1", turn_id=-1, generation_id=0).validate()


def test_is_current_matches_when_triple_equal() -> None:
    current = GenerationIdentity(behavior_id="b1", turn_id=1, generation_id=2)
    same = GenerationIdentity(behavior_id="b1", turn_id=1, generation_id=2, context_version=99)
    assert is_current(same, current) is True


def test_is_current_stale_generation_dropped() -> None:
    """Race-condition guard (report §28.1): stale generation_id must not execute."""
    current = GenerationIdentity(behavior_id="b1", turn_id=1, generation_id=2)
    stale = GenerationIdentity(behavior_id="b1", turn_id=1, generation_id=1)
    assert is_current(stale, current) is False


def test_is_current_stale_behavior_changed_while_tts() -> None:
    """Race-condition guard: behavior_id changed underneath a running TTS job."""
    current = GenerationIdentity(behavior_id="b3", turn_id=0, generation_id=0)
    stale = GenerationIdentity(behavior_id="b2", turn_id=0, generation_id=0)
    assert is_current(stale, current) is False


def test_context_version_not_part_of_staleness_triple() -> None:
    """context_version tracks ConversationContext freshness only, not the triple."""
    current = GenerationIdentity(behavior_id="b1", turn_id=0, generation_id=0, context_version=5)
    candidate = GenerationIdentity(behavior_id="b1", turn_id=0, generation_id=0, context_version=1)
    assert is_current(candidate, current) is True


# ---------------------------------------------------------------------------
# CandidateUtterance (generator claim, not evidence)
# ---------------------------------------------------------------------------


def test_candidate_utterance_valid() -> None:
    ident = GenerationIdentity(behavior_id="b2", turn_id=1, generation_id=1)
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={"max_budget": 30000},
        utterance="Would you be able to come down to $30,000?",
        identity=ident,
    )
    candidate.validate()


def test_candidate_utterance_rejects_empty_utterance() -> None:
    ident = GenerationIdentity(behavior_id="b2", turn_id=1, generation_id=1)
    candidate = CandidateUtterance(act="negotiate", target="price", slots={}, utterance="", identity=ident)
    with pytest.raises(ValueError, match="utterance must be a non-empty string"):
        candidate.validate()


def test_candidate_utterance_rejects_non_dict_slots() -> None:
    ident = GenerationIdentity(behavior_id="b2", turn_id=1, generation_id=1)
    candidate = CandidateUtterance(
        act="negotiate", target="price", slots="not-a-dict", utterance="hi", identity=ident
    )
    with pytest.raises(ValueError, match="slots must be a dict"):
        candidate.validate()


# ---------------------------------------------------------------------------
# ObservedAct (verifier evidence, multi-label)
# ---------------------------------------------------------------------------


def test_observed_act_valid() -> None:
    observed = ObservedAct(act="negotiate", target="price", confidence=0.92)
    observed.validate()


def test_observed_act_rejects_out_of_range_confidence() -> None:
    observed = ObservedAct(act="negotiate", target="price", confidence=1.5)
    with pytest.raises(ValueError, match=r"confidence must be in \[0.0, 1.0\]"):
        observed.validate()


def test_observed_act_multi_label_detects_secondary_act() -> None:
    """Report §28.3(10): multi-act utterances must surface all detected acts."""
    observed = ObservedAct(
        act="negotiate",
        target="price",
        confidence=0.7,
        all_acts=["negotiate", "ask"],
    )
    assert "ask" in observed.all_acts


# ---------------------------------------------------------------------------
# ValidationResult verdict mapping (UNKNOWN -> reject, ERROR -> fail)
# ---------------------------------------------------------------------------


def test_validation_result_valid_verdict() -> None:
    result = ValidationResult(verdict=Verdict.VALID)
    assert result.is_valid() is True


def test_validation_result_invalid_verdict_not_valid() -> None:
    result = ValidationResult(verdict=Verdict.INVALID, reason="TARGET_MISMATCH")
    assert result.is_valid() is False


def test_validation_result_unknown_verdict_is_not_valid() -> None:
    """UNKNOWN must never be treated as VALID (report §14.1 / §17.4)."""
    result = ValidationResult(verdict=Verdict.UNKNOWN, reason="low_confidence")
    assert result.is_valid() is False


def test_validation_result_error_verdict_is_not_valid() -> None:
    """ERROR (verifier crashed/unavailable) must never be treated as VALID."""
    result = ValidationResult(verdict=Verdict.ERROR, reason="verifier_unavailable")
    assert result.is_valid() is False


@pytest.mark.parametrize("verdict", [Verdict.INVALID, Verdict.UNKNOWN, Verdict.ERROR])
def test_only_valid_verdict_permits_publish(verdict: Verdict) -> None:
    """Enforcement boundary: unvalidated utterance never reaches LiveKit."""
    result = ValidationResult(verdict=verdict)
    assert result.is_valid() is False


# ---------------------------------------------------------------------------
# FailureReason / EndedBy / EvaluatorVerdict enums (shared primitives)
# ---------------------------------------------------------------------------


def test_failure_reason_has_seven_canonical_codes() -> None:
    expected = {
        "CALLER_BEHAVIOR_VIOLATION",
        "LANGUAGE_GENERATION_ERROR",
        "VALIDATION_ERROR",
        "TTS_ERROR",
        "TRANSPORT_ERROR",
        "AGENT_TIMEOUT",
        "BEHAVIOR_TIMEOUT",
    }
    assert {member.value for member in FailureReason} == expected


def test_ended_by_canonical_values() -> None:
    expected = {"scenario", "caller", "agent", "timeout", "transport", "error"}
    assert {member.value for member in EndedBy} == expected


def test_evaluator_verdict_uppercase_values() -> None:
    expected = {"SATISFIED", "PARTIAL", "NOT_SATISFIED"}
    assert {member.value for member in EvaluatorVerdict} == expected


# ---------------------------------------------------------------------------
# JSON Schema export (for lksr / Rust consumption without importing Python)
# ---------------------------------------------------------------------------


def test_contract_json_schema_has_required_fields() -> None:
    schema = contract_json_schema()
    assert schema["title"] == "CallerContract"
    props = schema["properties"]
    assert "behavior" in props
    assert "candidate" in props
    identity_def = schema["definitions"]["identity"]
    assert set(identity_def["required"]) == {"behavior_id", "turn_id", "generation_id"}


# ---------------------------------------------------------------------------
# Golden JSON fixtures (shared with Rust parity suite, P0-8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", ["negotiate_price.json", "ask_price.json"])
def test_golden_fixture_round_trips_into_contract_and_candidate(fixture_name: str) -> None:
    data = json.loads((FIXTURES_DIR / fixture_name).read_text(encoding="utf-8"))

    constraints = ContractConstraints(**data["constraints"])
    contract = BehaviorContract(behavior=data["behavior"], target=data["target"], constraints=constraints)
    contract.validate()

    cand = data["candidate"]
    identity = GenerationIdentity(**cand["identity"])
    candidate = CandidateUtterance(
        act=cand["act"],
        target=cand["target"],
        slots=cand["slots"],
        utterance=cand["utterance"],
        identity=identity,
    )
    candidate.validate()

    assert candidate.act == data["behavior"] or candidate.act in {"ask", "negotiate"}


def test_golden_fixtures_directory_is_not_empty() -> None:
    fixtures = list(FIXTURES_DIR.glob("*.json"))
    assert len(fixtures) >= 2, "expected at least the negotiate_price and ask_price golden fixtures"


def test_to_dict_serializes_dataclasses_and_enums() -> None:
    ident = GenerationIdentity(behavior_id="b1", turn_id=0, generation_id=0)
    result = ValidationResult(verdict=Verdict.VALID)
    payload = to_dict({"identity": ident, "result": result})
    assert payload["identity"]["behavior_id"] == "b1"
    assert payload["result"]["verdict"] == "VALID"
