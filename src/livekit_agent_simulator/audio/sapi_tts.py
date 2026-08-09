"""Optional Windows SAPI → 24 kHz mono PCM16 (Script inject fallback)."""

from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

TARGET_RATE = 24_000


def synthesize_pcm16_mono(text: str, *, rate: int = TARGET_RATE) -> bytes | None:
    """Return PCM16 mono at ``rate``, or None if local TTS is unavailable.

    Windows: SAPI (System.Speech). macOS: built-in ``say`` (AIFF → PCM).
    """
    say_text = (text or "").strip()
    if not say_text:
        return None
    try:
        if sys.platform == "win32":
            with tempfile.TemporaryDirectory(prefix="lks-sapi-") as tmp:
                wav_path = Path(tmp) / "out.wav"
                ps = f"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {{ $synth.SelectVoice('Microsoft Zira Desktop') }} catch {{ }}
$synth.Rate = 0
$synth.SetOutputToWaveFile('{str(wav_path).replace("'", "''")}')
$synth.Speak('{say_text.replace("'", "''")}')
$synth.Dispose()
"""
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                with wave.open(str(wav_path), "rb") as wf:
                    if wf.getsampwidth() != 2:
                        return None
                    pcm = wf.readframes(wf.getnframes())
                    channels = wf.getnchannels()
                    src_rate = wf.getframerate()
                return _to_mono_rate(pcm, channels, src_rate, rate)
        if sys.platform == "darwin":
            # macOS built-in TTS → AIFF → afconvert to WAV PCM16 @ rate.
            with tempfile.TemporaryDirectory(prefix="lks-say-") as tmp:
                aiff_path = Path(tmp) / "out.aiff"
                wav_path = Path(tmp) / "out.wav"
                subprocess.run(
                    ["say", "-o", str(aiff_path), say_text],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                subprocess.run(
                    [
                        "afconvert",
                        "-f", "WAVE",
                        "-d", f"LEI16@{rate}",
                        str(aiff_path),
                        str(wav_path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                with wave.open(str(wav_path), "rb") as wf:
                    if wf.getsampwidth() != 2:
                        return None
                    pcm = wf.readframes(wf.getnframes())
                    channels = wf.getnchannels()
                    src_rate = wf.getframerate()
                return _to_mono_rate(pcm, channels, src_rate, rate)
        return None
    except (OSError, subprocess.SubprocessError, wave.Error, ValueError, ImportError):
        return None


def _to_mono_rate(pcm: bytes, channels: int, src_rate: int, target: int) -> bytes:
    if channels == 2:
        out = bytearray()
        for i in range(0, len(pcm), 4):
            if i + 3 >= len(pcm):
                break
            left, right = struct.unpack_from("<hh", pcm, i)
            out.extend(struct.pack("<h", max(-32768, min(32767, (left + right) // 2))))
        pcm = bytes(out)
        channels = 1
    if channels != 1:
        raise ValueError(f"unsupported channels={channels}")
    if src_rate == target:
        return pcm
    n_in = len(pcm) // 2
    n_out = max(1, int(n_in * target / src_rate))
    samples = struct.unpack(f"<{n_in}h", pcm)
    out_samples: list[int] = []
    for i in range(n_out):
        src = i * (n_in - 1) / max(1, n_out - 1)
        j = int(src)
        frac = src - j
        a = samples[j]
        b = samples[min(j + 1, n_in - 1)]
        out_samples.append(int(a + (b - a) * frac))
    return struct.pack(f"<{n_out}h", *out_samples)
