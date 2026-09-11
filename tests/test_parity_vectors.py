"""P0-8: Python side of the Python-Rust behavioral parity suite.

Golden vectors under tests/fixtures/parity/ are language-neutral JSON —
this file replays them through the REAL Python caller_contract
implementation and asserts the expected verdict/result. The Rust side
(src/livekit_agent_simulator_rust/crates/lks-core/src/caller_contract.rs)
reads the SAME directory and must produce the SAME verdicts for the SAME
inputs — see that module's `#[cfg(test)] mod parity_tests`.

Any change to a vector file requires updating both sides in the same
commit (see bead notes / module docstrings).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from livekit_agent_simulator.caller_contract import (
    BehaviorContract,
    CandidateUtterance,
    ContractConstraints,
    EndedBy,
    FailureReason,
    GenerationIdentity,
    is_current,
)
from livekit_agent_simulator.caller_contract.orchestrator import TurnDetector
from livekit_agent_simulator.caller_contract.semantic import RuleBasedSemanticVerifier
from livekit_agent_simulator.caller_contract.validator import ContractValidator

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "parity"

_VALIDATOR_VECTOR_FILES = [
    "validator_valid_pass.json",
    "validator_act_mismatch.json",
    "validator_target_mismatch.json",
    "validator_slot_violation.json",
    "validator_utterance_too_long.json",
    "validator_end_call_not_allowed.json",
    "validator_forbidden_intent_lexical.json",
    "validator_forbidden_intent_nested_semantic.json",
    "validator_ambiguous_low_confidence.json",
]

_REQUIRED_VALIDATOR_VECTOR_KEYS = {
    "id",
    "use_semantic_verifier",
    "contract",
    "candidate",
    "expected_verdict",
    "expected_reason",
}
_REQUIRED_CONTRACT_KEYS = {"behavior", "target", "constraints"}
_REQUIRED_CONSTRAINTS_KEYS = {
    "max_turns",
    "max_budget",
    "max_words",
    "max_duration_s",
    "forbidden_intents",
    "must_not",
}
_REQUIRED_CANDIDATE_KEYS = {"act", "target", "slots", "utterance", "identity"}
_REQUIRED_IDENTITY_KEYS = {"behavior_id", "turn_id", "generation_id", "context_version"}


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Structural validation ("validates against its own JSON schema" — a
# hand-rolled structural check equivalent to schema validation, since this
# fixture set is small and language-neutral rather than a general-purpose
# schema library consumer)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename", _VALIDATOR_VECTOR_FILES)
def test_validator_vector_matches_required_schema_shape(filename: str) -> None:
    data = _load(filename)
    assert _REQUIRED_VALIDATOR_VECTOR_KEYS <= set(data.keys())
    assert _REQUIRED_CONTRACT_KEYS <= set(data["contract"].keys())
    assert _REQUIRED_CONSTRAINTS_KEYS <= set(data["contract"]["constraints"].keys())
    assert _REQUIRED_CANDIDATE_KEYS <= set(data["candidate"].keys())
    assert _REQUIRED_IDENTITY_KEYS <= set(data["candidate"]["identity"].keys())
    assert data["expected_verdict"] in {"VALID", "INVALID", "UNKNOWN", "ERROR"}


def test_turn_debounce_vector_matches_required_shape() -> None:
    data = _load("turn_debounce.json")
    assert "cases" in data and len(data["cases"]) > 0
    for case in data["cases"]:
        assert {"silence_debounce_ms", "last_stop_ms", "now_ms", "expected_turn_complete"} <= set(case.keys())


def test_staleness_vector_matches_required_shape() -> None:
    data = _load("staleness_drop.json")
    assert "cases" in data and len(data["cases"]) > 0
    for case in data["cases"]:
        assert {"identity", "current", "expected_is_current"} <= set(case.keys())
        assert _REQUIRED_IDENTITY_KEYS <= set(case["identity"].keys())


def test_failure_codes_vector_matches_required_shape() -> None:
    data = _load("failure_codes.json")
    assert "expected_failure_reasons" in data
    assert "expected_ended_by" in data
    assert len(data["expected_failure_reasons"]) == 7
    assert len(data["expected_ended_by"]) == 6


# ---------------------------------------------------------------------------
# Replay through the real Python implementation
# ---------------------------------------------------------------------------


def _build_contract(raw: dict[str, Any]) -> BehaviorContract:
    constraints = ContractConstraints(**raw["constraints"])
    return BehaviorContract(behavior=raw["behavior"], target=raw["target"], constraints=constraints)


def _build_candidate(raw: dict[str, Any]) -> CandidateUtterance:
    identity = GenerationIdentity(**raw["identity"])
    return CandidateUtterance(
        act=raw["act"], target=raw["target"], slots=raw["slots"], utterance=raw["utterance"], identity=identity
    )


@pytest.mark.parametrize("filename", _VALIDATOR_VECTOR_FILES)
def test_validator_vector_produces_expected_verdict_in_python(filename: str) -> None:
    data = _load(filename)
    contract = _build_contract(data["contract"])
    candidate = _build_candidate(data["candidate"])
    semantic_verifier = RuleBasedSemanticVerifier() if data["use_semantic_verifier"] else None
    validator = ContractValidator(semantic_verifier=semantic_verifier)

    result = validator.validate(candidate, contract)

    assert result.verdict.value == data["expected_verdict"], (
        f"{data['id']}: expected {data['expected_verdict']}, got {result.verdict.value} ({result.details})"
    )
    if data["expected_reason"] is not None:
        assert result.reason == data["expected_reason"], f"{data['id']}: reason mismatch: {result.reason!r}"


def test_turn_debounce_vector_matches_python_formula() -> None:
    data = _load("turn_debounce.json")
    for case in data["cases"]:
        detector = TurnDetector(silence_debounce_ms=case["silence_debounce_ms"])
        detector.on_agent_audio_started(now_ms=0)
        detector.on_agent_audio_stopped(now_ms=case["last_stop_ms"])
        from livekit_agent_simulator.caller_contract.orchestrator import TurnState

        state = detector.poll(now_ms=case["now_ms"])
        is_complete = state == TurnState.AGENT_TURN_COMPLETE
        assert is_complete == case["expected_turn_complete"], case


def test_staleness_vector_matches_python_is_current() -> None:
    data = _load("staleness_drop.json")
    for case in data["cases"]:
        identity = GenerationIdentity(**case["identity"])
        current = GenerationIdentity(**case["current"])
        assert is_current(identity, current) == case["expected_is_current"], case["name"]


def test_failure_codes_vector_matches_python_enums() -> None:
    data = _load("failure_codes.json")
    assert {m.value for m in FailureReason} == set(data["expected_failure_reasons"])
    assert {m.value for m in EndedBy} == set(data["expected_ended_by"])


def test_should_interrupt_vector_matches_required_shape() -> None:
    data = _load("should_interrupt.json")
    assert data["none_interaction_expected"] is False
    assert data["no_rate_expected"] is False
    assert len(data["cases"]) > 0
    for case in data["cases"]:
        assert {"scenario_id", "seed", "turn", "rate", "expected"} <= set(case.keys())
        assert case["rate"] in {"low", "medium", "high"}


def test_should_interrupt_vector_matches_python_policy() -> None:
    from livekit_agent_simulator.caller_contract.dsl import InteractionConfig
    from livekit_agent_simulator.caller_contract.interaction_planner import CallerInteractionPlanner

    data = _load("should_interrupt.json")
    planner = CallerInteractionPlanner()
    assert planner.should_interrupt(None, scenario_id="s", agent_turn_index=0) is (
        data["none_interaction_expected"]
    )
    assert planner.should_interrupt(
        InteractionConfig(), scenario_id="s", agent_turn_index=0
    ) is (data["no_rate_expected"])
    for case in data["cases"]:
        ic = InteractionConfig(
            interruption_rate=case["rate"], interruption_seed=case["seed"]
        )
        assert planner.should_interrupt(
            ic, scenario_id=case["scenario_id"], agent_turn_index=case["turn"]
        ) is case["expected"], case
