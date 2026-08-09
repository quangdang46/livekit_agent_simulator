"""SAPI TTS fallback (Windows only — skips elsewhere)."""

from __future__ import annotations

import sys

import pytest

from livekit_agent_simulator.audio.sapi_tts import TARGET_RATE, synthesize_pcm16_mono


@pytest.mark.skipif(sys.platform != "win32", reason="Windows SAPI only")
def test_synthesize_pcm16_mono_short_phrase():
    pcm = synthesize_pcm16_mono("Wait, what's the monthly fee?")
    assert pcm is not None
    assert len(pcm) > TARGET_RATE  # > 0.5s of mono PCM16
    assert len(pcm) % 2 == 0


def test_synthesize_empty_returns_none():
    assert synthesize_pcm16_mono("") is None
    assert synthesize_pcm16_mono("   ") is None
