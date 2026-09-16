"""P0-6: TTS engine abstraction + cache tests.

Runs fully WITHOUT sherpa-onnx installed — the cache/engine-protocol
contract is TTS-backend-agnostic and is proven here with a fake in-memory
engine. Real sherpa-onnx-specific tests (model download/verify) are in
test_sherpa_tts.py and skip cleanly when the optional package is absent.
"""

from __future__ import annotations

from livekit_agent_simulator.audio.tts_engine import TtsCache, cache_key


class _FakeEngine:
    """Deterministic fake TtsEngine: returns bytes derived from the text,
    so we can assert cache hits reuse the SAME bytes without a real model."""

    model_id = "fake-model-v1"

    def __init__(self) -> None:
        self.calls = 0

    def synthesize(self, text: str, *, voice: str, language: str) -> bytes:
        self.calls += 1
        return f"PCM:{voice}:{language}:{text}".encode("utf-8")


def test_cache_key_deterministic_for_same_tuple() -> None:
    k1 = cache_key(model_id="m1", voice="v1", language="en-US", text="Hi.")
    k2 = cache_key(model_id="m1", voice="v1", language="en-US", text="Hi.")
    assert k1 == k2


def test_cache_key_differs_for_different_text() -> None:
    k1 = cache_key(model_id="m1", voice="v1", language="en-US", text="Hi.")
    k2 = cache_key(model_id="m1", voice="v1", language="en-US", text="Bye.")
    assert k1 != k2


def test_cache_key_differs_across_model_voice_language() -> None:
    base = cache_key(model_id="m1", voice="v1", language="en-US", text="Hi.")
    assert base != cache_key(model_id="m2", voice="v1", language="en-US", text="Hi.")
    assert base != cache_key(model_id="m1", voice="v2", language="en-US", text="Hi.")
    assert base != cache_key(model_id="m1", voice="v1", language="en-GB", text="Hi.")


def test_synthesize_cached_miss_then_hit(tmp_path) -> None:
    cache = TtsCache(cache_dir=tmp_path / "tts_cache")
    engine = _FakeEngine()

    pcm1, hit1 = cache.synthesize_cached(engine, "Hi.", voice="af_heart", language="en-US")
    assert hit1 is False
    assert engine.calls == 1

    pcm2, hit2 = cache.synthesize_cached(engine, "Hi.", voice="af_heart", language="en-US")
    assert hit2 is True
    assert engine.calls == 1, "cache hit must not call the engine again"
    assert pcm1 == pcm2


def test_synthesize_cached_different_text_is_a_new_cache_entry(tmp_path) -> None:
    cache = TtsCache(cache_dir=tmp_path / "tts_cache")
    engine = _FakeEngine()

    cache.synthesize_cached(engine, "Hi.", voice="af_heart", language="en-US")
    cache.synthesize_cached(engine, "Bye.", voice="af_heart", language="en-US")
    assert engine.calls == 2


def test_cache_persists_pcm_bytes_on_disk(tmp_path) -> None:
    cache_dir = tmp_path / "tts_cache"
    cache = TtsCache(cache_dir=cache_dir)
    engine = _FakeEngine()

    cache.synthesize_cached(engine, "Hi.", voice="af_heart", language="en-US")
    files = list(cache_dir.glob("*.pcm"))
    assert len(files) == 1


def test_cache_creates_directory_if_missing(tmp_path) -> None:
    cache_dir = tmp_path / "does" / "not" / "exist" / "yet"
    assert not cache_dir.exists()
    TtsCache(cache_dir=cache_dir)
    assert cache_dir.exists()


def test_no_os_tts_import_in_cache_module() -> None:
    """Structural guard: the cache/engine abstraction must not import any
    OS-specific TTS mechanism (e.g. via sapi_tts) — it is engine-agnostic."""
    import livekit_agent_simulator.audio.tts_engine as mod

    assert "sapi_tts" not in mod.__dict__
    assert "subprocess" not in mod.__dict__
