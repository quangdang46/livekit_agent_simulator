"""Play short PCM/WAV cues directly into the sim caller mic (what the agent STT hears)."""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from typing import Any

from livekit import rtc

FRAME_MS = 10


def load_wav_pcm(path: Path) -> tuple[bytes, int, int]:
    """Return (pcm16_le_bytes, sample_rate, num_channels)."""
    with wave.open(str(path), "rb") as wf:
        if wf.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16-bit PCM WAV")
        channels = wf.getnchannels()
        rate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    return pcm, rate, channels


def resolve_cue_asset(
    asset: str,
    *,
    scenario_dir: Path | None = None,
    package_root: Path | None = None,
    templates_dir: Path | None = None,
    project_root: Path | None = None,
    cues_config: Any = None,
) -> Path:
    """Resolve a WAV cue (builtin + target override). See ``cue_catalog``."""
    from .cue_catalog import resolve_cue_asset as _resolve

    return _resolve(
        asset,
        scenario_dir=scenario_dir,
        package_root=package_root,
        templates_dir=templates_dir,
        project_root=project_root,
        cues_config=cues_config,
    )


async def play_pcm_to_source(
    source: rtc.AudioSource,
    pcm: bytes,
    *,
    sample_rate: int,
    num_channels: int = 1,
) -> None:
    """Stream PCM16 mono/stereo into LiveKit AudioSource in ~10ms frames."""
    if sample_rate != source.sample_rate:
        raise ValueError(
            f"PCM sample rate {sample_rate} != AudioSource {source.sample_rate} "
            "(resample cue WAV to match sim mic rate)"
        )
    if num_channels != 1:
        raise ValueError("Only mono cue assets are supported")

    bytes_per_sample = 2
    samples_per_frame = max(1, (sample_rate * FRAME_MS) // 1000)
    frame_bytes = samples_per_frame * bytes_per_sample * num_channels
    offset = 0
    while offset < len(pcm):
        chunk = pcm[offset : offset + frame_bytes]
        offset += frame_bytes
        if len(chunk) < frame_bytes:
            chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
        samples = len(chunk) // bytes_per_sample
        frame = rtc.AudioFrame(
            data=chunk,
            sample_rate=sample_rate,
            num_channels=num_channels,
            samples_per_channel=samples,
        )
        await source.capture_frame(frame)
        await asyncio.sleep(FRAME_MS / 1000.0)
    # Do NOT call source.wait_for_playout(): Gemini Live also streams into the same
    # AudioSource, so the queue never drains and wait_for_playout hangs forever.
    # Streaming frames + FRAME_MS sleep already paces real-time playout.
