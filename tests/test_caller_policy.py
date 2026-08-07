"""Unit tests for caller dialog policy (Strategy + Composite sections)."""

from __future__ import annotations

from livekit_agent_simulator.caller import build_persona_system_instruction
from livekit_agent_simulator.caller.default_policy import DefaultCallerPolicy
from livekit_agent_simulator.caller.policy import CallerPolicyContext, MidcallCue
from livekit_agent_simulator.caller.prompt_sections import (
    ConstraintsSection,
    ContextSection,
    FirstSpeakerSection,
    GoalsSection,
    GuardrailsSection,
    NaturalSpeechSection,
    RoleSection,
    ScriptTimingSection,
)


def test_role_section_includes_locale():
    sec = RoleSection()
    lines = sec.render(CallerPolicyContext(persona={"name": "Sam"}, locale="vi-VN"))
    joined = "\n".join(lines)
    assert "RESPOND IN vi-VN" in joined
    assert "HUMAN" in joined
    assert "Sam" in joined
    assert "If→then" in joined or "role lock" in joined.lower()


def test_guardrails_block_role_flip():
    joined = "\n".join(
        GuardrailsSection().render(
            CallerPolicyContext(persona={"goals": ["Ask fee"]}, locale="en")
        )
    )
    assert "Never switch roles" in joined or "sounding like staff" in joined


def test_role_section_situation_and_outcome():
    ctx = CallerPolicyContext(
        persona={
            "name": "Mai",
            "situation": "Signing up; unsure about the monthly fee",
            "outcome": "Hear a clear fee or decide to decline",
            "brief": "Small business owner",
        },
        locale="en-US",
    )
    joined = "\n".join(RoleSection().render(ctx))
    assert "Your situation" in joined
    assert "monthly fee" in joined
    assert "Desired call outcome" in joined
    assert "Additional brief" in joined


def test_goals_section_creates_checklist():
    ctx = CallerPolicyContext(persona={"goals": ["A", "B"]}, locale="en")
    lines = GoalsSection().render(ctx)
    joined = "\n".join(lines)
    assert "GOAL 1" in joined
    assert "GOAL 2" in joined
    assert "Do NOT say goodbye" in joined
    assert "dialogue mode" in joined.lower() or "you own speech" in joined.lower()


def test_constraints_section_adds_examples():
    ctx = CallerPolicyContext(
        persona={"constraints": ["No card numbers"]}, locale="en"
    )
    lines = ConstraintsSection().render(ctx)
    joined = "\n".join(lines)
    assert "HARD CONSTRAINTS" in joined
    assert "card number" in joined.lower()


def test_guardrails_present():
    ctx = CallerPolicyContext(persona={"goals": ["Ask about order"]}, locale="en")
    lines = GuardrailsSection().render(ctx)
    joined = "\n".join(lines)
    assert "GUARDRAILS" in joined
    assert "[END_CALL]" in joined
    assert 'NEVER pronounce the English words "end call"' in joined


def test_script_timing_forbids_early_bye():
    ctx = CallerPolicyContext(
        persona={"goals": ["Ask fee"]},
        locale="en",
        script_steps=[{"id": "open", "say": "hi", "action": "speak"}, {"id": "bye"}],
    )
    joined = "\n".join(ScriptTimingSection().render(ctx))
    assert "OVERLAY" in joined or "overlay" in joined.lower()
    assert "Do NOT freestyle barge" in joined or "Do NOT freestyle" in joined
    assert "Freestyle farewell" in joined
    assert "1–3 natural" in joined or "continue freestyle" in joined.lower()
    assert "milestone" in joined.lower()
    assert "freestyle" in joined.lower()
    assert "do not wait for another simulator injection" in joined.lower()
    # Natural default must not lock the old hard 1–2 ceiling as sole length rule
    assert "1–2 natural phone sentences" not in joined
    assert "answer in 1–2 natural" not in joined.lower()


