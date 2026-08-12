"""RmsOnsetDetector — stateful RMS onset detection on the agent R channel.

Spec-locked behaviors under test:
- silence → 0 onset
- single speech → 1 onset
- continuous speech → 1 onset
- speech → silence → speech → 2 onsets
- short noise burst → 0 onset
- below-threshold audio → 0 onset
- threshold boundary → deterministic
- chunk boundary → same onsets regardless of push() chunking
- onset timestamp backdates to the first energy window (not detection time)
"""

from __future__ import annotations

import array

import pytest

from livekit_agent_simulator.audio.vad import (
    RmsOnsetDetector,
    onset_to_audio_ms,
    pcm16_mono_rms,
)


def _sig(values: list[int]) -> bytes:
    return array.array("h", values).tobytes()


def _detector(**kw) -> RmsOnsetDetector:
    defaults = dict(
        sample_rate=1000,  # 1 sample = 1 ms, easy math
        win_ms=20,
        threshold=0.012,
        energy_frames=3,
        exit_frames=5,
        refractory_ms=60,
    )
    defaults.update(kw)
    return RmsOnsetDetector(**defaults)


def _run_onsets(pcm: bytes, **kw) -> list[int]:
    onsets: list[int] = []
    d = _detector(on_onset=onsets.append, **kw)
    d.push(pcm)
    d.flush()
    return onsets


# ---------------------------------------------------------------- helpers


def test_pcm16_mono_rms() -> None:
    assert pcm16_mono_rms(b"") == 0.0
    # Full-scale constant → normalized RMS 1.0
    assert pcm16_mono_rms(array.array("h", [32767] * 4).tobytes()) > 0.99
    # value 1000 → ~1000/32768
    rms = pcm16_mono_rms(array.array("h", [1000] * 4).tobytes())
    assert abs(rms - 1000 / 32768) < 1e-3
    assert pcm16_mono_rms(array.array("h", [0] * 4).tobytes()) == 0.0


def test_onset_to_audio_ms() -> None:
    assert onset_to_audio_ms(0, 16000) == 0
    assert onset_to_audio_ms(16000, 16000) == 1000
    assert onset_to_audio_ms(800, 16000) == 50
    with pytest.raises(ValueError):
        onset_to_audio_ms(0, 0)


# ------------------------------------------------------------- main cases


def test_silence_zero_onsets() -> None:
    assert _run_onsets(_sig([0] * 1000)) == []


def test_single_speech_one_onset() -> None:
    pcm = _sig([0] * 500 + [1000] * 200 + [0] * 500)
    onsets = _run_onsets(pcm)
    assert len(onsets) == 1
    # onset backdated to first energy window: 500 (25 windows * 20 samples)
    assert onsets[0] == 500


def test_continuous_speech_one_onset() -> None:
    assert _run_onsets(_sig([1000] * 2000)) == [0]


def test_speech_silence_speech_two_onsets() -> None:
    pcm = _sig([1000] * 200 + [0] * 300 + [1000] * 200)
    onsets = _run_onsets(pcm)
    assert onsets == [0, 500]


def test_short_noise_burst_zero_onsets() -> None:
    # 30 samples (< energy_frames windows) then silence → never fires.
    pcm = _sig([1000] * 30 + [0] * 500)
    assert _run_onsets(pcm) == []


def test_below_threshold_zero_onsets() -> None:
    # value 100 → RMS ~0.003 < 0.012 threshold.
    pcm = _sig([100] * 2000)
    assert _run_onsets(pcm) == []


def test_threshold_boundary_deterministic() -> None:
    # Slightly above threshold → energy; slightly below → not. Same input → same output.
    hi = [2000] * 1000  # RMS ~0.061
    lo = [200] * 1000  # RMS ~0.0061
    assert _run_onsets(_sig(hi)) == [0]
    assert _run_onsets(_sig(lo)) == []
    assert _run_onsets(_sig(hi)) == _run_onsets(_sig(hi))


# ------------------------------------------------------- chunk invariance


def _logical_stream() -> list[int]:
    """Speech(200) + silence(300) + speech(200) at 1 sample/ms → 2 onsets."""
    return [1000] * 200 + [0] * 300 + [1000] * 200


def _chunk_push(detector: RmsOnsetDetector, samples: list[int], chunk_samples: int) -> None:
    """Feed ``samples`` via pushes of ``chunk_samples`` whole samples each."""
    for i in range(0, len(samples), chunk_samples):
        detector.push(_sig(samples[i : i + chunk_samples]))
    detector.flush()


def test_chunk_boundary_invariance() -> None:
    samples = _logical_stream()
    reference = _run_onsets(_sig(samples))

    for chunk in (1, 7, 20, 33, 64, 500):
        onsets: list[int] = []
        d = _detector(on_onset=onsets.append)
        _chunk_push(d, samples, chunk)
        assert onsets == reference, f"chunk={chunk}"

    # win-by-win chunking
    onsets: list[int] = []
    d = _detector(on_onset=onsets.append)
    for i in range(0, len(samples), 20):
        d.push(_sig(samples[i : i + 20]))
    d.flush()
    assert onsets == reference


def test_chunk_boundary_continuous_speech() -> None:
    samples = [1000] * 2000
    reference = _run_onsets(_sig(samples))
    assert reference == [0]
    for chunk in (1, 7, 33, 64):
        onsets: list[int] = []
        d = _detector(on_onset=onsets.append)
        _chunk_push(d, samples, chunk)
        assert onsets == reference


# ------------------------------------------------------------ backdating


def test_onset_backdates_to_window_start_not_detection_time() -> None:
    """Onset fires after energy_frames windows; must report the FIRST window."""
    # Speech starts at sample 400. With 20-sample windows the first energy window
    # is [400, 420). Detection confirms at window [440, 460) (3rd), so the fired
    # index must be 400, not 440.
    pcm = _sig([0] * 400 + [1000] * 200 + [0] * 400)
    onsets = _run_onsets(pcm)
    assert onsets == [400]


def test_frames_before_first_onset() -> None:
    d = _detector()
    d.push(_sig([0] * 300 + [1000] * 200))
    d.flush()
    assert d.frames_before_first_onset == 300


def test_frames_before_first_zero_when_speech_at_start() -> None:
    d = _detector()
    d.push(_sig([1000] * 200))
    d.flush()
    assert d.frames_before_first_onset == 0


def test_refractory_merges_close_bursts() -> None:
    """Two bursts closer than refractory_ms → single onset (chunk-safe merge)."""
    # speech(200) + silence(40 — shorter than exit_frames+refractory) + speech(200)
    pcm = _sig([1000] * 200 + [0] * 40 + [1000] * 200)
    onsets = _run_onsets(pcm)
    # 40ms gap: quiet windows = 2 < exit_frames(5) → never exits SPEECH.
    assert onsets == [0]


# ------------------------------------------------------------- validation


def test_invalid_params_raise() -> None:
    with pytest.raises(ValueError):
        _detector(sample_rate=0)
    with pytest.raises(ValueError):
        _detector(win_ms=0)
    with pytest.raises(ValueError):
        _detector(threshold=0.0)
    with pytest.raises(ValueError):
        _detector(threshold=1.0)
    with pytest.raises(ValueError):
        _detector(energy_frames=0)
    with pytest.raises(ValueError):
        _detector(exit_frames=0)
    with pytest.raises(ValueError):
        _detector(refractory_ms=-1)
