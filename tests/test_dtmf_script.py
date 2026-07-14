"""P1.A — Script action dtmf (draft: unit-level; worker may lack GetDtmfTask)."""

from __future__ import annotations

import pytest

from livekit_agent_simulator.asserts import evaluate_asserts, parse_assert_spec
from livekit_agent_simulator.script_parse import parse_script_steps


def test_parse_dtmf_step():
    steps = parse_script_steps(
        {
            "steps": [
                {
                    "id": "pin",
                    "action": "dtmf",
                    "digits": "1234#",
                    "trigger": "time",
                    "delay_ms": 500,
                }
            ]
        },
        "t",
    )
    s = steps[0]
    assert s.action == "dtmf"
    assert s.dtmf_digits == "1234#"
    assert s.interrupt_class == "dtmf"
    assert s.say.startswith("[dtmf:")


def test_parse_dtmf_rejects_bad_chars():
    with pytest.raises(ValueError, match="invalid DTMF"):
        parse_script_steps(
            {"steps": [{"id": "x", "action": "dtmf", "digits": "12A", "trigger": "time"}]},
            "t",
        )


def test_parse_dtmf_requires_digits():
    with pytest.raises(ValueError, match="requires digits"):
        parse_script_steps(
            {"steps": [{"id": "x", "action": "dtmf", "trigger": "time"}]},
            "t",
        )


def test_assert_dtmf_sequence():
    events = [
        {"kind": "sim.dtmf", "ts_mono_ms": 1, "spec": {"digits": "1w2w3#", "sent": "123#"}},
    ]
    spec = parse_assert_spec({"sip": {"dtmf_sequence": "123#"}})
    out = evaluate_asserts(events, spec)
    assert out["pass"] is True
    assert any(c["check"] == "sip_dtmf_sequence" and c["pass"] for c in out["checks"])


def test_assert_dtmf_sequence_fail():
    events = [{"kind": "sim.dtmf", "spec": {"sent": "99"}}]
    spec = parse_assert_spec({"sip": {"dtmf_sequence": "123"}})
    out = evaluate_asserts(events, spec)
    assert out["pass"] is False