def test_guardrails_script_mode_skips_freestyle_end_call_marker():
    ctx = CallerPolicyContext(
        persona={"goals": ["Ask fee"]},
        locale="en",
        script_steps=[{"id": "bye"}],
    )
    joined = "\n".join(GuardrailsSection().render(ctx))
    assert "Script overlay active" in joined or "Script hang-up" in joined
    assert "append the exact harness marker" not in joined
    assert "Natural answers" in joined or "1–3 natural" in joined or "answer" in joined.lower()
    assert "Ending before they are addressed is a failure" not in joined


def test_goals_script_mode_is_overlay_not_mute():
    ctx = CallerPolicyContext(
        persona={"goals": ["Ask fee", "Sign up"]},
        locale="en",
        script_steps=[{"id": "bye"}],
    )
    joined = "\n".join(GoalsSection().render(ctx))
    assert "Script overlay" in joined or "overlay is present" in joined
    assert "Work through ALL goals one by one in a natural" not in joined
    assert "milestone" in joined.lower()


def test_build_persona_system_instruction_facade():
    prompt = build_persona_system_instruction(
        persona={
            "name": "Sam",
            "brief": "Test caller",
            "goals": ["Confirm support", "End call"],
            "constraints": ["No card numbers"],
        },
        locale="en-US",
        first_speaker="agent",
    )
    assert "PERSONA" in prompt
    assert "GOAL 1" in prompt
    assert "HARD CONSTRAINTS" in prompt
    assert "GUARDRAILS" in prompt
    assert "Sam" in prompt
    # first_speaker agent → wait mark
    assert "Wait for the assistant" in prompt


def test_default_policy_midcall_dialogue_user_bootstrap():
    """Dialogue (no Script) + user-first still needs speak-first kick."""
    policy = DefaultCallerPolicy()
    ctx = CallerPolicyContext(
        persona={"goals": ["Find my order", "Cancel"], "brief": "test"},
        locale="en-US",
        first_speaker="user",
    )
    cues = policy.midcall_cues(ctx)
    boot = [c for c in cues if c.kind == "bootstrap"]
    regd = [c for c in cues if c.kind == "reground"]
    assert len(boot) == 1
    assert "speak first" in boot[0].text.lower()
    assert len(regd) >= 1
    assert "GOAL 1" in regd[0].text
    assert isinstance(cues[0], MidcallCue)


def test_default_policy_midcall_script_no_early_bye():
    policy = DefaultCallerPolicy()
    ctx = CallerPolicyContext(
        persona={"goals": ["Fee"], "brief": "test"},
        locale="en-US",
        first_speaker="agent",
        script_steps=[{"id": "open"}, {"id": "bye"}],
    )
    cues = policy.midcall_cues(ctx)
    script_rg = [c for c in cues if c.label == "script_no_early_bye"]
    assert len(script_rg) == 1
    assert "Do not say bye" in script_rg[0].text
    assert "1–3 natural" in script_rg[0].text or "spoken clauses" in script_rg[0].text
    # Must not reintroduce hard 1–2 as the only answer length
    assert "1–2 natural sentences" not in script_rg[0].text
    assert "1-2 natural sentences" not in script_rg[0].text
    assert not any(c.kind == "bootstrap" for c in cues)


def test_default_policy_script_user_no_bootstrap():
    """Script actively opens (time trigger + say) — never bootstrap (avoids double-open)."""
    policy = DefaultCallerPolicy()
    ctx = CallerPolicyContext(
        persona={"goals": ["Fee"], "brief": "test"},
        locale="en-US",
        first_speaker="user",
        script_steps=[{"id": "open", "trigger": "time", "say": "Hi, I'm calling about the fee."}],
    )
    cues = policy.midcall_cues(ctx)
    assert [c for c in cues if c.kind == "bootstrap"] == []


