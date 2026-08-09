import struct
import wave
from pathlib import Path

from livekit_agent_simulator.audio.local_recorder import (
    LocalConversationRecorder,
    resample_pcm16_mono,
)


def _tone(n_samples: int, amplitude: int = 1000) -> bytes:
    return struct.pack("<" + "h" * n_samples, *([amplitude] * n_samples))


def test_resample_identity() -> None:
    pcm = _tone(100)
    assert resample_pcm16_mono(pcm, 16_000, 16_000) == pcm


def test_resample_changes_length() -> None:
    pcm = _tone(240)  # 10ms @ 24k
    out = resample_pcm16_mono(pcm, 24_000, 16_000)
    # ~160 samples @ 16k
    assert abs(len(out) // 2 - 160) <= 2


def test_finalize_writes_stereo_wav(tmp_path: Path) -> None:
    rec = LocalConversationRecorder(sample_rate=16_000)
    rec.mark_start()
    rec.push_sim(_tone(160), 16_000)
    rec.push_agent(_tone(160, amplitude=500), 16_000)

    path = tmp_path / "conversation.wav"
    result = rec.finalize(path)
    assert result is not None
    assert result.path == path
    assert path.exists()
    assert result.duration_ms >= 10

    with wave.open(str(path), "rb") as wf:
        assert wf.getnchannels() == 2
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16_000
        frames = wf.readframes(wf.getnframes())
    # Stereo interleaved: even samples L (sim=1000), odd R (agent=500) at start
    first_l, first_r = struct.unpack_from("<hh", frames, 0)
    assert first_l == 1000
    assert first_r == 500


def test_finalize_empty_returns_none(tmp_path: Path) -> None:
    rec = LocalConversationRecorder()
    assert rec.finalize(tmp_path / "empty.wav") is None
    assert not (tmp_path / "empty.wav").exists()


def test_24k_sim_and_16k_agent(tmp_path: Path) -> None:
    rec = LocalConversationRecorder(sample_rate=16_000)
    rec.mark_start()
    # 20ms of sim at 24k, 20ms agent at 16k
    rec.push_sim(_tone(480), 24_000)
    rec.push_agent(_tone(320), 16_000)
    result = rec.finalize(tmp_path / "mix.wav")
    assert result is not None
    assert result.sim_samples > 0
    assert result.agent_samples > 0


def test_concurrent_agent_tracks_mix_in_place_instead_of_appending(
    tmp_path: Path,
) -> None:
    rec = LocalConversationRecorder(sample_rate=16_000)
    rec.mark_start()

    rec.push_agent(_tone(160, amplitude=1_000), 16_000, track_id="tts")
    rec.push_agent(_tone(160, amplitude=500), 16_000, track_id="background_audio")

    path = tmp_path / "multitrack.wav"
    result = rec.finalize(path)
    assert result is not None
    # Two concurrent 10ms tracks remain 10ms, not a concatenated 20ms stream.
    assert result.agent_samples == 160

    with wave.open(str(path), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
    _left, right = struct.unpack_from("<hh", frames, 0)
    assert right == 1_500


def test_agent_track_mix_saturates_pcm16(tmp_path: Path) -> None:
    rec = LocalConversationRecorder(sample_rate=16_000)
    rec.mark_start()
    rec.push_agent(_tone(160, amplitude=30_000), 16_000, track_id="tts")
    rec.push_agent(
        _tone(160, amplitude=30_000),
        16_000,
        track_id="background_audio",
    )

    path = tmp_path / "saturated.wav"
    assert rec.finalize(path) is not None
    with wave.open(str(path), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
    _left, right = struct.unpack_from("<hh", frames, 0)
    assert right == 32_767
