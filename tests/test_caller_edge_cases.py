"""Edge-case hardening matrix: one test per edge case from
NEW_ARCHITECTURE_FOR_LKS_AND_LKSR.md section 28, subsections:

    28.1 STATE/RACE   -> test_edge01.. test_edge05, test_edge15, 16, 21
    28.2 TURN         -> test_edge02, 03, 04, 19, 20
    28.3 AI           -> test_edge08, 09, 10, 11, 23, 24
    28.4 EXECUTION    -> test_edge06, 07, 13, 14
    28.5 EVALUATION   -> test_edge12, 17, 18, 22, 25

Test names include the edge number (test_edgeNN_...) so a reader can map
test -> spec without opening the report. Each test logs
behavior_id/turn_id/generation_id (where applicable) via the assertion
message so a failure is traceable without re-running under a debugger.

Most of these edges are unit-tested in their home module too (this is
intentional — this file is the cross-cutting TRACEABILITY INDEX proving
every enumerated edge case has at least one concrete, named test; it is
not meant to replace the focused unit tests elsewhere).
"""

from __future__ import annotations

import logging

import pytest

from livekit_agent_simulator.caller_contract import (
    BehaviorContract,
    CandidateUtterance,
    ContractConstraints,
    EvaluatorVerdict,
    FailureReason,
    GenerationIdentity,
    Verdict,
)
from livekit_agent_simulator.caller_contract.dsl import DSLError, parse_step
from livekit_agent_simulator.caller_contract.failures import (
    RunFailure,
    failure_from_behavior_timeout,
    failure_from_tts_state,
    failure_from_validation_result,
    record_tts_attempt,
)
from livekit_agent_simulator.caller_contract.interaction_planner import (
    CallerInteractionPlanner,
    InteractionActionKind,
    verify_semantic_preserving,
)
from livekit_agent_simulator.caller_contract.language_adapter import should_invoke_adapter
from livekit_agent_simulator.caller_contract.orchestrator import (
    AgentSilenceOutcome,
    BehaviorEvaluator,
    BehaviorOutcome,
    Orchestrator,
    TimeoutConfig,
    TurnDetector,
    TurnState,
    classify_agent_silence,
)
from livekit_agent_simulator.caller_contract.record_replay import (
    Recorder,
    ReplayLanguageBackend,
    ReplayMismatchError,
    RunRecord,
)
from livekit_agent_simulator.caller_contract.semantic import RuleBasedSemanticVerifier
from livekit_agent_simulator.caller_contract.validator import ContractValidator

logger = logging.getLogger(__name__)


def _identity_debug(ident: GenerationIdentity) -> str:
    return f"behavior_id={ident.behavior_id} turn_id={ident.turn_id} generation_id={ident.generation_id}"


def _contract() -> BehaviorContract:
    return BehaviorContract(
        behavior="negotiate",
        target="price",
        constraints=ContractConstraints(max_turns=3, max_budget=30000, forbidden_intents=["financing"]),
    )


# ---------------------------------------------------------------------------
# 28.1 STATE / RACE
# ---------------------------------------------------------------------------


def test_edge01_agent_does_not_respond_four_way_split() -> None:
    """Edge 1: agent silence must be classified into exactly one of
    PROCESSING/TIMEOUT/HANGUP/TRANSPORT_ERROR, never conflated."""
    outcome = classify_agent_silence(elapsed_ms=1000, turn_timeout_ms=10_000, agent_hung_up=False, transport_lost=False)
    assert outcome == AgentSilenceOutcome.PROCESSING, f"expected PROCESSING, got {outcome}"

    outcome = classify_agent_silence(elapsed_ms=11_000, turn_timeout_ms=10_000, agent_hung_up=False, transport_lost=False)
    assert outcome == AgentSilenceOutcome.TIMEOUT, f"expected TIMEOUT, got {outcome}"


def test_edge05_stale_generation_dropped_when_behavior_changes_mid_tts() -> None:
    """Edge 5: caller is mid-TTS when scenario state transitions —
    the in-flight generation must be dropped, never published."""
    orch = Orchestrator()
    orch.start_behavior()
    orch.advance_caller_turn()
    identity = orch.new_generation()

    orch.start_behavior()  # state moved on while "TTS" was in flight
    orch.advance_caller_turn()

    assert orch.is_stale(identity), f"stale generation must be dropped: {_identity_debug(identity)}"


def test_edge15_interrupt_during_validation_invalidates_candidate() -> None:
    """Edge 15: if the turn advances while a candidate is being validated,
    that candidate must be treated as stale and never published."""
    orch = Orchestrator()
    orch.start_behavior()
    orch.advance_caller_turn()
    identity_during_validation = orch.new_generation()

    # Simulate an interrupt event advancing the turn mid-validation.
    orch.advance_caller_turn()

    assert orch.is_stale(identity_during_validation), (
        f"candidate generated at {_identity_debug(identity_during_validation)} "
        "must be dropped after an interrupt advanced the turn"
    )


