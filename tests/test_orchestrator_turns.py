"""P0-4: Orchestrator + Turn Detector + Behavior Evaluator tests.

Covers: silence-debounce turn completion, multi-chunk correlation, agent
self-interrupt handling, stale-generation drop during validation and TTS,
four-way agent-silence split, max_turns fail-run default, and a dedicated
test that the say-path still passes through the Orchestrator turn gate
(never parser-direct publish). No new LiveKit SDK calls are made anywhere
in this module (pure Python, no `livekit` import).
"""

from __future__ import annotations

import sys

import pytest

from livekit_agent_simulator.caller_contract import (
    BehaviorContract,
    ContractConstraints,
    EvaluatorVerdict,
)
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


def test_no_livekit_sdk_import_in_orchestrator_module() -> None:
    """Structural guard: this module must never import the LiveKit SDK
    (report §22.1 — no logic injected into the LiveKit SDK)."""
    mod = sys.modules["livekit_agent_simulator.caller_contract.orchestrator"]
    assert "livekit" not in mod.__dict__
    assert not hasattr(mod, "livekit")


# ---------------------------------------------------------------------------
# TurnDetector: silence-debounce turn completion
# ---------------------------------------------------------------------------


def test_silence_debounce_turn_completion() -> None:
    detector = TurnDetector(silence_debounce_ms=300)
    detector.on_agent_audio_started(now_ms=0)
    assert detector.state == TurnState.AGENT_SPEAKING

    detector.on_agent_audio_stopped(now_ms=100)
    assert detector.poll(now_ms=150) == TurnState.POSSIBLE_END, "still inside debounce window"

    assert detector.poll(now_ms=500) == TurnState.AGENT_TURN_COMPLETE, "debounce window elapsed"


def test_short_pause_does_not_prematurely_complete_turn() -> None:
    """report §22.2: 'Sure, I can help...' [300ms pause] '...order number?'
    must not be split into two turns if a new chunk starts within the
    debounce window."""
    detector = TurnDetector(silence_debounce_ms=300)
    detector.on_agent_audio_started(now_ms=0)
    detector.on_agent_audio_stopped(now_ms=500)
    assert detector.poll(now_ms=600) == TurnState.POSSIBLE_END

    # New chunk arrives before debounce elapses -> same turn continues.
    detector.on_agent_audio_started(now_ms=650)
    assert detector.state == TurnState.AGENT_SPEAKING
    assert detector.poll(now_ms=650) == TurnState.AGENT_SPEAKING


# ---------------------------------------------------------------------------
# Multi-chunk correlation (report §28.2(3))
# ---------------------------------------------------------------------------


def test_multi_chunk_streaming_audio_correlates_into_one_turn() -> None:
    detector = TurnDetector(silence_debounce_ms=200)
    # started -> chunk -> chunk -> stopped, all within one logical turn.
    detector.on_agent_audio_started(now_ms=0)
    detector.on_agent_audio_started(now_ms=50)  # another chunk, same turn
    detector.on_agent_audio_started(now_ms=100)  # another chunk, same turn
    assert detector.chunk_count == 3
    detector.on_agent_audio_stopped(now_ms=150)
    assert detector.poll(now_ms=160) == TurnState.POSSIBLE_END
    assert detector.poll(now_ms=400) == TurnState.AGENT_TURN_COMPLETE


def test_chunk_count_resets_on_next_agent_turn() -> None:
    detector = TurnDetector(silence_debounce_ms=100)
    detector.on_agent_audio_started(now_ms=0)
    detector.on_agent_audio_stopped(now_ms=10)
    detector.poll(now_ms=200)
    assert detector.state == TurnState.AGENT_TURN_COMPLETE
    detector.reset_for_next_agent_turn()
    assert detector.chunk_count == 0
    assert detector.state == TurnState.WAITING_FOR_AGENT


# ---------------------------------------------------------------------------
# Agent self-interrupt vs transport glitch (report §28.2(4))
# ---------------------------------------------------------------------------


def test_agent_self_interrupt_requires_explicit_signal() -> None:
    """Agent starting to speak during the caller's turn is, by default,
    NOT treated as intentional — only an explicit signal marks it so
    (guards against transport glitches masquerading as interruptions)."""
    detector = TurnDetector()
    detector.begin_caller_turn()
    assert detector.state == TurnState.CALLER_TURN

    detector.on_agent_audio_started(now_ms=0)  # agent starts speaking mid-caller-turn
    assert detector.intentional_interrupt is False, "must not assume intent without an explicit signal"


