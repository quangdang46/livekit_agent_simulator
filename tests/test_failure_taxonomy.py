"""Failure taxonomy tests: all seven canonical codes, the
VALIDATION_PASS/TTS_FAILED split state, and snapshot-style report JSON for
each code. See caller_contract/failures.py module docstring for the
deliberate scope decision on the legacy `ended_by` assertion type.
"""

from __future__ import annotations

import pytest

from livekit_agent_simulator.caller_contract import EndedBy, FailureReason, Verdict
from livekit_agent_simulator.caller_contract.failures import (
    RunFailure,
    TTSPublishState,
    failure_from_agent_timeout,
    failure_from_behavior_timeout,
    failure_from_language_generation_error,
    failure_from_transport,
    failure_from_tts_state,
    failure_from_validation_result,
    record_tts_attempt,
    to_ended_by_report_dict,
)


def test_all_seven_canonical_codes_exist() -> None:
    assert {member.value for member in FailureReason} == {
        "CALLER_BEHAVIOR_VIOLATION",
        "LANGUAGE_GENERATION_ERROR",
        "VALIDATION_ERROR",
        "TTS_ERROR",
        "TRANSPORT_ERROR",
        "AGENT_TIMEOUT",
        "BEHAVIOR_TIMEOUT",
    }


@pytest.mark.parametrize(
    "make_failure,expected_reason",
    [
        (lambda: failure_from_validation_result(Verdict.INVALID, retries_exhausted=True), FailureReason.CALLER_BEHAVIOR_VIOLATION),
        (lambda: failure_from_language_generation_error("timeout"), FailureReason.LANGUAGE_GENERATION_ERROR),
        (lambda: failure_from_validation_result(Verdict.ERROR, retries_exhausted=False), FailureReason.VALIDATION_ERROR),
        (lambda: failure_from_tts_state(TTSPublishState(validation_passed=True, tts_failed=True)), FailureReason.TTS_ERROR),
        (lambda: failure_from_transport("room disconnected"), FailureReason.TRANSPORT_ERROR),
        (lambda: failure_from_agent_timeout(10_000), FailureReason.AGENT_TIMEOUT),
        (lambda: failure_from_behavior_timeout(max_turns=3), FailureReason.BEHAVIOR_TIMEOUT),
    ],
)
def test_each_failure_helper_maps_to_correct_reason(make_failure, expected_reason: FailureReason) -> None:
    failure = make_failure()
    assert isinstance(failure, RunFailure)
    assert failure.reason == expected_reason


def test_validation_result_returns_none_while_retries_remain() -> None:
    """Not yet a terminal failure — the caller should retry, not report a
    CALLER_BEHAVIOR_VIOLATION prematurely."""
    assert failure_from_validation_result(Verdict.INVALID, retries_exhausted=False) is None


def test_tts_success_returns_no_failure() -> None:
    state = record_tts_attempt(validation_passed=True, tts_succeeded=True)
    assert failure_from_tts_state(state) is None


# ---------------------------------------------------------------------------
# VALIDATION_PASS / TTS_FAILED split state (report §28.4(6))
# ---------------------------------------------------------------------------


def test_validation_pass_but_tts_fail_is_not_behavior_executed() -> None:
    state = record_tts_attempt(validation_passed=True, tts_succeeded=False)
    assert state.validation_passed is True
    assert state.tts_failed is True
    failure = failure_from_tts_state(state)
    assert failure is not None
    assert failure.reason == FailureReason.TTS_ERROR


def test_tts_retry_policy_never_reinvokes_ai() -> None:
    """should_retry_tts_only() must be True exactly when the fix is
    'retry TTS with the same candidate', never 'call the AI again'."""
    state = record_tts_attempt(validation_passed=True, tts_succeeded=False)
    assert state.should_retry_tts_only() is True


def test_validation_failed_never_reaches_tts_retry_state() -> None:
    """If validation itself did not pass, TTS was never attempted — the
    split state must reflect that distinctly from a TTS-layer failure."""
    state = record_tts_attempt(validation_passed=False, tts_succeeded=False)
    assert state.tts_failed is False, "no TTS attempt was made, so this is not a TTS_ERROR case"
    assert state.should_retry_tts_only() is False


# ---------------------------------------------------------------------------
# Snapshot-style report JSON for each code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", list(FailureReason))
def test_run_failure_report_json_snapshot_per_code(reason: FailureReason) -> None:
    failure = RunFailure(reason=reason, detail="example detail", metadata={"step": 3})
    payload = failure.to_report_dict()
    assert payload == {"reason": reason.value, "detail": "example detail", "step": 3}


def test_tts_publish_state_report_json_snapshot() -> None:
    state = TTSPublishState(validation_passed=True, tts_failed=True)
    assert state.to_report_dict() == {"validation_passed": True, "tts_failed": True}


def test_ended_by_report_json_snapshot() -> None:
    for member in EndedBy:
        assert to_ended_by_report_dict(member) == {"ended_by": member.value}


# ---------------------------------------------------------------------------
# Existing ended_by assertions still pass after this change (scope check)
# ---------------------------------------------------------------------------


def test_existing_ended_by_assertion_type_untouched() -> None:
    """Deliberate scope decision (see failures.py module docstring): the
    legacy asserts.py `ended_by` type ("sim"|"agent"|"detect") is a
    different, already-shipped feature and is not migrated here. This test
    documents that decision is intentional, not an oversight."""
    from livekit_agent_simulator.asserts import OutcomeExpect

    oc = OutcomeExpect(id="e1", type="ended_by", ended_by="agent")
    assert oc.ended_by == "agent"
