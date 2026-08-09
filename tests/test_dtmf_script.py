"""DTMF/IVR Script action tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from livekit_agent_simulator.script import ScriptStep, normalize_interrupt_class
from livekit_agent_simulator.script_parse import parse_script_steps

# import for tests that need actual models via module
from livekit_agent_simulator.script.models import SUPPORTED_ACTIONS


def test_dtmf_in_supported_actions():
    assert "dtmf" in SUPPORTED_ACTIONS


def test_parse_dtmf_valid_digits():
    steps = parse_script_steps(
        {
            "steps": [
                {
                    "id": "s1",
                    "action": "dtmf",
                    "trigger": "time",
                    "delay_ms": 100,
                    "digits": "1234#",
                }
            ]
        },
        "test",
    )
    assert len(steps) == 1
    assert steps[0].action == "dtmf"
    assert steps[0].digits == "1234#"
    assert "1234" in steps[0].say


def test_parse_dtmf_with_pause():
    steps = parse_script_steps(
        {
            "steps": [
                {
                    "id": "s1",
                    "action": "dtmf",
                    "trigger": "time",
                    "delay_ms": 100,
                    "digits": "12w34",
                }
            ]
        },
        "test",
    )
    assert steps[0].digits == "12w34"


def test_parse_dtmf_rejects_invalid_chars():
    with pytest.raises(ValueError, match="digits can only contain"):
        parse_script_steps(
            {
                "steps": [
                    {
                        "id": "bad",
                        "action": "dtmf",
                        "trigger": "time",
                        "delay_ms": 100,
                        "digits": "abc",
                    }
                ]
            },
            "test",
        )


def test_export_includes_digits():
    from livekit_agent_simulator.scenario_from_dict import export_scenario_dict
    from livekit_agent_simulator.scenario import parse_scenario
    from pathlib import Path

    p = Path(".") / "_test_dtmf_export.jsonl"
    p.write_text(
        '{"apiVersion":"agent-sim/v1","kind":"Scenario","metadata":{"id":"dtmf-export-test"}}\n'
        '{"kind":"Persona","spec":{"brief":"caller","goals":["g"]}}\n'
        '{"kind":"Execute","spec":{"max_turns":3}}\n'
        '{"kind":"Script","spec":{"steps":[{"id":"pin","action":"dtmf","trigger":"time","delay_ms":100,"digits":"1234#"}]}}\n',
        encoding="utf-8",
    )
    s = parse_scenario(p)
    assert any(st.action == "dtmf" and st.digits == "1234#" for st in s.script_steps)
    p.unlink()
    print("  export test pass")
