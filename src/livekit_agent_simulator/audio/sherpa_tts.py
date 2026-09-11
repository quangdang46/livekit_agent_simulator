"""P0-6: sherpa-onnx offline TTS backend (optional extra).

Offline, cross-platform (Windows/macOS/Linux), no API key, no cloud
billing — chosen over OS TTS (`sapi_tts.py`) specifically because the
same (model, voice) pair produces the same audio regardless of platform,
which the report identifies as a requirement for a deterministic-behavior
simulator (NEW_ARCHITECTURE_FOR_LKS_AND_LKSR.md §3).

`sherpa_onnx` itself is an OPTIONAL dependency (see the `tts-sherpa` extra
in pyproject.toml) — importing this module must never fail just because
the package is not installed; only calling `SherpaOnnxTtsEngine.synthesize()`
requires it, and does so via a lazy import with a clear error message.

Model download: pinned URL + SHA256 verification into a per-platform cache
dir. This module defines the download/verify contract; the actual network
fetch is a thin function so tests can inject a fake downloader and never
touch the network.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DEFAULT_SAMPLE_RATE = 24_000


class SherpaOnnxNotInstalledError(RuntimeError):
    """Raised when SherpaOnnxTtsEngine.synthesize() is called but the
    optional `sherpa-onnx` package is not installed. Install with:
    `uv sync --extra tts-sherpa` (or `pip install livekit-agent-simulator[tts-sherpa]`).
    """


@dataclass(frozen=True)
class PinnedModelSpec:
    """Identifies exactly one pinned model release for reproducible audio
    across machines: same model_id + sha256 => same synthesized bytes for
    a given (voice, language, text)."""

    model_id: str
    url: str
    sha256: str
    filename: str


class ModelIntegrityError(RuntimeError):
    """Raised when a downloaded model file's SHA256 does not match the
    pinned spec — never silently accept a corrupted/tampered model."""


def download_and_verify_model(
    spec: PinnedModelSpec,
    cache_dir: Path,
    *,
    downloader: Callable[[str, Path], None],
) -> Path:
    """Download `spec.url` into `cache_dir/spec.filename` (via the injected
    `downloader`, so tests never touch the network) and verify SHA256.
    Returns the verified path; raises ModelIntegrityError on mismatch.
    Idempotent: if the file already exists AND verifies, skips downloading.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / spec.filename

    if not dest.exists():
        downloader(spec.url, dest)

    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    if digest != spec.sha256:
        dest.unlink(missing_ok=True)
        raise ModelIntegrityError(
            f"model {spec.model_id!r} failed SHA256 verification: expected {spec.sha256}, got {digest}"
        )
    return dest


# Voice name -> Kitten speaker id. Kitten nano EN ships 8 voices
# (4 female + 4 male); af_heart (female) is the contract-path default.
# Unknown names fall back to sid 0 (fail-audible, never fail-silent).
VOICE_TO_SID = {
    "af_heart": 0,
    "af_bella": 1,
    "af_nicole": 2,
    "af_sarah": 3,
    "am_adam": 4,
    "am_michael": 5,
    "bf_emma": 6,
    "bm_george": 7,
}


def _voice_to_sid(voice: str) -> int:
    return VOICE_TO_SID.get(str(voice or "").strip().lower(), 0)


@dataclass
class SherpaOnnxTtsEngine:
    """TtsEngine implementation backed by sherpa-onnx OfflineTts (Kitten).

    `model_id` matches the TtsEngine Protocol (see tts_engine.py) so this
    engine can be used interchangeably with TtsCache. `model_path` is the
    extracted model DIRECTORY (model.fp16.onnx + voices.bin + tokens.txt +
    espeak-ng-data/); `voice` selects the Kitten speaker id.
    """

    model_id: str
    model_path: Path
    sample_rate: int = DEFAULT_SAMPLE_RATE
    _offline_tts: object | None = None

    def _ensure_backend(self):
        if self._offline_tts is not None:
            return self._offline_tts
        try:
            import sherpa_onnx
        except ImportError as exc:  # pragma: no cover - exercised only when package absent
            raise SherpaOnnxNotInstalledError(
                "sherpa-onnx is not installed. Install with `uv sync --extra tts-sherpa`."
            ) from exc
        model_dir = Path(self.model_path)
        kitten = sherpa_onnx.OfflineTtsKittenModelConfig(
            model=str(model_dir / "model.fp16.onnx"),
            voices=str(model_dir / "voices.bin"),
            tokens=str(model_dir / "tokens.txt"),
            data_dir=str(model_dir / "espeak-ng-data"),
        )
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kitten=kitten,
                num_threads=2,
                debug=False,
                provider="cpu",
            ),
            max_num_sentences=1,
        )
        if not config.validate():
            raise RuntimeError(
                f"sherpa-onnx Kitten config invalid (model dir {model_dir})"
            )
        self._offline_tts = sherpa_onnx.OfflineTts(config)
        return self._offline_tts

    def synthesize(self, text: str, *, voice: str, language: str) -> bytes:
        """Synthesize text -> PCM16 mono bytes at the engine sample rate.

        Raises SherpaOnnxNotInstalledError (no package) or RuntimeError
        (bad model dir) — the contract path treats every raise as "fall
        back to OS TTS", never as silent audio.
        """
        import array

        if not text or not text.strip():
            raise ValueError("synthesize text must be non-empty")
        backend = self._ensure_backend()
        audio = backend.generate(text, sid=_voice_to_sid(voice), speed=1.0)
        samples = audio.samples
        rate = int(audio.sample_rate or self.sample_rate)
        pcm = array.array("h", (max(-32768, min(32767, int(s * 32768))) for s in samples))
        raw = pcm.tobytes()
        if rate != self.sample_rate and rate > 0:
            # Resample to the engine rate (contract mixer runs at 24k;
            # Kitten native is 24k so this is normally a no-op).
            import audioop

            raw, _ = audioop.ratecv(raw, 2, 1, rate, self.sample_rate, None)
        return bytes(raw)


__all__ = [
    "DEFAULT_SAMPLE_RATE",
    "ModelIntegrityError",
    "PinnedModelSpec",
    "SherpaOnnxNotInstalledError",
    "SherpaOnnxTtsEngine",
    "download_and_verify_model",
]
