"""Audio degradation effects — Persona.speech_conditions.effects → caller mic.

Mirrors the LangWatch Scenario ``effects`` pipeline idea (noise/prosody/quality)
but kept minimal: packet loss / breaking up / echo / phone quality / static as
pure per-frame PCM transforms applied post-mix in ``ParallelMicMixer``.
"""

from __future__ import annotations

import array
from pathlib import Path

import pytest

from livekit_agent_simulator.audio.degradation import (
    SUPPORTED_EFFECTS,
    effects_spec_from_persona,
    resolve_audio_effects,
)
from livekit_agent_simulator.audio.mic_mixer import ParallelMicMixer
from livekit_agent_simulator.scenario import parse_scenario

EX = Path(__file__).resolve().parents[1] / "templates" / "examples"


class _FakeSrc:
    sample_rate = 24_000

    def __init__(self) -> None:
        self.frames: list[bytes] = []

    async def capture_frame(self, frame) -> None:  # noqa: ANN001
        self.frames.append(frame.data)


def _speech_pcm(n: int, value: int = 1000) -> bytes:
    return array.array("h", [value] * n).tobytes()


@pytest.mark.asyncio
async def _run_mixer(*, effects: list, speech_frames: int = 2) -> tuple[_FakeSrc, bytes]:
    src = _FakeSrc()
    mixer = ParallelMicMixer(
        src,
        sample_rate=24_000,
        frame_ms=10,
        speech_preroll_ms=0,
        effects=effects,
    )  # type: ignore[arg-type]
    n = mixer.frame_samples
    mixer.begin_speech_turn()
    mixer.push_speech(_speech_pcm(n * speech_frames))
    mixer.end_speech_turn()
    mixer.start()
    try:
        await mixer.wait_speech_drain(timeout_s=0.5)
    finally:
        await mixer.aclose()
    return src, b"".join(src.frames)


def test_degraded_call_template_parses() -> None:
    s = parse_scenario(EX / "degraded-call.yaml")
    assert s.id == "degraded-call"
    fx = resolve_audio_effects(s.persona)
    assert len(fx) == 2  # packet_loss + phone_quality


def test_supported_effects_known() -> None:
    assert set(SUPPORTED_EFFECTS) == {
        "packet_loss",
        "breaking_up",
        "echo",
        "phone_quality",
        "static",
    }


def test_effects_spec_from_persona() -> None:
    persona = {"speech_conditions": {"effects": {"echo": {"delay_ms": 100}}}}
    assert effects_spec_from_persona(persona) == {"echo": {"delay_ms": 100}}
    assert effects_spec_from_persona(None) is None
    assert effects_spec_from_persona({}) is None
    # camelCase alias
    assert effects_spec_from_persona({"speechConditions": {"effects": ["static"]}}) == ["static"]


def test_resolve_empty() -> None:
    assert resolve_audio_effects(None) == []
    assert resolve_audio_effects({}) == []
    assert resolve_audio_effects({"speech_conditions": {}}) == []


def test_resolve_list_and_dict() -> None:
    list_chain = resolve_audio_effects({"speech_conditions": {"effects": ["static", "phone_quality"]}})
    assert len(list_chain) == 2
    dict_chain = resolve_audio_effects(
        {"speech_conditions": {"effects": {"packet_loss": {"probability": 0.1}, "echo": {}}}}
    )
    assert len(dict_chain) == 2


def test_resolve_unknown_effect_raises() -> None:
    with pytest.raises(ValueError, match="Unknown audio effect"):
        resolve_audio_effects({"speech_conditions": {"effects": {"bogus": {}}}})


def test_effect_outputs_preserve_length() -> None:
    pcm = _speech_pcm(2400)
    for fx in resolve_audio_effects({"speech_conditions": {"effects": list(SUPPORTED_EFFECTS)}}):
        assert len(fx(pcm)) == len(pcm), f"length changed for {fx}"


def test_packet_loss_zeroes_some_samples() -> None:
    pcm = _speech_pcm(4800)
    out = resolve_audio_effects({"speech_conditions": {"effects": {"packet_loss": {"probability": 1.0}}}})[0](pcm)
    samples = array.array("h", out)
    assert any(s == 0 for s in samples)


def test_echo_extends_signal() -> None:
    pcm = _speech_pcm(2400)
    fx = resolve_audio_effects({"speech_conditions": {"effects": {"echo": {"decay": 0.5}}}})[0]
    out = array.array("h", fx(pcm))
    assert out[-1] != 0  # tail carries the delayed copy


@pytest.mark.asyncio
async def test_mixer_applies_effects_to_written_frames() -> None:
    # With packet_loss probability=1.0 every window is zeroed → written frames
    # differ from the raw mixed speech.
    src, written = await _run_mixer(
        effects=resolve_audio_effects(
            {"speech_conditions": {"effects": {"packet_loss": {"probability": 1.0}}}}
        )
    )
    assert written
    samples = array.array("h", written)
    assert any(s == 0 for s in samples)


@pytest.mark.asyncio
async def test_no_effects_mixer_still_writes_speech() -> None:
    src, written = await _run_mixer(effects=[])
    assert written
    samples = array.array("h", written)
    assert any(s != 0 for s in samples)