def test_agent_self_interrupt_marked_intentional_via_explicit_signal() -> None:
    detector = TurnDetector()
    detector.begin_caller_turn()
    detector.on_agent_audio_started(now_ms=0)
    detector.mark_intentional_interrupt()
    assert detector.intentional_interrupt is True


# ---------------------------------------------------------------------------
# Stale-generation drop during validation and TTS (report §28.1(15)(16)(21))
# ---------------------------------------------------------------------------


def test_stale_generation_dropped_during_validation() -> None:
    """Candidate generated for one generation_id must be dropped if the
    state advanced (e.g. a new turn started) before validation finished."""
    orch = Orchestrator()
    orch.start_behavior()
    orch.advance_caller_turn()
    identity_at_generate_time = orch.new_generation()

    # Simulate the world moving on while validation was in flight: a new
    # turn started (e.g. interrupt handling advanced state).
    orch.advance_caller_turn()

    assert orch.is_stale(identity_at_generate_time) is True, "stale candidate must be dropped, never published"


def test_stale_generation_dropped_during_tts() -> None:
    """Same staleness guard applies to a TTS job in flight."""
    orch = Orchestrator()
    orch.start_behavior()
    orch.advance_caller_turn()
    identity_at_tts_start = orch.new_generation()

    # Behavior changed underneath the running TTS job.
    orch.start_behavior()
    orch.advance_caller_turn()

    assert orch.is_stale(identity_at_tts_start) is True


def test_current_generation_is_not_stale() -> None:
    orch = Orchestrator()
    orch.start_behavior()
    orch.advance_caller_turn()
    identity = orch.new_generation()
    assert orch.is_stale(identity) is False


def test_context_version_does_not_affect_staleness() -> None:
    """context_version tracks ConversationContext freshness only; it is
    not part of the behavior_id/turn_id/generation_id staleness triple."""
    orch = Orchestrator()
    orch.start_behavior()
    orch.advance_caller_turn()
    identity = orch.new_generation()
    from livekit_agent_simulator.caller_contract import GenerationIdentity

    bumped_context = GenerationIdentity(
        behavior_id=identity.behavior_id,
        turn_id=identity.turn_id,
        generation_id=identity.generation_id,
        context_version=identity.context_version + 5,
    )
    assert orch.is_stale(bumped_context) is False


# ---------------------------------------------------------------------------
# Four-way agent-silence split (report §28.1(1))
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "elapsed_ms,turn_timeout_ms,agent_hung_up,transport_lost,expected",
    [
        (1000, 10_000, False, False, AgentSilenceOutcome.PROCESSING),
        (11_000, 10_000, False, False, AgentSilenceOutcome.TIMEOUT),
        (1000, 10_000, True, False, AgentSilenceOutcome.HANGUP),
        (1000, 10_000, False, True, AgentSilenceOutcome.TRANSPORT_ERROR),
    ],
)
def test_agent_silence_four_way_split(
    elapsed_ms: int, turn_timeout_ms: int, agent_hung_up: bool, transport_lost: bool, expected: AgentSilenceOutcome
) -> None:
    outcome = classify_agent_silence(
        elapsed_ms=elapsed_ms,
        turn_timeout_ms=turn_timeout_ms,
        agent_hung_up=agent_hung_up,
        transport_lost=transport_lost,
    )
    assert outcome == expected


def test_transport_lost_takes_priority_over_hangup_flag() -> None:
    """Distinguishing transport failure from a real hangup matters for
    debugging (report §28.4(7)) — transport_lost must win if both are set."""
    outcome = classify_agent_silence(
        elapsed_ms=1000, turn_timeout_ms=10_000, agent_hung_up=True, transport_lost=True
    )
    assert outcome == AgentSilenceOutcome.TRANSPORT_ERROR


def test_orchestrator_classify_silence_uses_configured_timeout() -> None:
    orch = Orchestrator(timeouts=TimeoutConfig(turn_timeout_ms=5000))
    assert orch.classify_silence(elapsed_ms=6000) == AgentSilenceOutcome.TIMEOUT
    assert orch.classify_silence(elapsed_ms=1000) == AgentSilenceOutcome.PROCESSING


# ---------------------------------------------------------------------------
# max_turns fail-run default (report §28.4(13))
# ---------------------------------------------------------------------------


def test_max_turns_reached_fails_run_by_default() -> None:
    orch = Orchestrator()
    orch.start_behavior()
    contract = BehaviorContract(
        behavior="negotiate", target="price", constraints=ContractConstraints(max_turns=3)
    )

    # The behavior budget counts only advance_behavior_turn() (published do:
    # turns inside the behavior) -- never the say:/action-level
    # advance_caller_turn() gate. An opening say: must not steal a turn.
    orch.advance_caller_turn()  # say: gate -- must NOT consume behavior budget
    assert orch.check_max_turns(contract) == BehaviorOutcome.CONTINUE

    outcomes = []
    for _ in range(3):
        orch.advance_behavior_turn()
        outcomes.append(orch.check_max_turns(contract))

    assert outcomes[-1] == BehaviorOutcome.FAILED_MAX_TURNS, (
        "hitting max_turns without satisfaction must FAIL the run, "
        "never silently skip to the next behavior (that would be a false-positive result)"
    )


