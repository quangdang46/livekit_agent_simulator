"""audio_onset marker coalescing — one marker per agent speech burst.

The RMS onset detector re-arms on short intra-utterance pauses (~100ms
exit_frames), so a single agent turn can emit many `sim.agent.audio_onset`
events. The web layer coalesces onsets closer than AUDIO_ONSET_BURST_MS so the
report player shows one "agent audio onset" per utterance instead of flooding
the transcript (observed 483 onsets for ~55 utterances in a real run).

Only affects the web marker list — the TTFA / turn_taking_audio metrics read the
raw `sim.agent.audio_onset` events directly and are untouched.
"""

from __future__ import annotations

from livekit_agent_simulator.web.markers import (
    AUDIO_ONSET_BURST_MS,
    AUDIO_ONSET_MAX_SPAN_MS,
    MARKER_AUDIO_ONSET,
    _build_markers,
    _coalesce_audio_onsets,
)


def _onset(ms: int, dur: int = 300) -> dict:
    return {
        "type": MARKER_AUDIO_ONSET,
        "start_ms": ms,
        "end_ms": ms + dur,
        "label": "agent audio onset",
        "detail": f"onset_frame={ms} · vad=rms",
    }


def _onset_events(ms_list: list[int]) -> list[dict]:
    return [
        {
            "kind": "sim.agent.audio_onset",
            "ts_mono_ms": ms,
            "spec": {"onset_frame_idx": ms, "vad": {"method": "rms"}},
        }
        for ms in ms_list
    ]


def test_no_onsets_passthrough():
    markers = [{"type": "silence", "start_ms": 100, "end_ms": 500}]
    assert _coalesce_audio_onsets(markers, None) == markers


def test_single_onset_unchanged():
    markers = [_onset(1000)]
    out = _coalesce_audio_onsets(markers, None)
    assert len(out) == 1
    assert out[0]["start_ms"] == 1000


def test_dense_burst_coalesces_to_one():
    # 10 onsets at 500ms apart (~4.5s span, within the max-span cap) — collapse.
    onsets = [1000 + i * 500 for i in range(10)]  # 500ms apart, same burst
    markers = [_onset(ms) for ms in onsets]
    out = _coalesce_audio_onsets(markers, None)
    assert len(out) == 1
    assert out[0]["start_ms"] == 1000  # earliest onset kept
    assert out[0]["end_ms"] >= onsets[-1]  # spans the burst


def test_separate_bursts_stay_separate():
    # Two utterances far apart (> burst window) → two markers.
    markers = [_onset(1000), _onset(1000 + AUDIO_ONSET_BURST_MS + 5000)]
    out = _coalesce_audio_onsets(markers, None)
    assert len(out) == 2


def test_build_markers_coalesces_dense_onsets():
    events = _onset_events([1000, 1500, 2000, 2500, 3000]) + [
        {
            "kind": "sim.agent.audio_onset",
            "ts_mono_ms": 20000,
            "spec": {"onset_frame_idx": 20000, "vad": {"method": "rms"}},
        }
    ]
    markers = _build_markers(events, t0=0, duration_ms=60000)
    ao = [m for m in markers if m["type"] == MARKER_AUDIO_ONSET]
    # 5 dense (1s apart → same burst) + 1 isolated → 2 markers total.
    assert len(ao) == 2, [m["start_ms"] for m in ao]
    assert ao[0]["start_ms"] == 1000
    assert ao[1]["start_ms"] == 20000


def test_other_marker_types_preserved():
    markers = [_onset(1000), _onset(1500), {"type": "silence", "start_ms": 9000, "end_ms": 9500}]
    out = _coalesce_audio_onsets(markers, None)
    types = [m["type"] for m in out]
    assert types.count(MARKER_AUDIO_ONSET) == 1
    assert "silence" in types


def test_marker_counts_reflect_coalescing():
    events = _onset_events([1000, 1500, 2000, 30000, 30500, 60000])
    markers = _build_markers(events, t0=0, duration_ms=90000)
    ao = [m for m in markers if m["type"] == MARKER_AUDIO_ONSET]
    # bursts: [1000,1500,2000] and [30000,30500] and [60000] → 3
    assert len(ao) == 3


def test_long_chain_breaks_at_max_span():
    # Onsets every 2s for 30s: chaining ≤ burst gap would collapse into one
    # giant 30s marker. The max-span cap must break the chain so no single
    # marker spans > AUDIO_ONSET_MAX_SPAN_MS.
    onsets = [1000 + i * 2000 for i in range(16)]  # 1s→31s
    markers = [_onset(ms) for ms in onsets]
    out = _coalesce_audio_onsets(markers, None)
    ao = [m for m in out if m["type"] == MARKER_AUDIO_ONSET]
    assert len(ao) > 1, "long chain must not collapse to one marker"
    for m in ao:
        assert m["end_ms"] - m["start_ms"] <= AUDIO_ONSET_MAX_SPAN_MS + 2000
    # First marker keeps the earliest onset.
    assert ao[0]["start_ms"] == 1000