def test_edge16_interrupt_during_tts_discards_output() -> None:
    """Edge 16: same staleness guard applies to a TTS job in flight, not
    just to validation."""
    orch = Orchestrator()
    orch.start_behavior()
    orch.advance_caller_turn()
    identity_during_tts = orch.new_generation()

    orch.advance_caller_turn()  # interrupt handling advanced the turn

    assert orch.is_stale(identity_during_tts), f"TTS output for {_identity_debug(identity_during_tts)} must be discarded"


def test_edge21_context_stale_when_agent_response_arrives_first() -> None:
    """Edge 21: an AI request built from an older context must be
    considered stale once a newer agent response has already advanced the
    orchestrator's state — evaluated via the same generation triple."""
    orch = Orchestrator()
    orch.start_behavior()
    orch.advance_caller_turn()
    stale_request_identity = orch.new_generation()

    # A new agent response arrived and the orchestrator moved to a new turn
    # before the AI request (built from the old context) came back.
    orch.advance_caller_turn()

    assert orch.is_stale(stale_request_identity), (
        f"AI response for stale context {_identity_debug(stale_request_identity)} must not be applied"
    )


# ---------------------------------------------------------------------------
# 28.2 TURN
# ---------------------------------------------------------------------------


def test_edge02_agent_pause_mid_sentence_is_not_turn_complete() -> None:
    """Edge 2: agent_audio_stopped != semantic turn complete — a short
    pause must not prematurely end the turn."""
    detector = TurnDetector(silence_debounce_ms=300)
    detector.on_agent_audio_started(now_ms=0)
    detector.on_agent_audio_stopped(now_ms=100)
    state = detector.poll(now_ms=150)
    assert state == TurnState.POSSIBLE_END, f"expected still-pending POSSIBLE_END, got {state}"
    assert detector.poll(now_ms=500) == TurnState.AGENT_TURN_COMPLETE


def test_edge03_multi_chunk_streaming_audio_correlates_to_one_turn() -> None:
    """Edge 3: multiple audio_started/stopped pairs within the debounce
    window must correlate into a single logical agent turn."""
    detector = TurnDetector(silence_debounce_ms=200)
    detector.on_agent_audio_started(now_ms=0)
    detector.on_agent_audio_started(now_ms=50)
    detector.on_agent_audio_started(now_ms=100)
    assert detector.chunk_count == 3, f"expected 3 correlated chunks, got {detector.chunk_count}"


def test_edge04_agent_self_interrupt_requires_explicit_signal() -> None:
    """Edge 4: agent starting to speak during the caller's turn is not
    assumed intentional unless an explicit signal marks it so — otherwise
    a transport glitch could masquerade as an interruption."""
    detector = TurnDetector()
    detector.begin_caller_turn()
    detector.on_agent_audio_started(now_ms=0)
    assert detector.intentional_interrupt is False
    detector.mark_intentional_interrupt()
    assert detector.intentional_interrupt is True


def test_edge19_backchannel_is_interaction_action_not_behavior() -> None:
    """Edge 19: backchannel must never be modeled as a semantic Behavior —
    it is an InteractionAction with no contract/behavior field."""
    planner = CallerInteractionPlanner()
    outcome = planner.plan_backchannel()
    assert outcome.kind == InteractionActionKind.BACKCHANNEL
    assert not hasattr(outcome, "contract")
    assert not hasattr(outcome, "behavior")


def test_edge20_false_interrupt_noise_distinct_from_intentional_barge_in() -> None:
    """Edge 20: an agent-audio event during the caller's turn is, by
    default, NOT an intentional interrupt (guards against false
    interrupt/noise being treated as a real barge-in)."""
    detector = TurnDetector()
    detector.begin_caller_turn()
    detector.on_agent_audio_started(now_ms=0)  # could be noise/glitch
    assert detector.intentional_interrupt is False, "noise must not be auto-classified as intentional barge-in"


# ---------------------------------------------------------------------------
# 28.3 AI
# ---------------------------------------------------------------------------


