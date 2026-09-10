"""Pinned sherpa-onnx model registry for the contract-path TTS backend.

One pinned release (Kitten EN, ~25MB — the smaller of the two benchmark
candidates) so every machine with the ``tts-sherpa`` extra synthesizes the
SAME bytes for the same (voice, text). Kokoro stays a benchmark candidate
(see docs/tts-benchmark.md); promoting it means adding a second spec here
plus a selection knob — not silently swapping this default.

SHA256 values are verified by ``download_and_verify_model``; a mismatch
deletes the file and raises ``ModelIntegrityError`` (which the contract
path treats as "fall back to OS TTS", never as silent corruption).
"""

from __future__ import annotations

from .sherpa_tts import PinnedModelSpec

# Kitten nano EN (kittentts_nano_en) — sherpa-onnx community build.
# URL + SHA pinned 2026-09-11; re-pin deliberately (never float `latest`).
_KITTEN_NANO_EN = PinnedModelSpec(
    model_id="kitten-nano-en-v1",
    url=(
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
        "tts-models/kittentts_nano_en.onnx"
    ),
    # NOTE: placeholder until scripts/benchmark_tts.py records the real
    # digest in a network-enabled environment. download_and_verify_model
    # will reject the file (fail-closed) rather than accept a wrong hash —
    # so a wrong value here degrades to OS-TTS fallback, never to silent
    # wrong-audio. Replace with the measured SHA256 when benchmarked.
    sha256="0" * 64,
    filename="kittentts_nano_en.onnx",
)

_DEFAULT_VOICE = "af_heart"
_DEFAULT_LANGUAGE = "en-US"


def default_model_spec() -> PinnedModelSpec:
    """The production pinned model for contract-path TTS."""
    return _KITTEN_NANO_EN


def default_voice() -> tuple[str, str]:
    """(voice, language) defaults for contract-path synthesis."""
    return _DEFAULT_VOICE, _DEFAULT_LANGUAGE


__all__ = ["default_model_spec", "default_voice"]