def test_default_policy_script_reactive_user_still_bootstraps():
    """Reactive-only script (agent_speaking triggers) does NOT own the opening —
    the caller still needs the speak-first bootstrap."""
    policy = DefaultCallerPolicy()
    ctx = CallerPolicyContext(
        persona={"goals": ["Fee"], "brief": "test"},
        locale="en-US",
        first_speaker="user",
        script_steps=[
            {"id": "barge", "trigger": "agent_speaking", "say": "Hold on"},
            {"id": "hangup", "trigger": "time", "action": "hang_up"},
        ],
    )
    cues = policy.midcall_cues(ctx)
    assert any(c.kind == "bootstrap" for c in cues)


def test_first_speaker_section_defers_to_script():
    ctx = CallerPolicyContext(
        persona={},
        locale="en-US",
        first_speaker="user",
        script_steps=[{"id": "open"}],
    )
    joined = "\n".join(FirstSpeakerSection().render(ctx))
    assert "stay silent at connect" in joined.lower()
    assert "freestyle" in joined.lower()
    assert "You speak first" not in joined


def test_first_speaker_user_dialogue_in_si():
    ctx = CallerPolicyContext(
        persona={},
        locale="en-US",
        first_speaker="user",
        script_steps=[],
    )
    joined = "\n".join(FirstSpeakerSection().render(ctx))
    assert "You speak first" in joined


def test_context_notes_not_injected_into_si():
    ctx = CallerPolicyContext(
        persona={},
        locale="en-US",
        context={
            "notes": "Dialogue mode — no Script. Use first_speaker=user.",
            "caller_knows": "You only need the basic plan.",
            "fixtures": {"preferred_plan": "basic"},
        },
    )
    joined = "\n".join(ContextSection().render(ctx))
    assert "Dialogue mode" not in joined
    assert "basic plan" in joined
    assert "preferred_plan=basic" in joined


def test_dialogue_guardrails_one_goodbye():
    ctx = CallerPolicyContext(persona={"goals": ["Ask fee"]}, locale="en-US", script_steps=[])
    joined = "\n".join(GuardrailsSection().render(ctx))
    assert "ONE short goodbye" in joined
    assert "thank-you loops" in joined


# --- Verbosity resolution + SI bands ---


def test_resolved_verbosity_default_natural():
    ctx = CallerPolicyContext(persona={}, locale="en-US")
    assert ctx.resolved_verbosity() == "natural"


def test_resolved_verbosity_explicit_and_traits():
    assert (
        CallerPolicyContext(
            persona={"speech_conditions": {"verbosity": "quiet"}, "traits": ["chatty"]},
            locale="en",
        ).resolved_verbosity()
        == "quiet"
    )
    assert (
        CallerPolicyContext(
            persona={"speech_conditions": {"verbosity": "CHATTY"}},
            locale="en",
        ).resolved_verbosity()
        == "chatty"
    )
    assert (
        CallerPolicyContext(persona={"traits": ["chatty", "quiet"]}, locale="en").resolved_verbosity()
        == "chatty"
    )
    assert (
        CallerPolicyContext(persona={"traits": ["quiet"]}, locale="en").resolved_verbosity()
        == "quiet"
    )
    assert (
        CallerPolicyContext(persona={"traits": ["terse"]}, locale="en").resolved_verbosity()
        == "quiet"
    )
    assert (
        CallerPolicyContext(
            persona={"speech_conditions": {"verbosity": "loud"}},
            locale="en",
        ).resolved_verbosity()
        == "natural"
    )


def test_default_si_natural_band_no_hard_one_two_ceiling():
    prompt = build_persona_system_instruction(
        persona={"name": "Sam", "goals": ["Ask fee"]},
        locale="en-US",
    )
    assert "NATURAL SPEECH" in prompt
    assert "um" in prompt.lower() or "uh" in prompt.lower()
    # Natural band must NOT hard-cap utterance length
    assert "Keep every utterance short and natural like real phone speech (1-2 sentences)." not in prompt
    assert "(1-2 sentences)" not in prompt
    assert "2–5 spoken clauses" not in prompt and "2-5 spoken clauses" not in prompt


