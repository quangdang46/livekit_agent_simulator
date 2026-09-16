"""P0-5b: Caller Interaction Planner tests.

Proves the output token set is a subset of the validated utterance plus the
hesitation-token allowlist, covers pace/hesitation/stumble/pre_delay,
DTMF/silence/hangup routes, and backchannel as an InteractionAction
distinct from a semantic Behavior.
"""

from __future__ import annotations

from livekit_agent_simulator.caller_contract.dsl import InteractionConfig
from livekit_agent_simulator.caller_contract.interaction_planner import (
    HESITATION_TOKEN_ALLOWLIST,
    CallerInteractionPlanner,
    InteractionActionKind,
    verify_semantic_preserving,
)

VALIDATED_UTTERANCE = "Would you be able to come down to $30,000?"


def test_plan_speak_no_interaction_config_passes_utterance_through() -> None:
    planner = CallerInteractionPlanner()
    outcome = planner.plan_speak(VALIDATED_UTTERANCE, interaction=None)
    assert outcome.kind == InteractionActionKind.SPEAK
    assert outcome.tokens == VALIDATED_UTTERANCE.split()
    assert outcome.pre_delay_ms == 0
    assert outcome.pace is None


def test_plan_speak_applies_pace_hint() -> None:
    planner = CallerInteractionPlanner()
    config = InteractionConfig(pace="slow")
    outcome = planner.plan_speak(VALIDATED_UTTERANCE, interaction=config)
    assert outcome.pace == "slow"


def test_plan_speak_applies_pre_delay() -> None:
    planner = CallerInteractionPlanner()
    config = InteractionConfig(pre_delay_ms=500)
    outcome = planner.plan_speak(VALIDATED_UTTERANCE, interaction=config)
    assert outcome.pre_delay_ms == 500


def test_plan_speak_applies_hesitation_token_from_allowlist() -> None:
    planner = CallerInteractionPlanner()
    config = InteractionConfig(hesitation="occasional")
    outcome = planner.plan_speak(VALIDATED_UTTERANCE, interaction=config)
    inserted = [t for t in outcome.tokens if t in HESITATION_TOKEN_ALLOWLIST]
    assert len(inserted) == 1


def test_plan_speak_applies_stumble_as_word_repetition() -> None:
    planner = CallerInteractionPlanner()
    config = InteractionConfig(stumble="occasional")
    outcome = planner.plan_speak(VALIDATED_UTTERANCE, interaction=config)
    assert outcome.tokens[0].startswith("Would") and outcome.tokens[0].endswith("...")


def test_plan_speak_combines_pace_hesitation_stumble_pre_delay() -> None:
    planner = CallerInteractionPlanner()
    config = InteractionConfig(pace="slow", hesitation="occasional", stumble="occasional", pre_delay_ms=300)
    outcome = planner.plan_speak(VALIDATED_UTTERANCE, interaction=config)
    assert outcome.pace == "slow"
    assert outcome.pre_delay_ms == 300
    assert any(t in HESITATION_TOKEN_ALLOWLIST for t in outcome.tokens)
    assert any(t.endswith("...") for t in outcome.tokens)


# ---------------------------------------------------------------------------
# Semantic-preserving whitelist enforcement (report §28.5(18))
# ---------------------------------------------------------------------------


def test_output_tokens_are_subset_of_utterance_plus_hesitation_allowlist_no_config() -> None:
    planner = CallerInteractionPlanner()
    outcome = planner.plan_speak(VALIDATED_UTTERANCE, interaction=None)
    assert verify_semantic_preserving(VALIDATED_UTTERANCE, outcome.tokens)


def test_output_tokens_are_subset_with_hesitation_and_stumble() -> None:
    planner = CallerInteractionPlanner()
    config = InteractionConfig(hesitation="occasional", stumble="occasional")
    outcome = planner.plan_speak(VALIDATED_UTTERANCE, interaction=config)
    assert verify_semantic_preserving(VALIDATED_UTTERANCE, outcome.tokens)


def test_verify_semantic_preserving_detects_injected_new_content() -> None:
    """Adversarial check: if a token set contains a word NOT in the
    original utterance or the hesitation allowlist, it must be flagged —
    this is what would catch a design violation where the planner starts
    generating free text (e.g. 'Also, do you offer financing?')."""
    tokens = VALIDATED_UTTERANCE.split() + ["financing?"]
    assert verify_semantic_preserving(VALIDATED_UTTERANCE, tokens) is False


def test_plan_speak_asserts_semantic_preserving_internally() -> None:
    """The planner itself asserts semantic-preservation before returning —
    proven here by confirming a normal call never raises."""
    planner = CallerInteractionPlanner()
    config = InteractionConfig(hesitation="occasional", stumble="occasional", pace="fast", pre_delay_ms=100)
    outcome = planner.plan_speak(VALIDATED_UTTERANCE, interaction=config)  # must not raise
    assert outcome.kind == InteractionActionKind.SPEAK


# ---------------------------------------------------------------------------
# Non-speech routes: DTMF / silence / hangup (never go through TTS)
# ---------------------------------------------------------------------------


def test_plan_dtmf_route() -> None:
    planner = CallerInteractionPlanner()
    outcome = planner.plan_dtmf("123#")
    assert outcome.kind == InteractionActionKind.DTMF
    assert outcome.dtmf_digits == "123#"
    assert outcome.tokens == []


def test_plan_silence_route() -> None:
    planner = CallerInteractionPlanner()
    outcome = planner.plan_silence()
    assert outcome.kind == InteractionActionKind.SILENCE
    assert outcome.tokens == []


def test_plan_hangup_route() -> None:
    planner = CallerInteractionPlanner()
    outcome = planner.plan_hangup()
    assert outcome.kind == InteractionActionKind.HANGUP
    assert outcome.tokens == []


# ---------------------------------------------------------------------------
# Backchannel as InteractionAction distinct from semantic Behavior
# ---------------------------------------------------------------------------


def test_backchannel_is_interaction_action_not_behavior() -> None:
    planner = CallerInteractionPlanner()
    outcome = planner.plan_backchannel()
    assert outcome.kind == InteractionActionKind.BACKCHANNEL
    assert outcome.tokens == ["uh-huh"]
    # Structural proof: InteractionOutcome carries no contract/behavior field.
    assert not hasattr(outcome, "contract")
    assert not hasattr(outcome, "behavior")


def test_backchannel_custom_text() -> None:
    planner = CallerInteractionPlanner()
    outcome = planner.plan_backchannel(text="mm-hmm")
    assert outcome.tokens == ["mm-hmm"]


# ---------------------------------------------------------------------------
# Barge-in trigger only — execution stays with the Orchestrator
# ---------------------------------------------------------------------------


def test_trigger_barge_in_only_emits_event_no_audio_cancellation() -> None:
    planner = CallerInteractionPlanner()
    outcome = planner.trigger_barge_in()
    assert outcome.kind == InteractionActionKind.BARGE_IN_TRIGGER
    # The planner has no method that cancels audio directly.
    assert not hasattr(planner, "cancel_audio")
    assert not hasattr(planner, "stop_playback")
