"""Freestyle room audio is muted while Script steps remain."""

from __future__ import annotations

from types import SimpleNamespace

from livekit_agent_simulator.gemini.live_session import GeminiCallerBridge


def _bridge(**flags):
    """Minimal stand-in — only methods/_allow_persona_room_audio needs."""
    b = object.__new__(GeminiCallerBridge)
    b._script_hangup_farewell = False
    b._inject_turn_active = False
    b._mute_persona_audio = False
    b._suppress_output_until_mono = None
    b._script_pending = lambda: False
    for k, v in flags.items():
        setattr(b, k, v)
    return b


def test_allow_freestyle_when_no_script():
    b = _bridge()
    assert b._allow_persona_room_audio() is True


def test_mute_freestyle_when_script_pending():
    b = _bridge(_script_pending=lambda: True)
    assert b._allow_persona_room_audio() is False


def test_allow_script_gemini_text_inject_while_pending():
    b = _bridge(_script_pending=lambda: True, _inject_turn_active=True)
    assert b._allow_persona_room_audio() is True


def test_allow_script_hangup_farewell_while_pending():
    b = _bridge(_script_pending=lambda: True, _script_hangup_farewell=True)
    assert b._allow_persona_room_audio() is True


def test_ttl_suppress_still_mutes_without_script():
    import time

    b = _bridge(_suppress_output_until_mono=time.monotonic() + 5)
    assert b._allow_persona_room_audio() is False
