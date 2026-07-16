"""Player cue dedupe: ASR typo tolerance + dual transcript sources."""

from __future__ import annotations

from livekit_agent_simulator.web.cue_helpers.source_priority import (
    source_rank,
    texts_similar,
)
from livekit_agent_simulator.web.transcript_cues import _build_transcript_cues


def test_texts_similar_asr_typos_okay_thanks():
    assert texts_similar("Okey. Thank's.", "Okay. Thanks.")
    assert texts_similar("Okay, thanks. Bye.", "Okay. Thanks.")
    assert not texts_similar("What's the monthly fee?", "Okay. Thanks.")


def test_source_rank_prefers_worker_topic_over_lk_for_user():
    assert source_rank("voice_ai.transcript", "user") < source_rank("lk.transcription", "user")


def test_build_transcript_cues_collapses_dual_stt_bye():
    t0 = 8000
    events = [
        {
            "kind": "transcript.user.final",
            "source": "lk.transcription",
            "ts_mono_ms": t0 + 92200,
            "spec": {"text": "Okey. Thank's."},
        },
        {
            "kind": "transcript.user.final",
            "source": "voice_ai.transcript",
            "ts_mono_ms": t0 + 93100,
            "spec": {"text": "Okay. Thanks."},
        },
    ]
    cues = _build_transcript_cues(events, t0=t0, duration_ms=94000)
    user = [c for c in cues if c["role"] == "user"]
    assert len(user) == 1
    # Prefer worker topic (cleaner) over LK ASR garble when ranks differ.
    assert "Okay" in user[0]["text"] or "okay" in user[0]["text"].lower()