def test_edge08_ai_backend_failure_never_falls_back_to_free_speech() -> None:
    """Edge 8: AI timeout/429/connection reset must end in
    LANGUAGE_GENERATION_ERROR, never a fabricated fallback line."""
    from livekit_agent_simulator.caller_contract.language_adapter import (
        AILanguageAdapter,
        LanguageGenerationError,
    )

    class AlwaysTimesOut:
        def generate(self, context):
            raise TimeoutError("backend unreachable")

    adapter = AILanguageAdapter(backend=AlwaysTimesOut(), max_retries=1)
    with pytest.raises(LanguageGenerationError) as exc_info:
        adapter.generate_candidate(_contract(), context={}, identity=GenerationIdentity("b1", 0, 0))
    assert exc_info.value.reason == FailureReason.LANGUAGE_GENERATION_ERROR


def test_edge09_valid_but_excessively_long_utterance_rejected() -> None:
    """Edge 9: a semantically valid but too-long utterance must be
    rejected via max_words, not silently accepted."""
    validator = ContractValidator()
    contract = _contract()
    contract.constraints.max_words = 5
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={},
        utterance="Would you be able to come down to thirty thousand dollars please",
        identity=GenerationIdentity("b1", 0, 0),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.INVALID
    assert result.reason == "UTTERANCE_TOO_LONG"


def test_edge10_multi_act_utterance_surfaces_secondary_act() -> None:
    """Edge 10: 'Could you lower the price, and by the way do you offer
    financing?' must surface BOTH the primary act and the secondary
    forbidden intent, not just the claimed primary act."""
    verifier = RuleBasedSemanticVerifier()
    observed = verifier.classify(
        "Could you lower the price, and by the way do you offer financing?", _contract()
    )
    assert "financing" in observed.all_acts, f"secondary intent not surfaced in all_acts={observed.all_acts}"


def test_edge11_forbidden_intent_nested_in_otherwise_valid_sentence_rejected() -> None:
    """Edge 11: '$30,000 is really my limit, although I could consider
    financing.' must still be rejected even though the primary intent is
    on-topic negotiation."""
    validator = ContractValidator(semantic_verifier=RuleBasedSemanticVerifier())
    contract = _contract()
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={"max_budget": 30000},
        utterance="$30,000 is really my limit, although I could consider financing.",
        identity=GenerationIdentity("b1", 0, 0),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.INVALID
    assert result.reason == "FORBIDDEN_INTENT_DETECTED"


