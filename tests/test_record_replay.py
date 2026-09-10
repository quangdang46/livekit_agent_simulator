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


@pytest.mark.asyncio
async def test_driver_records_every_attempt_and_replays_without_ai(tmp_path):
    """Driver-level record->replay: reject + pass both recorded; replay
    makes zero backend calls and reaches the same outcome."""
    from livekit_agent_simulator.caller_contract.driver import ContractCallerDriver
    from livekit_agent_simulator.caller_contract.dsl import parse_steps
    from livekit_agent_simulator.caller_contract.language_adapter import AILanguageAdapter
    from livekit_agent_simulator.caller_contract.orchestrator import Orchestrator
    from livekit_agent_simulator.caller_contract.semantic import RuleBasedSemanticVerifier
    from livekit_agent_simulator.caller_contract.validator import ContractValidator

    from livekit_agent_simulator.caller_contract.record_replay import (
        Recorder,
        ReplayLanguageBackend,
        RunRecord,
    )

    class _LiveBackend:
        def __init__(self):
            self.calls = 0

        def generate(self, context):
            self.calls += 1
            behavior = context["current_behavior"]
            if self.calls == 1:
                return {
                    "act": "negotiate",
                    "target": "price",
                    "slots": {},
                    "utterance": "Do you offer financing options?",
                }
            return {
                "act": behavior["act"],
                "target": behavior["target"],
                "slots": {},
                "utterance": "Would you come down to $30,000?",
            }

    class _Sink:
        def __init__(self, orch):
            self.orch = orch

        async def publish(self, pcm, identity, *, label, gain=1.0):
            return True

    class _Agent:
        async def wait_agent_turn(self, *, timeout_s: float = 30.0):
            return "Sure, I can do $30,000."

        def is_agent_speaking_now(self) -> bool:
            return False

    def _make(backend):
        orch = Orchestrator()
        return (
            ContractCallerDriver(
                orchestrator=orch,
                validator=ContractValidator(
                    semantic_verifier=RuleBasedSemanticVerifier()
                ),
                adapter=AILanguageAdapter(backend=backend),
                synthesize=lambda text: b"\x00\x01" * 10,
            ),
            orch,
        )

    actions = parse_steps(
        [
            {
                "do": {
                    "behavior": "negotiate",
                    "target": "price",
                    "constraints": {
                        "max_turns": 2,
                        "forbidden_intents": ["financing"],
                    },
                }
            }
        ],
        file="t",
    )
    live = _LiveBackend()
    driver, orch = _make(live)
    recorder = Recorder(scenario_id="s", seed=1)
    driver.recorder = recorder
    result = await driver.run(actions, _Sink(orch), _Agent())
    assert result.failure is None
    assert live.calls == 2  # reject + pass
    record = recorder.finalize()
    assert [a.verdict for a in record.attempts] == ["INVALID", "VALID"]

    path = tmp_path / "run.json"
    record.write(path)
    replay_backend = ReplayLanguageBackend(RunRecord.read(path))
    driver2, orch2 = _make(replay_backend)
    result2 = await driver2.run(actions, _Sink(orch2), _Agent())
    assert result2.failure is None
    assert live.calls == 2  # replay made zero AI calls


@pytest.mark.asyncio
async def test_replay_verdict_divergence_fails_loudly(tmp_path):
    """A replayed candidate that validates differently than recorded raises
    ReplayMismatchError (never silently diverges)."""
    from livekit_agent_simulator.caller_contract import Verdict
    from livekit_agent_simulator.caller_contract.language_adapter import AILanguageAdapter
    from livekit_agent_simulator.caller_contract.semantic import RuleBasedSemanticVerifier
    from livekit_agent_simulator.caller_contract.validator import ContractValidator

    from livekit_agent_simulator.caller_contract.record_replay import (
        ReplayLanguageBackend,
        ReplayMismatchError,
        RunRecord,
    )

    record = RunRecord(
        scenario_id="s",
        seed=1,
        attempts=[
            __import__("livekit_agent_simulator.caller_contract.record_replay", fromlist=["RecordedAttempt"]).RecordedAttempt(
                candidate={
                    "act": "ask",
                    "target": None,
                    "slots": {},
                    "utterance": "Could you tell me more?",
                    "identity": {
                        "behavior_id": "b1",
                        "turn_id": 0,
                        "generation_id": 1,
                        "context_version": 0,
                    },
                },
                verdict="VALID",
                reason=None,
                retry_index=0,
            )
        ],
    )
    backend = ReplayLanguageBackend(record)
    candidate_dict = backend.generate({})
    # Validate against a DIFFERENT contract so the verdict diverges.
    from livekit_agent_simulator.caller_contract import BehaviorContract
    from livekit_agent_simulator.caller_contract.language_adapter import (
        _parse_backend_response,
    )
    from livekit_agent_simulator.caller_contract import GenerationIdentity

    candidate = _parse_backend_response(
        candidate_dict, GenerationIdentity(behavior_id="b", turn_id=0, generation_id=0)
    )
    validator = ContractValidator(semantic_verifier=RuleBasedSemanticVerifier())
    result = validator.validate(candidate, BehaviorContract(behavior="negotiate"))
    assert result.verdict != Verdict.VALID
    with pytest.raises(ReplayMismatchError):
        backend.assert_verdict(result.verdict)