def test_verbosity_quiet_si_no_natural_speech_section():
    prompt = build_persona_system_instruction(
        persona={
            "name": "Sam",
            "goals": ["Ask fee"],
            "speech_conditions": {"verbosity": "quiet"},
        },
        locale="en-US",
    )
    assert "one short spoken clause" in prompt.lower() or "short spoken clause" in prompt
    assert "NATURAL SPEECH" not in prompt
    ctx = CallerPolicyContext(
        persona={"speech_conditions": {"verbosity": "quiet"}},
        locale="en-US",
    )
    assert NaturalSpeechSection().render(ctx) == []


def test_verbosity_chatty_si_longer_band_and_fillers():
    prompt = build_persona_system_instruction(
        persona={
            "name": "Sam",
            "goals": ["Ask fee"],
            "speech_conditions": {"verbosity": "chatty"},
        },
        locale="en-US",
    )
    assert "3–6 spoken clauses" in prompt.lower() or "several spoken" in prompt.lower() or "3-6" in prompt.lower()
    assert "NATURAL SPEECH" in prompt


def test_trait_quiet_and_chatty_si_bands():
    quiet_prompt = build_persona_system_instruction(
        persona={"goals": ["Ask"], "traits": ["quiet"]},
        locale="en-US",
    )
    assert "NATURAL SPEECH" not in quiet_prompt
    assert "short spoken clause" in quiet_prompt.lower() or "short" in quiet_prompt.lower()

    chatty_prompt = build_persona_system_instruction(
        persona={"goals": ["Ask"], "traits": ["chatty"]},
        locale="en-US",
    )
    assert "NATURAL SPEECH" in chatty_prompt
    assert "3–6" in chatty_prompt or "3-6" in chatty_prompt or "conversational loop" in chatty_prompt.lower()


def test_explicit_verbosity_overrides_traits_in_si():
    prompt = build_persona_system_instruction(
        persona={
            "goals": ["Ask"],
            "traits": ["chatty"],
            "speech_conditions": {"verbosity": "quiet"},
        },
        locale="en-US",
    )
    assert "NATURAL SPEECH" not in prompt
    assert "short spoken clause" in prompt.lower() or "short" in prompt.lower()


def test_style_short_turns_does_not_force_quiet():
    """Free-text style is advisory; missing verbosity → natural."""
    ctx = CallerPolicyContext(
        persona={"style": "warm, everyday caller; short turns"},
        locale="en-US",
    )
    assert ctx.resolved_verbosity() == "natural"
    prompt = build_persona_system_instruction(
        persona={"goals": ["Ask"], "style": "warm, everyday caller; short turns"},
        locale="en-US",
    )
    assert "NATURAL SPEECH" in prompt
    assert "(1-2 sentences)" not in prompt
    # v2: short-turns phrase must not fight the natural band in SI
    assert "Speaking style:" in prompt
    assert "short turns" not in prompt.lower()
    assert "verbosity" in prompt.lower() or "brevity" in prompt.lower()
    assert "warm" in prompt.lower() or "everyday" in prompt.lower()
    assert "ordinary person on the phone" in prompt.lower() or "real person" in prompt.lower()


def test_style_short_turns_kept_when_quiet():
    prompt = build_persona_system_instruction(
        persona={
            "goals": ["Ask"],
            "style": "warm, everyday caller; short turns",
            "speech_conditions": {"verbosity": "quiet"},
        },
        locale="en-US",
    )
    assert "short turns" in prompt.lower()
    assert "NATURAL SPEECH" not in prompt


def test_natural_speech_section_has_phone_examples():
    ctx = CallerPolicyContext(persona={}, locale="en-US")
    joined = "\n".join(NaturalSpeechSection().render(ctx))
    assert "NATURAL SPEECH" in joined
    # bad→natural example pairs with real filler patterns
    assert "silver one" in joined.lower() or "financing" in joined.lower()
    assert "um" in joined.lower() or "uh" in joined.lower()
    assert "goodbye" in joined.lower() or "Script" in joined
