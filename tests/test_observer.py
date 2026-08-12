"""Unit tests for Observer transcript dedupe and generic data-topic parsing."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from livekit_agent_simulator.config import ObserveConfig
from livekit_agent_simulator.livekit.observer import Observer
from livekit_agent_simulator.logging.event_writer import EventWriter


def _observer(
    tmp_path: pytest.TempPathFactory | object,
    *,
    first_speaker: str = "user",
    observe: ObserveConfig | None = None,
) -> tuple[Observer, EventWriter]:
    report_dir = tmp_path / "reports" / "r-test"  # type: ignore[operator]
    writer = EventWriter("r-test", report_dir, timezone_name="UTC")
    obs = Observer(
        MagicMock(),
        writer,
        observe or ObserveConfig(transcript_dedupe_window_ms=5000),
        agent_identity="agent-1",
        sim_identity="sim-1",
        first_speaker=first_speaker,
    )
    return obs, writer


def test_dedupe_user_final_prefers_sim_gemini_over_lk(tmp_path) -> None:
    obs, writer = _observer(tmp_path)
    text = "あの、すみません、田中と申します。"

    obs.on_transcript("user", text, final=True, source="sim.gemini")
    event_count = len(writer._events)
    obs.on_transcript("user", "あの 、 すみ ませ ん 、 田中 と 申し ます 。", final=True, source="lk.transcription")

    finals = [e for e in writer._events if e["kind"] == "transcript.user.final"]
    assert len(finals) == 1
    assert obs.turn == 1
    assert len(writer._events) == event_count


def test_dedupe_agent_final_prefers_data_topic_over_lk(tmp_path) -> None:
    obs, writer = _observer(tmp_path, first_speaker="agent")
    user = "こんにちは"
    agent = "こんにちは、何かお手伝いできますか？"

    obs.on_transcript("user", user, final=True, source="sim.gemini")
    obs.on_transcript("agent", agent, final=True, source="app.transcript")
    before_turn = obs.turn
    obs.on_transcript("agent", agent, final=True, source="lk.transcription")

    agent_finals = [e for e in writer._events if e["kind"] == "transcript.agent.final"]
    assert len(agent_finals) == 1
    assert obs.turn == before_turn


def test_agent_preamble_not_counted_when_user_speaks_first(tmp_path) -> None:
    obs, writer = _observer(tmp_path, first_speaker="user")

    obs.on_transcript("agent", "System UI artifact", final=True, source="lk.transcription")
    obs.on_transcript("user", "もしもし", final=True, source="sim.gemini")

    preambles = [e for e in writer._events if e["kind"] == "transcript.agent.preamble"]
    user_finals = [e for e in writer._events if e["kind"] == "transcript.user.final"]
    assert len(preambles) == 1
    assert len(user_finals) == 1
    assert obs.turn == 1


def test_parse_transcript_payload_generic_type(tmp_path) -> None:
    obs, _ = _observer(tmp_path)
    payload = {
        "type": "transcript_turn",
        "interim": False,
        "turn": {"role": "agent", "text": "Hello", "timestampMs": 1},
    }
    parsed = obs._parse_transcript_payload(payload)
    assert parsed == ("agent", "Hello")


def test_late_user_echo_after_agent_reply_not_new_turn(tmp_path) -> None:
    obs, writer = _observer(tmp_path, first_speaker="user")
    user1 = "こんにちは"
    agent1 = "はい、どうぞ"

    obs.on_transcript("user", user1, final=True, source="sim.gemini")
    obs.on_transcript("agent", agent1, final=True, source="app.transcript")
    turn_after_agent = obs.turn
    obs.on_transcript("user", "こん に ちは", final=True, source="lk.transcription")

    assert obs.turn == turn_after_agent
    user_finals = [e for e in writer._events if e["kind"] == "transcript.user.final"]
    assert len(user_finals) == 1


def test_parse_transcript_payload_custom_type_from_config(tmp_path) -> None:
    obs, _ = _observer(
        tmp_path,
        observe=ObserveConfig(transcript_payload_types=["live_transcript"]),
    )
    payload = {
        "type": "live_transcript",
        "turn": {"role": "user", "text": "Hi"},
    }
    assert obs._parse_transcript_payload(payload) == ("user", "Hi")


def test_agent_audio_onset_emits_corrected_timestamp(tmp_path) -> None:
    """sim.agent.audio_onset ts_mono_ms backdates to the onset frame, not detection."""
    from livekit_agent_simulator.audio.local_recorder import LocalConversationRecorder

    recorder = LocalConversationRecorder(sample_rate=16_000)
    recorder.mark_start()

    ao = ObserveConfig().audio_onset
    assert ao.enabled is False  # default off

    from livekit_agent_simulator.config import AudioOnsetConfig

    observe = ObserveConfig(
        audio_onset=AudioOnsetConfig(
            enabled=True,
            vad="rms",
            threshold=0.012,
            win_ms=20,
            energy_frames=3,
            exit_frames=5,
            refractory_ms=60,
        )
    )
    report_dir = tmp_path / "reports" / "r-onset"
    writer = EventWriter("r-onset", report_dir, timezone_name="UTC")
    obs = Observer(
        MagicMock(),
        writer,
        observe,
        agent_identity="agent-1",
        sim_identity="sim-1",
        first_speaker="agent",
        recorder=recorder,
    )
    assert obs._onset_detector is not None

    # audio_t0 relative to run: recorder.started_mono - writer.t0_mono
    audio_t0 = int((recorder.started_mono - writer.t0_mono) * 1000)  # type: ignore[operator]

    # Onset at frame 3200 = 200ms into the agent stream.
    obs._on_agent_onset(3200)

    events = writer.events
    onsets = [e for e in events if e["kind"] == "sim.agent.audio_onset"]
    assert len(onsets) == 1
    ev = onsets[0]
    assert ev["ts_mono_ms"] == max(0, audio_t0 + 200)
    assert ev["spec"]["onset_frame_idx"] == 3200
    assert ev["spec"]["sample_rate"] == 16_000
    assert ev["spec"]["vad"]["method"] == "rms"
