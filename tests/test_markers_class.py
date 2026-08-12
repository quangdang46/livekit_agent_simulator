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


def test_markers_include_audio_onset_and_user_source() -> None:
    events = [
        {
            "kind": "sim.caller.audio_source_start",
            "ts_mono_ms": 500,
            "spec": {"provider": "gemini", "via": "freestyle_tts"},
        },
        {
            "kind": "sim.agent.audio_onset",
            "ts_mono_ms": 1200,
            "spec": {"onset_frame_idx": 3200, "vad": {"method": "rms"}},
        },
    ]
    markers = _build_markers(events, t0=0, duration_ms=10000)
    types = [m["type"] for m in markers]
    assert "user_audio_source" in types
    assert "audio_onset" in types
    onset = next(m for m in markers if m["type"] == "audio_onset")
    assert onset["start_ms"] == 1200
    assert onset["onset_frame_idx"] == 3200
    src = next(m for m in markers if m["type"] == "user_audio_source")
    assert src["start_ms"] == 500
