"""Generate portable EN voice.* interrupt WAVs (24 kHz mono PCM16) via Windows SAPI.

Run on Windows with System.Speech installed:

  .venv\\Scripts\\python.exe scripts/generate_voice_interrupt_cues.py

Does not require ffmpeg. Vietnamese vocal cues: keep / extend existing
``barge_wait_vi.wav`` (SAPI has no vi-VN voice on this machine).
"""

from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CUES = ROOT / "templates" / "cues"
TARGET_RATE = 24_000

# Hamming-aligned vocal interrupt classes (portable English phrases).
PHRASES: dict[str, str] = {
    "barge_correction_en.wav": "No wait - I meant next Friday.",
    "barge_escalate_en.wav": "Stop. I need to speak with a human please.",
    "barge_soft_en.wav": "Um, hang on one second please.",
    "backchannel_yeah_en.wav": "Yeah. Okay. Mhm. Yeah. Okay.",
}


def _sapi_to_wav(text: str, out_path: Path, rate: int = 0) -> None:
    """Speak text to out_path using Windows SAPI (Zira preferred)."""
    ps = f"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {{ $synth.SelectVoice('Microsoft Zira Desktop') }} catch {{ }}
$synth.Rate = {rate}
$synth.SetOutputToWaveFile('{str(out_path).replace("'", "''")}')
$synth.Speak('{text.replace("'", "''")}')
$synth.Dispose()
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        check=True,
        capture_output=True,
        text=True,
    )


def _read_wav(path: Path) -> tuple[bytes, int, int]:
    with wave.open(str(path), "rb") as w:
        assert w.getsampwidth() == 2
        return w.readframes(w.getnframes()), w.getnchannels(), w.getframerate()


def _resample_pcm16_mono(pcm: bytes, channels: int, rate: int, target: int) -> bytes:
    if channels == 2:
        # average L/R
        out = bytearray()
        for i in range(0, len(pcm), 4):
            if i + 3 >= len(pcm):
                break
            l, r = struct.unpack_from("<hh", pcm, i)
            out.extend(struct.pack("<h", max(-32768, min(32767, (l + r) // 2))))
        pcm = bytes(out)
        channels = 1
    if channels != 1:
        raise ValueError(f"unsupported channels={channels}")
    if rate == target:
        return pcm
    # Linear resample
    n_in = len(pcm) // 2
    n_out = max(1, int(n_in * target / rate))
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


def _write_wav(path: Path, pcm: bytes, rate: int = TARGET_RATE) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def _stack_wav(src: Path, dest: Path, times: int = 3, gap_ms: int = 120) -> None:
    """Repeat an existing cue with short silence gaps (VI backchannel sustain)."""
    pcm, ch, rate = _read_wav(src)
    mono = _resample_pcm16_mono(pcm, ch, rate, TARGET_RATE)
    gap = b"\x00\x00" * int(TARGET_RATE * gap_ms / 1000)
    parts = [mono]
    for _ in range(times - 1):
        parts.extend([gap, mono])
    _write_wav(dest, b"".join(parts))


def main() -> int:
    CUES.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        print("Windows SAPI required for EN generation", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for fname, text in PHRASES.items():
            raw = tmp_path / fname
            print(f"SAPI -> {fname}: {text!r}")
            _sapi_to_wav(text, raw, rate=-1 if "soft" in fname else 0)
            pcm, ch, rate = _read_wav(raw)
            out = _resample_pcm16_mono(pcm, ch, rate, TARGET_RATE)
            _write_wav(CUES / fname, out)
            print(f"  wrote {CUES / fname} ({len(out) // 2 / TARGET_RATE:.2f}s)")

    # VI: lengthen existing barge_wait_vi into sustained backchannel + longer barge.
    vi = CUES / "barge_wait_vi.wav"
    if vi.is_file():
        _stack_wav(vi, CUES / "backchannel_vi.wav", times=4, gap_ms=180)
        print(f"  wrote {CUES / 'backchannel_vi.wav'} (stacked from barge_wait_vi)")
        _stack_wav(vi, CUES / "barge_long_vi.wav", times=2, gap_ms=80)
        print(f"  wrote {CUES / 'barge_long_vi.wav'} (stacked from barge_wait_vi)")

    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
