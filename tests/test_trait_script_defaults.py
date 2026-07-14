"""P1.G1 — soft Script defaults from Persona.traits (portable; explicit Script wins)."""

from __future__ import annotations

from livekit_agent_simulator.behavior_compile import (
    apply_caller_behavior,
    compile_from_traits,
    merge_script_steps,
)
from livekit_agent_simulator.script.models import ScriptStep, counts_for_recovery_barge


def test_interrupts_trait_adds_correction_barge():
    steps = compile_from_traits({"traits": ["interrupts", "polite"]})
    assert any(
        counts_for_recovery_barge(barge_in=s.barge_in, interrupt_class=s.interrupt_class)
        for s in steps
    )
    barge = next(s for s in steps if s.id == "trait-auto-barge-1")
    assert barge.interrupt_class == "correction"
    assert barge.barge_in is True


def test_backchannel_trait_adds_non_barge():
    steps = compile_from_traits({"traits": ["backchannel"]})
    bc = next(s for s in steps if s.interrupt_class == "backchannel")
    assert bc.barge_in is False
    assert bc.asset and "backchannel" in bc.asset


def test_silent_trait_adds_wait_hold():
    steps = compile_from_traits({"traits": ["silent"]})
    w = next(s for s in steps if s.action == "wait")
    assert w.silence_after_cue_ms >= 5000


def test_no_trait_no_steps():
    assert compile_from_traits({"traits": ["polite"]}) == []


def test_skip_when_barge_already_present():
    already = [
        ScriptStep(
            id="manual-barge",
            trigger="agent_speaking",
            delay_ms=100,
            say="Hold",
            barge_in=True,
            interrupt_class="correction",
        )
    ]
    steps = compile_from_traits({"traits": ["interrupts"]}, already=already)
    assert not any(s.id == "trait-auto-barge-1" for s in steps)


def test_explicit_script_wins_id():
    explicit = [
        ScriptStep(
            id="trait-auto-barge-1",
            trigger="agent_speaking",
            delay_ms=50,
            say="CUSTOM",
            barge_in=True,
            interrupt_class="correction",
        )
    ]
    steps, verify = apply_caller_behavior(
        {"traits": ["interrupts"], "brief": "x"},
        None,
        explicit,
        None,
    )
    barge = next(s for s in steps if s.id == "trait-auto-barge-1")
    assert barge.say == "CUSTOM"
    assert verify is not None
    assert verify.min_agent_finals_after_barge_in >= 1


def test_apply_traits_only_compiles_and_default_verify():
    steps, verify = apply_caller_behavior(
        {"traits": ["impatient"], "brief": "caller"},
        None,
        [],
        None,
    )
    assert any(s.barge_in for s in steps)
    assert verify is not None and verify.min_agent_finals_after_barge_in >= 1


def test_hangup_threat_does_not_auto_hang_up():
    steps = compile_from_traits({"traits": ["hangup_threat"]})
    assert not any(s.action == "hang_up" for s in steps)
