"""P1.L — web markers expose Hamming interrupt class chips."""

from __future__ import annotations

from livekit_agent_simulator.web.markers import _build_markers


def test_markers_include_class_and_types():
    events = [
        {
            "kind": "sim.script.cue",
            "ts_mono_ms": 1000,
            "spec": {
                "barge_in": True,
                "class": "correction",
                "label": "cut",
                "say": "Wait",
                "during_agent_speech": True,
                "trigger": "agent_speaking",
            },
        },
        {
            "kind": "sim.script.cue",
            "ts_mono_ms": 2000,
            "spec": {
                "barge_in": False,
                "class": "backchannel",
                "label": "uh",
                "say": "uh-huh",
                "during_agent_speech": True,
                "trigger": "agent_speaking",
            },
        },
        {
            "kind": "sim.script.cue",
            "ts_mono_ms": 3000,
            "spec": {
                "barge_in": True,
                "class": "noise",
                "label": "clk",
                "say": "[noise]",
                "during_agent_speech": True,
                "trigger": "agent_speaking",
            },
        },
    ]
    markers = _build_markers(events, t0=0, duration_ms=60000)
    by_type = {}
    for m in markers:
        by_type.setdefault(m["type"], m)
    assert by_type["barge_in"]["class"] == "correction"
    assert by_type["backchannel"]["class"] == "backchannel"
    assert by_type["false_interrupt"]["class"] == "noise"
    # noise should not seed recovery points only — at least types present
    assert "💬" in by_type["backchannel"]["label"] or "backchannel" in by_type["backchannel"]["detail"]