def test_behavior_satisfied_before_max_turns_is_not_a_failure() -> None:
    orch = Orchestrator()
    orch.start_behavior()
    contract = BehaviorContract(
        behavior="negotiate", target="price", constraints=ContractConstraints(max_turns=3)
    )
    orch.advance_behavior_turn()
    assert orch.check_max_turns(contract) == BehaviorOutcome.CONTINUE


def test_say_gate_does_not_consume_behavior_budget() -> None:
    """Run 016-017 regression: an opening say: (action-level gate) must not
    steal a turn from the first behavior's max_turns budget. A behavior with
    max_turns=2 still gets its FULL two turns even after any number of say:
    gates crossed beforehand."""
    orch = Orchestrator()
    orch.start_behavior()
    contract = BehaviorContract(
        behavior="ask", target=None, constraints=ContractConstraints(max_turns=2)
    )
    for _ in range(5):
        orch.advance_caller_turn()  # say: gates -- budget must not move
    assert orch.check_max_turns(contract) == BehaviorOutcome.CONTINUE
    orch.advance_behavior_turn()  # first published do: turn
    assert orch.check_max_turns(contract) == BehaviorOutcome.CONTINUE
    orch.advance_behavior_turn()  # second published do: turn
    assert orch.check_max_turns(contract) == BehaviorOutcome.FAILED_MAX_TURNS


# ---------------------------------------------------------------------------
# say-path must still cross the Orchestrator turn gate (report §28.5(17))
# ---------------------------------------------------------------------------


def test_say_path_crosses_orchestrator_turn_gate() -> None:
    orch = Orchestrator()
    orch.start_behavior()

    orch.advance_caller_turn()  # the ONLY legitimate way to cross the gate
    orch.gate_say(has_crossed_turn_gate=True)  # must not raise


def test_say_path_direct_publish_without_turn_gate_raises() -> None:
    """A `say` action must never be published without first going through
    advance_caller_turn() — this is the invariant that keeps `say` from
    becoming a parser-direct publish that skips the Orchestrator entirely."""
    orch = Orchestrator()
    orch.start_behavior()

    with pytest.raises(RuntimeError, match="without crossing the Orchestrator turn gate"):
        orch.gate_say(has_crossed_turn_gate=False)


# ---------------------------------------------------------------------------
# BehaviorEvaluator (separate from Caller Contract Validator; report §28.5(25))
# ---------------------------------------------------------------------------


def test_behavior_evaluator_satisfied_on_clear_agreement() -> None:
    evaluator = BehaviorEvaluator()
    contract = BehaviorContract(behavior="arrange_visit", target="saturday")
    verdict = evaluator.evaluate(contract, "Saturday works perfectly for me.")
    assert verdict == EvaluatorVerdict.SATISFIED


def test_behavior_evaluator_partial_on_hedged_agreement() -> None:
    evaluator = BehaviorEvaluator()
    contract = BehaviorContract(behavior="arrange_visit", target="saturday")
    verdict = evaluator.evaluate(contract, "I can probably make Saturday work.")
    assert verdict == EvaluatorVerdict.PARTIAL


def test_behavior_evaluator_not_satisfied_on_unrelated_response() -> None:
    evaluator = BehaviorEvaluator()
    contract = BehaviorContract(behavior="arrange_visit", target="saturday")
    verdict = evaluator.evaluate(contract, "We don't have any openings this month.")
    assert verdict == EvaluatorVerdict.NOT_SATISFIED


def test_behavior_evaluator_satisfied_on_bare_price_quote() -> None:
    evaluator = BehaviorEvaluator()
    contract = BehaviorContract(behavior="ask", target="price")
    verdict = evaluator.evaluate(contract, "The lowest I can do is $31,000.")
    assert verdict == EvaluatorVerdict.SATISFIED


def test_behavior_evaluator_is_a_distinct_object_from_validator() -> None:
    """Structural proof that BehaviorEvaluator and ContractValidator are
    separate mechanisms answering different questions (§28.5(25))."""
    from livekit_agent_simulator.caller_contract.validator import ContractValidator

    assert BehaviorEvaluator is not ContractValidator
    assert not issubclass(BehaviorEvaluator, ContractValidator)
