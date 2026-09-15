# TTS benchmark status (P0-6)

Per `NEW_ARCHITECTURE_FOR_LKS_AND_LKSR.md` §3, the choice between Kokoro
and Kitten (both runnable via `sherpa-onnx`) should be based on cold/warm
startup time, RAM, RTF, first-audio latency, and install size — not
voice-quality MOS alone.

## Current status

**Not yet benchmarked in this environment.** This development environment
has no network access to fetch pinned ONNX models, so no real
cold-start/RTF/RAM numbers are recorded here yet. Fabricating numbers
would be worse than admitting the gap — do not fill this table with
guessed values.

What IS implemented and tested (see `src/livekit_agent_simulator/audio/`):

- `tts_engine.py`: `TtsEngine` protocol + `TtsCache` (hash-based
  per-utterance PCM cache, engine-agnostic, fully unit-tested without any
  TTS backend installed).
- `sherpa_tts.py`: `SherpaOnnxTtsEngine` skeleton, `PinnedModelSpec` +
  `download_and_verify_model()` (SHA256-verified model download,
  independent of the `sherpa-onnx` package itself and fully unit-tested
  with an injected fake downloader).
- `scripts/benchmark_tts.py`: the entry point for the real benchmark once
  a network-enabled environment with the `tts-sherpa` extra installed is
  available.

## Next steps (tracked, not fabricated)

1. Pin exact Kokoro and Kitten model URLs + SHA256 in a `PinnedModelSpec`
   registry.
2. Wire `SherpaOnnxTtsEngine._ensure_backend()` to actually construct
   `sherpa_onnx.OfflineTts` (currently raises `NotImplementedError` with a
   pointer back to this file).
3. Run `scripts/benchmark_tts.py` on Windows x64, macOS ARM64, and Linux
   x64; fill in the table below with real numbers.

| Model | Cold start | Warm start | First-audio latency | RSS | RTF | Install size |
|---|---|---|---|---|---|---|
| Kokoro (int8) | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ |
| Kitten | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ |
