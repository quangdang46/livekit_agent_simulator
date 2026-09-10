"""P0-6: offline TTS engine abstraction + cache.

`TtsEngine` is the interface every backend implements (existing
`sapi_tts.synthesize_pcm16_mono` is OS-dependent and stays as the legacy
fallback; new code should prefer an offline, cross-platform `TtsEngine`
such as `sherpa_tts.SherpaOnnxTtsEngine`).

`TtsCache` is engine-agnostic: it caches PCM bytes on disk keyed by
`hash(model_id + voice + language + text)`, so the SAME (model, voice,
language, text) tuple across Windows/macOS/Linux and across repeated runs
never re-synthesizes. This lets `TtsEngine` implementations be swapped
without touching the caching/publish path (report §3/§19.1).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class TtsEngine(Protocol):
    """Offline TTS backend. Must return PCM16 mono bytes at a fixed sample
    rate; no OS-specific system TTS calls belong behind this interface."""

    model_id: str

    def synthesize(self, text: str, *, voice: str, language: str) -> bytes: ...


def cache_key(*, model_id: str, voice: str, language: str, text: str) -> str:
    """sha256(model_id + voice + language + text) — same tuple always
    yields the same key, across platforms and across runs."""
    payload = f"{model_id}\x1f{voice}\x1f{language}\x1f{text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass
class TtsCache:
    """Per-utterance PCM cache on disk. One file per cache key."""

    cache_dir: Path

    def __post_init__(self) -> None:
        self.cache_dir = Path(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        return self.cache_dir / f"{key}.pcm"

    def get(self, key: str) -> bytes | None:
        path = self.path_for(key)
        if path.exists():
            return path.read_bytes()
        return None

    def put(self, key: str, pcm: bytes) -> None:
        self.path_for(key).write_bytes(pcm)

    def synthesize_cached(self, engine: TtsEngine, text: str, *, voice: str, language: str) -> tuple[bytes, bool]:
        """Returns (pcm_bytes, was_cache_hit)."""
        key = cache_key(model_id=engine.model_id, voice=voice, language=language, text=text)
        cached = self.get(key)
        if cached is not None:
            return cached, True
        pcm = engine.synthesize(text, voice=voice, language=language)
        self.put(key, pcm)
        return pcm, False


__all__ = ["TtsCache", "TtsEngine", "cache_key"]
