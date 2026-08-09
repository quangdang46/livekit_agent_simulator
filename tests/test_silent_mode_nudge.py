"""P1.B1 — silent_mode skips agent-greeted nudge; no barge under silent speech_conditions."""

from __future__ import annotations

import asyncio

import pytest

from livekit_agent_simulator.behavior_compile import compile_from_speech_conditions
from livekit_agent_simulator.caller_nudge import nudge_caller_after_agent_greeting
from livekit_agent_simulator.behavior_compile import silent_mode_enabled


def test_persona_is_silent_mode_flags():
    assert silent_mode_enabled({"speech_conditions": {"silent_mode": True}})
    # trait "silent" is a speech-styling hint, NOT dead-air silent_mode (main design)
    assert not silent_mode_enabled({"traits": ["silent"]})
    assert not silent_mode_enabled({"traits": ["polite"]})


def test_silent_mode_skips_barge_policy():
    steps = compile_from_speech_conditions(
        {
            "speech_conditions": {
                "silent_mode": True,
                "barge_policy": "mid_agent_turn",
            }
        }
    )
    assert not any(s.barge_in for s in steps)
    # Silent mode compiles NO auto steps at all — caller stays mute (Coval).


@pytest.mark.asyncio
async def test_nudge_skip_silent_emits_event():
    events = []

    class W:
        def emit(self, kind, spec=None, **kw):
            events.append((kind, spec))

    class O:
        agent_has_spoken = True
        user_has_spoken = False

    class B:
        end_call = asyncio.Event()

        async def inject_cue(self, *a, **k):
            raise AssertionError("must not inject when silent")

    await nudge_caller_after_agent_greeting(
        O(), B(), W(), first_speaker="agent", silent_mode=True
    )
    assert events and events[0][0] == "sim.agent_greeted_nudge_skipped"
    assert events[0][1].get("reason") == "silent_mode"
