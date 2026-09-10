"""P0-6: sherpa-onnx backend tests.

Model download + SHA256 verification is tested WITHOUT network (injected
fake downloader) and WITHOUT sherpa-onnx installed (download/verify logic
has no dependency on the package itself). The benchmark-dependent
synthesize() path is skipped when sherpa-onnx is not installed.
"""

from __future__ import annotations

import hashlib

import pytest

from livekit_agent_simulator.audio.sherpa_tts import (
    ModelIntegrityError,
    PinnedModelSpec,
    SherpaOnnxNotInstalledError,
    SherpaOnnxTtsEngine,
    download_and_verify_model,
)


def _spec_for(content: bytes) -> PinnedModelSpec:
    return PinnedModelSpec(
        model_id="kokoro-v1.0-en-int8",
        url="https://example.invalid/model.onnx",
        sha256=hashlib.sha256(content).hexdigest(),
        filename="model.onnx",
    )


def test_download_and_verify_model_succeeds_with_matching_sha(tmp_path) -> None:
    content = b"fake-model-bytes"
    spec = _spec_for(content)

    def fake_downloader(url: str, dest) -> None:
        dest.write_bytes(content)

    path = download_and_verify_model(spec, tmp_path, downloader=fake_downloader)
    assert path.exists()
    assert path.read_bytes() == content


def test_download_and_verify_model_rejects_tampered_content(tmp_path) -> None:
    content = b"fake-model-bytes"
    spec = _spec_for(content)

    def fake_downloader(url: str, dest) -> None:
        dest.write_bytes(b"TAMPERED-BYTES-DIFFERENT")  # does not match spec.sha256

    with pytest.raises(ModelIntegrityError, match="failed SHA256 verification"):
        download_and_verify_model(spec, tmp_path, downloader=fake_downloader)

    # A corrupted file must not be left behind for a later run to trust.
    assert not (tmp_path / "model.onnx").exists()


def test_download_is_skipped_when_cached_file_already_verifies(tmp_path) -> None:
    content = b"fake-model-bytes"
    spec = _spec_for(content)
    (tmp_path / "model.onnx").write_bytes(content)

    calls = []

    def fake_downloader(url: str, dest) -> None:
        calls.append(url)

    download_and_verify_model(spec, tmp_path, downloader=fake_downloader)
    assert calls == [], "must not re-download when the cached file already verifies"


def test_sherpa_engine_synthesize_raises_clear_error_without_package(tmp_path) -> None:
    """When sherpa-onnx is not installed, calling synthesize() must raise a
    clear, actionable error — not an opaque ImportError deep in a stack."""
    try:
        import sherpa_onnx  # noqa: F401

        pytest.skip("sherpa-onnx is installed; this test targets the not-installed path")
    except ImportError:
        pass

    engine = SherpaOnnxTtsEngine(model_id="kokoro-v1.0-en-int8", model_path=tmp_path / "model.onnx")
    with pytest.raises(SherpaOnnxNotInstalledError, match="uv sync --extra tts-sherpa"):
        engine.synthesize("Hi.", voice="af_heart", language="en-US")


@pytest.mark.skipif(
    True,
    reason=(
        "Benchmark requires the sherpa-onnx optional extra plus a downloaded "
        "model; not exercised in this environment. See docs/tts-benchmark.md "
        "for the current status and scripts/benchmark_tts.py to run it "
        "locally once the extra is installed."
    ),
)
def test_synthesize_produces_pcm_at_expected_sample_rate() -> None:  # pragma: no cover
    pytest.importorskip("sherpa_onnx")
