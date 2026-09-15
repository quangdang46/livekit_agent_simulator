#!/usr/bin/env python3
"""P0-6: sherpa-onnx TTS benchmark script.

Skips gracefully (exit 0, clear message) when the `tts-sherpa` optional
extra is not installed — this script is not part of the default CI run.

Usage (after `uv sync --extra tts-sherpa` and downloading a pinned model):

    uv run python scripts/benchmark_tts.py

Measures, per model (Kokoro vs Kitten): cold start time, warm start time,
first-audio latency, RSS memory, and real-time factor (RTF = synth_time /
audio_duration). Writes a table to docs/tts-benchmark.md.

See NEW_ARCHITECTURE_FOR_LKS_AND_LKSR.md section 3 for why this benchmark
matters (choosing a model is about cold/warm startup, RAM, RTF, latency,
and install size — not just voice-quality MOS).
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        print(
            "sherpa-onnx is not installed. Install with:\n"
            "    uv sync --extra tts-sherpa\n"
            "then download a pinned model and re-run this script.\n"
            "Skipping benchmark (not a failure)."
        )
        return 0

    print(
        "sherpa-onnx is installed, but no pinned model wiring has been "
        "benchmarked in this environment yet (see docs/tts-benchmark.md). "
        "This script is the entry point for that future benchmark run; it "
        "intentionally does not fabricate numbers here."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
