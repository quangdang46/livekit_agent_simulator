"""P0-7: Record and Replay tests.

End-to-end: records a mocked run with at least one rejection then replays
it to the identical failure without network. Passing-run replay passes.
Record files validate against the versioned record_format schema.
"""

from __future__ import annotations

import json

import pytest

from livekit_agent_simulator.caller_contract import (
    BehaviorContract,
    ContractConstraints,
    GenerationIdentity,
    Verdict,
)
from livekit_agent_simulator.caller_contract.language_adapter import AILanguageAdapter
from livekit_agent_simulator.caller_contract.record_replay import (
    RECORD_FORMAT_VERSION,
    Recorder,
    ReplayLanguageBackend,
    ReplayMismatchError,
    RunRecord,
    rebuild_identity_from_candidate,
)
from livekit_agent_simulator.caller_contract.validator import ContractValidator


def _contract() -> BehaviorContract:
    return BehaviorContract(
        behavior="negotiate",
        target="price",
        constraints=ContractConstraints(max_turns=3, max_budget=30000, forbidden_intents=["financing"]),
    )


def _identity(turn: int, gen: int) -> GenerationIdentity:
    return GenerationIdentity(behavior_id="b2", turn_id=turn, generation_id=gen)


# ---------------------------------------------------------------------------
# Recording: including at least one rejection, not just the final pass
# ---------------------------------------------------------------------------


def test_recorder_captures_rejection_and_final_pass() -> None:
    validator = ContractValidator()
    contract = _contract()
    recorder = Recorder(scenario_id="used-car-negotiate", seed=42)

    from livekit_agent_simulator.caller_contract import CandidateUtterance

    rejected = CandidateUtterance(
        act="ask", target="financing", slots={}, utterance="Do you offer financing?", identity=_identity(1, 1)
    )
    result1 = validator.validate(rejected, contract)
    recorder.record_attempt(rejected, result1, retry_index=0)
    assert result1.verdict != Verdict.VALID

    accepted = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={"max_budget": 30000},
        utterance="Could you do $30,000?",
        identity=_identity(1, 2),
    )
    result2 = validator.validate(accepted, contract)
    recorder.record_attempt(accepted, result2, retry_index=1)
    assert result2.is_valid()

    record = recorder.finalize()
    assert len(record.attempts) == 2
    assert record.attempts[0].verdict != Verdict.VALID.value
    assert record.attempts[1].verdict == Verdict.VALID.value


# ---------------------------------------------------------------------------
# Versioned schema round-trip
# ---------------------------------------------------------------------------


def test_run_record_round_trips_through_json(tmp_path) -> None:
    record = RunRecord(
        scenario_id="used-car-negotiate",
        seed=42,
        attempts=[],
    )
    path = tmp_path / "run.json"
    record.write(path)

    loaded = RunRecord.read(path)
    assert loaded.scenario_id == record.scenario_id
    assert loaded.seed == record.seed
    assert loaded.format_version == RECORD_FORMAT_VERSION


def test_run_record_file_validates_against_versioned_schema(tmp_path) -> None:
    record = RunRecord(scenario_id="s1", seed=1)
    path = tmp_path / "run.json"
    record.write(path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["format_version"] == RECORD_FORMAT_VERSION
    assert set(raw.keys()) == {"format_version", "scenario_id", "seed", "attempts"}


def test_run_record_rejects_unsupported_format_version() -> None:
    with pytest.raises(ValueError, match="unsupported record_format version"):
        RunRecord.from_dict({"format_version": 999, "scenario_id": "x", "seed": 1, "attempts": []})


# ---------------------------------------------------------------------------
# End-to-end: record a failing (rejected) turn, replay it, get the same
# failure without any network call.
# ---------------------------------------------------------------------------


def test_replay_reproduces_identical_rejection_without_network() -> None:
    validator = ContractValidator()
    contract = _contract()
    recorder = Recorder(scenario_id="used-car-negotiate", seed=7)

    from livekit_agent_simulator.caller_contract import CandidateUtterance

    off_topic = CandidateUtterance(
        act="ask", target="financing", slots={}, utterance="Do you offer financing?", identity=_identity(1, 1)
    )
    original_result = validator.validate(off_topic, contract)
    recorder.record_attempt(off_topic, original_result, retry_index=0)
    assert not original_result.is_valid()

    record = recorder.finalize()

    # --- Replay phase: no real backend, no network ---
    backend = ReplayLanguageBackend(record)
    adapter = AILanguageAdapter(backend=backend, max_retries=0)

    identity = rebuild_identity_from_candidate(record.attempts[0].candidate)
    replayed_candidate = adapter.generate_candidate(contract, context={}, identity=identity)
    replayed_result = validator.validate(replayed_candidate, contract)

    backend.assert_verdict(replayed_result.verdict)  # must not raise: identical failure reproduced
    assert replayed_result.verdict == original_result.verdict
    assert replayed_result.reason == original_result.reason
    assert not replayed_result.is_valid()


def test_replay_of_passing_run_passes() -> None:
    validator = ContractValidator()
    contract = _contract()
    recorder = Recorder(scenario_id="used-car-negotiate", seed=7)

    from livekit_agent_simulator.caller_contract import CandidateUtterance

    accepted = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={"max_budget": 30000},
        utterance="Could you do $30,000?",
        identity=_identity(1, 1),
    )
    original_result = validator.validate(accepted, contract)
    recorder.record_attempt(accepted, original_result, retry_index=0)
    assert original_result.is_valid()

    record = recorder.finalize()
    backend = ReplayLanguageBackend(record)
    adapter = AILanguageAdapter(backend=backend, max_retries=0)

    identity = rebuild_identity_from_candidate(record.attempts[0].candidate)
    replayed_candidate = adapter.generate_candidate(contract, context={}, identity=identity)
    replayed_result = validator.validate(replayed_candidate, contract)

    backend.assert_verdict(replayed_result.verdict)
    assert replayed_result.is_valid()


def test_replay_mismatch_fails_loudly_not_silently() -> None:
    """If a validator change (or a bug) causes the replayed verdict to
    diverge from the recorded one, this must raise, never silently pass a
    different result through (report §28.5(22))."""
    record = RunRecord(
        scenario_id="s1",
        seed=1,
        attempts=[],
    )
    from livekit_agent_simulator.caller_contract.record_replay import RecordedAttempt

    record.attempts.append(
        RecordedAttempt(
            candidate={
                "act": "negotiate",
                "target": "price",
                "slots": {},
                "utterance": "Could you do $30,000?",
                "identity": {"behavior_id": "b2", "turn_id": 1, "generation_id": 1, "context_version": 0},
            },
            verdict="VALID",
            reason=None,
            retry_index=0,
        )
    )
    backend = ReplayLanguageBackend(record)
    backend.generate({})  # populate _last_attempt

    with pytest.raises(ReplayMismatchError, match="replay verdict mismatch"):
        backend.assert_verdict(Verdict.INVALID)


def test_replay_exhausted_raises_clear_error() -> None:
    record = RunRecord(scenario_id="s1", seed=1, attempts=[])
    backend = ReplayLanguageBackend(record)
    with pytest.raises(RuntimeError, match="replay exhausted"):
        backend.generate({})
