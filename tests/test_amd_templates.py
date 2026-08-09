"""P1.B — AMD/voicemail portable templates parse."""

from __future__ import annotations

from pathlib import Path

from livekit_agent_simulator.scenario import parse_scenario

EX = Path(__file__).resolve().parents[1] / "templates" / "examples"


def test_voicemail_template():
    s = parse_scenario(EX / "amd-voicemail-greeting.jsonl")
    assert any(st.action == "wait" for st in s.script_steps)


def test_silent_template():
    s = parse_scenario(EX / "amd-silent-caller.jsonl")
    assert "silent" in (s.persona.get("traits") or [])


def test_slow_pickup_template():
    s = parse_scenario(EX / "amd-slow-pickup.jsonl")
    assert any("picked up" in (st.say or "").lower() for st in s.script_steps)