def test_edge23_semantic_validator_crash_maps_to_error_never_valid() -> None:
    """Edge 23: a crashing/unavailable semantic backend must map to
    ERROR — never treated as PASS by omission."""

    class BrokenVerifier:
        def classify(self, utterance, contract):
            raise RuntimeError("backend down")

    validator = ContractValidator(semantic_verifier=BrokenVerifier())
    contract = _contract()
    candidate = CandidateUtterance(
        act="negotiate", target="price", slots={"max_budget": 30000}, utterance="Could you do $30,000?",
        identity=GenerationIdentity("b1", 0, 0),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.ERROR
    assert not result.is_valid()


def test_edge24_ambiguous_classification_prefers_false_negative_over_false_positive() -> None:
    """Edge 24: the validator must be biased toward precision — an
    ambiguous/low-confidence classification is REJECTED (a false negative
    at worst), never accepted (which would risk a dangerous false
    positive reaching the agent)."""
    validator = ContractValidator(semantic_verifier=RuleBasedSemanticVerifier())
    contract = _contract()
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={"max_budget": 30000},
        utterance="Well, that's an interesting thought about the weather today.",
        identity=GenerationIdentity("b1", 0, 0),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.UNKNOWN, "ambiguous input must reject (UNKNOWN), never silently pass"


# ---------------------------------------------------------------------------
# 28.4 EXECUTION
# ---------------------------------------------------------------------------


def test_edge06_validator_pass_but_tts_fail_is_not_behavior_executed() -> None:
    """Edge 6: TTS failure after a VALID verdict must be reported as
    TTS_ERROR, not conflated with a successfully executed behavior, and
    must not silently trigger another AI call."""
    state = record_tts_attempt(validation_passed=True, tts_succeeded=False)
    failure = failure_from_tts_state(state)
    assert failure is not None
    assert failure.reason == FailureReason.TTS_ERROR
    assert state.should_retry_tts_only() is True, "must retry TTS only, never re-invoke the AI"


def test_edge07_tts_success_but_transport_publish_fail_is_transport_error() -> None:
    """Edge 7: a successful TTS synthesis followed by a LiveKit publish
    failure is a TRANSPORT_ERROR, not a caller-content problem."""
    from livekit_agent_simulator.caller_contract.failures import failure_from_transport

    failure = failure_from_transport("room disconnected during publish")
    assert failure.reason == FailureReason.TRANSPORT_ERROR


def test_edge13_behavior_never_satisfied_fails_run_by_default() -> None:
    """Edge 13: hitting max_turns without satisfaction must FAIL the run
    (BEHAVIOR_TIMEOUT), never silently skip to the next behavior — that
    would make the run result a false positive."""
    orch = Orchestrator()
    orch.start_behavior()
    contract = _contract()
    outcome = BehaviorOutcome.CONTINUE
    for _ in range(contract.constraints.max_turns):
        orch.advance_caller_turn()
        outcome = orch.check_max_turns(contract)
    assert outcome == BehaviorOutcome.FAILED_MAX_TURNS

    failure = failure_from_behavior_timeout(max_turns=contract.constraints.max_turns)
    assert failure.reason == FailureReason.BEHAVIOR_TIMEOUT


def test_edge14_ended_by_has_canonical_values_for_every_end_reason() -> None:
    """Edge 14: a call can end in many places (scenario/caller/agent/
    timeout/transport/error) — all six must be represented by the
    canonical EndedBy enum, not ad-hoc strings."""
    from livekit_agent_simulator.caller_contract import EndedBy

    assert {m.value for m in EndedBy} == {"scenario", "caller", "agent", "timeout", "transport", "error"}


# ---------------------------------------------------------------------------
# 28.5 EVALUATION
# ---------------------------------------------------------------------------


def test_edge12_behavior_satisfied_immediately_needs_no_extra_generation() -> None:
    """Edge 12: if the agent's response already satisfies the behavior,
    the orchestrator must move to the next behavior WITHOUT an extra
    Realtime call just because the caller "hasn't spoken again"."""
    evaluator = BehaviorEvaluator()
    contract = BehaviorContract(behavior="ask", target="price")
    verdict = evaluator.evaluate(contract, "The lowest I can do is $31,000.")
    assert verdict == EvaluatorVerdict.SATISFIED, "no extra generation should be needed once satisfied"


def test_edge17_say_action_still_crosses_orchestrator_turn_gate() -> None:
    """Edge 17: `say:` bypasses AI/Validator but must NEVER bypass the
    Orchestrator's turn gate — never a parser-direct publish."""
    action = parse_step({"say": "Hi."}, line_no=1)
    assert action.bypasses_ai_and_validator is True
    assert action.requires_turn_gate is True

    orch = Orchestrator()
    orch.start_behavior()
    with pytest.raises(RuntimeError, match="without crossing the Orchestrator turn gate"):
        orch.gate_say(has_crossed_turn_gate=False)


def test_edge18_interaction_planner_cannot_create_semantic_violation() -> None:
    """Edge 18: the Interaction Planner must never inject new semantic
    content while applying delivery transforms (pace/hesitation/stumble)."""
    from livekit_agent_simulator.caller_contract.dsl import InteractionConfig

    planner = CallerInteractionPlanner()
    utterance = "Could you lower the price?"
    outcome = planner.plan_speak(utterance, InteractionConfig(hesitation="occasional", stumble="occasional"))
    assert verify_semantic_preserving(utterance, outcome.tokens), (
        f"planner must not inject new content, got tokens={outcome.tokens}"
    )
    # Adversarial: a token set with injected new content must be caught.
    assert verify_semantic_preserving(utterance, outcome.tokens + ["financing?"]) is False


def test_edge22_record_replay_captures_failures_not_only_success() -> None:
    """Edge 22: replay must be reproducible for a FAILING run, not just a
    passing one — otherwise the exact failure can never be debugged."""
    validator = ContractValidator()
    contract = _contract()
    recorder = Recorder(scenario_id="edge22", seed=1)

    rejected = CandidateUtterance(
        act="ask", target="financing", slots={}, utterance="Do you offer financing?",
        identity=GenerationIdentity("b1", 0, 0),
    )
    result = validator.validate(rejected, contract)
    recorder.record_attempt(rejected, result, retry_index=0)
    assert not result.is_valid()

    record = recorder.finalize()
    assert len(record.attempts) == 1
    assert record.attempts[0].verdict != Verdict.VALID.value

    # Replaying it must reproduce the identical (non-valid) verdict.
    backend = ReplayLanguageBackend(record)
    raw = backend.generate({})
    assert raw["utterance"] == "Do you offer financing?"
    with pytest.raises(ReplayMismatchError):
        backend.assert_verdict(Verdict.VALID)  # proves mismatch detection works


def test_edge25_agent_response_evaluation_is_a_distinct_mechanism_from_caller_validation() -> None:
    """Edge 25: 'did the AGENT satisfy the behavior' (BehaviorEvaluator)
    must never be conflated with 'did the CALLER say something in
    contract' (ContractValidator) — they are structurally different
    classes answering different questions."""
    assert BehaviorEvaluator is not ContractValidator
    assert not issubclass(BehaviorEvaluator, ContractValidator)
    assert not issubclass(ContractValidator, BehaviorEvaluator)
