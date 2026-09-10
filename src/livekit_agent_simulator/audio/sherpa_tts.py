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


@dataclass
class SherpaOnnxTtsEngine:
    """TtsEngine implementation backed by sherpa-onnx OfflineTts.

    `model_id` matches the TtsEngine Protocol (see tts_engine.py) so this
    engine can be used interchangeably with TtsCache.
    """

    model_id: str
    model_path: Path
    sample_rate: int = DEFAULT_SAMPLE_RATE
    _offline_tts: object | None = None

    def _ensure_backend(self):
        if self._offline_tts is not None:
            return self._offline_tts
        try:
            import sherpa_onnx  # noqa: F401  (lazy import; optional dependency)
        except ImportError as exc:  # pragma: no cover - exercised only when package absent
            raise SherpaOnnxNotInstalledError(
                "sherpa-onnx is not installed. Install with `uv sync --extra tts-sherpa`."
            ) from exc
        # Real config construction (OfflineTtsConfig / OfflineTts.create) is
        # intentionally left as a documented next step for the actual model
        # wiring — see docs/tts-benchmark.md for the current benchmark
        # status. This module's contract (TtsEngine.synthesize signature,
        # caching, SHA verification) is what's under test today.
        raise NotImplementedError(
            "SherpaOnnxTtsEngine backend wiring pending model benchmark (see docs/tts-benchmark.md)"
        )

    def synthesize(self, text: str, *, voice: str, language: str) -> bytes:
        backend = self._ensure_backend()
        raise NotImplementedError  # pragma: no cover - see _ensure_backend


__all__ = [
    "DEFAULT_SAMPLE_RATE",
    "ModelIntegrityError",
    "PinnedModelSpec",
    "SherpaOnnxNotInstalledError",
    "SherpaOnnxTtsEngine",
    "download_and_verify_model",
]
