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
    # No `turnId` on the wire -> None, so this behaves exactly as before the
    # field was read. A worker that does not publish it must keep working.
    assert parsed == ("agent", "Hello", None)


def test_parse_transcript_payload_keeps_the_turn_id(tmp_path) -> None:
    """`turnId` sits at the TOP level of the payload, not inside `turn`.

    Dropping it is what let one utterance become two turns — see the next test.
    """
    obs, _ = _observer(tmp_path)
    payload = {
        "type": "transcript_turn",
        "interim": False,
        "turn": {"role": "agent", "text": "Hello", "timestampMs": 1},
        "turnId": "turn-42",
    }
    assert obs._parse_transcript_payload(payload) == ("agent", "Hello", "data:turn-42")


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


def test_split_utterance_with_short_agent_interjection_stays_same_turn(tmp_path) -> None:
    """A user utterance split into two finals with a short/backchannel-like
    agent reply in between must not be counted as two turns (issue #100)."""
    obs, writer = _observer(tmp_path, first_speaker="user")

    obs.on_transcript("user", "My name is Jane", final=True, source="lk.transcription")
    turn_after_first_half = obs.turn
    # Short, distinct (not an echo) agent interjection — e.g. a premature
    # partial reply, not the real turn-ending answer.
    obs.on_transcript("agent", "Got it", final=True, source="app.transcript")
    obs.on_transcript(
        "user", "and the company is Acme", final=True, source="lk.transcription"
    )

    assert obs.turn == turn_after_first_half
    user_finals = [e for e in writer._events if e["kind"] == "transcript.user.final"]
    assert len(user_finals) == 2
    assert user_finals[-1]["spec"].get("same_turn") is True

    row = writer.turn_metrics()[-1]
    assert row["user_text"] == "My name is Jane and the company is Acme"


def test_new_turn_starts_after_a_real_agent_answer(tmp_path) -> None:
    """A longer, genuine agent reply still ends the turn as before."""
    obs, writer = _observer(tmp_path, first_speaker="user")

    obs.on_transcript("user", "My name is Jane", final=True, source="lk.transcription")
    turn_after_first = obs.turn
    obs.on_transcript(
        "agent",
        "Thanks Jane, could you tell me your company name as well, please?",
        final=True,
        source="app.transcript",
    )
    obs.on_transcript("user", "Acme Corp", final=True, source="lk.transcription")

    assert obs.turn == turn_after_first + 1


def test_parse_transcript_payload_custom_type_from_config(tmp_path) -> None:
    obs, _ = _observer(
        tmp_path,
        observe=ObserveConfig(transcript_payload_types=["live_transcript"]),
    )
    payload = {
        "type": "live_transcript",
        "turn": {"role": "user", "text": "Hi"},
    }
    assert obs._parse_transcript_payload(payload) == ("user", "Hi", None)


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
def test_stale_segment_interim_dropped_across_turns_run_050(tmp_path) -> None:
    """Run 050 regression: the SDK's lk.transcription delta-stream writer
    closes asynchronously, so interim chunks of an ALREADY-FINALIZED segment
    keep arriving after the turn advanced (same segment_id, stale text).
    The role-level late-interim guard cannot catch them (the role's final
    set was cleared on begin_turn) — finality must be tracked per
    (role, segment_id) so the stale tail is dropped even across turns."""
    obs, writer = _observer(tmp_path, first_speaker="agent")
    seg = "SG_stale_tail"

    obs.on_transcript("user", "Could you tell me the price?", final=True,
                      segment_id=seg, source="lk.transcription")
    assert obs.turn == 1
    # A new turn begins (different segment) after the agent replied — a
    # long, substantive agent answer ends the turn, so the next user final
    # advances (mirrors test_new_turn_starts_after_a_real_agent_answer).
    obs.on_transcript("agent", "Thanks, let me pull up the full details and options for you now please.", final=True,
                      source="voice_ai.transcript")
    obs.on_transcript("user", "A brand new question here", final=True,
                      segment_id="SG_new", source="lk.transcription")
    assert obs.turn == 2
    before = len(writer._events)
    # Stale interim of the OLD segment arrives late — must be dropped.
    obs.on_transcript("user", "Could you tell me the pr", final=False,
                      segment_id=seg, source="lk.transcription")
    assert len(writer._events) == before
    assert obs.turn == 2


# ---------------------------------------------------------------------------
# Cross-source duplicate finals (VOICEAIDASHBOARD follow-up / run 033)
#
# A caller utterance is reported twice: once by the room's own STT
# (`lk.transcription`, carrying a segment_id) and once by the agent republishing
# its transcript (`voice_ai.transcript`, no segment_id, ~400 ms later, identical
# text). The second copy used to be merged as a split-utterance continuation, so
# the run summary APPENDED it and every caller line rendered doubled
# ("Hello. ... Hello. ...").
#
# Same text from the SAME source is a genuine repeat and must still merge, so
# the dedupe keys on source, not text alone.
# ---------------------------------------------------------------------------


def test_agent_republished_transcript_does_not_double_user_text(tmp_path):
    obs, writer = _observer(tmp_path)
    line = "Hello. I would like to speak to someone about my building."

    obs.on_transcript("user", line, final=True, source="lk.transcription", segment_id="SG_1")
    # Agent republishes the same line with no segment_id, before any agent reply.
    obs.on_transcript("user", line, final=True, source="voice_ai.transcript")

    finals = [
        e for e in writer.events if e["kind"] == "transcript.user.final"
    ]
    assert len(finals) == 1, "cross-source duplicate must be dropped"
    assert finals[0]["spec"]["text"] == line


def test_same_source_repeat_is_dropped_by_the_dedupe_window(tmp_path):
    """Identical text from the same source is already handled upstream.

    _accept_final drops a same-source, same-text final inside
    transcript_dedupe_window_ms, so it never reaches the turn-merge branch.
    This is the pre-existing behaviour the new check deliberately does not
    change — it only covers the CROSS-source case that slipped through.
    """
    obs, writer = _observer(tmp_path)

    obs.on_transcript("user", "yes", final=True, source="lk.transcription", segment_id="SG_1")
    obs.on_transcript("user", "yes", final=True, source="lk.transcription", segment_id="SG_2")

    finals = [
        e for e in writer.events if e["kind"] == "transcript.user.final"
    ]
    assert len(finals) == 1


def test_cross_source_duplicate_dropped_across_many_turns(tmp_path):
    """Regression shape from run 033: every turn doubled, 13/13 affected."""
    obs, writer = _observer(tmp_path)
    lines = [
        "Hello.",
        "I am afraid I cannot say.",
        "I really do not know.",
        "That is all I can say.",
    ]
    for i, line in enumerate(lines):
        obs.on_transcript("user", line, final=True, source="lk.transcription", segment_id=f"SG_{i}")
        obs.on_transcript("user", line, final=True, source="voice_ai.transcript")
        # A real agent answer is a full sentence. A 1-2 word reply would be
        # classified as a backchannel, which legitimately merges the next user
        # final into this turn instead of starting a new one.
        # Unique per turn: an identical agent final would be swallowed by
        # _accept_final dedupe window, leaving the turn open so the next user
        # final merges as a continuation.
        obs.on_transcript(
            "agent",
            f"Thank you. Next, could you please provide your callback phone "
            f"number, including the area code? (step {i})",
            final=True,
            source="lk.transcription",
        )

    finals = [
        e for e in writer.events if e["kind"] == "transcript.user.final"
    ]
    assert len(finals) == len(lines)
    assert [e["spec"]["text"] for e in finals] == lines


def test_data_channel_turn_id_does_not_drive_dedup(tmp_path) -> None:
    """Evidence yes, control no.

    `_finalized_segments` is only populated for `lk.transcription`, so a
    data-channel id never has anything to compare against. This pins that: if
    someone opens that gate, a worker re-publishing a corrected final under the
    same turnId would be silently dropped, and that is a worse failure than
    the duplicate this id was added to help diagnose.
    """
    obs, _ = _observer(tmp_path)
    payload = {
        "type": "transcript_turn",
        "interim": False,
        "turn": {"role": "agent", "text": "Hello"},
        "turnId": "turn-7",
    }
    role, text, seg = obs._parse_transcript_payload(payload)
    assert (role, text, seg) == ("agent", "Hello", "data:turn-7")

    # Drive the REAL path rather than simulating it. An earlier version of
    # this test hardcoded `if "lk.transcription" == "lk.transcription"`, which
    # is always true — it exercised the branch the test claims to exclude and
    # then asserted the opposite, so it failed for a reason that had nothing
    # to do with the behaviour under test.
    obs.on_transcript(
        role, text, final=True, source="voice_ai.transcript", segment_id=seg
    )
    assert obs._finalized_segments == set(), (
        "a data-channel turn id reached _finalized_segments; that changes "
        "which finals are dropped and is not what this field is for"
    )

    # And the lk path DOES record, so the gate is real and this test is not
    # passing merely because the field is never used. Role `user` because an
    # agent final at turn 0 takes the preamble branch, which returns early
    # without touching the set — which is what made the first attempt at this
    # assertion fail for an unrelated reason.
    obs.on_transcript(
        "user", "Hi", final=True, source="lk.transcription", segment_id="seg-1"
    )
    assert ("user", "seg-1") in obs._finalized_segments


def test_turn_id_is_namespaced_away_from_livekit_segment_ids(tmp_path) -> None:
    """They share one key space, so a bare id could collide."""
    obs, _ = _observer(tmp_path)
    payload = {
        "type": "transcript_turn",
        "interim": False,
        "turn": {"role": "agent", "text": "Hello"},
        "turnId": "seg-abc",
    }
    assert obs._parse_transcript_payload(payload)[2] == "data:seg-abc"


# ---------------------------------------------------------------------------
# the disconnect reason (previously discarded)
# ---------------------------------------------------------------------------


def test_a_disconnect_reason_is_named_not_dropped() -> None:
    """LiveKit passes a `DisconnectReason` and the handler used to emit `{}`.

    room.py:690 is `self.emit("disconnected", reason)`. The handler took
    `*args` and wrote an empty spec, so every `room.disconnected` event in
    every report said nothing about why. A peer reading run 081 correctly
    reported "no preceding room.reason" — there was never a reason recorded,
    because there was never a handler for one.
    """
    from livekit_agent_simulator.livekit.observer import _disconnect_reason_name

    # The distinction that matters: the harness tearing the room down versus
    # the transport going away underneath it. Previously indistinguishable.
    assert _disconnect_reason_name((1,)) == "CLIENT_INITIATED"
    assert _disconnect_reason_name((9,)) == "SIGNAL_CLOSE"
    assert _disconnect_reason_name((10,)) == "ROOM_CLOSED"
    assert _disconnect_reason_name((6,)) == "STATE_MISMATCH"


def test_an_unnameable_reason_still_says_something() -> None:
    """A disconnect whose reason cannot be named is still a disconnect.

    An empty string would read as "no reason", which is a different claim.
    """
    from livekit_agent_simulator.livekit.observer import _disconnect_reason_name

    assert _disconnect_reason_name(()) == "UNKNOWN"
    assert _disconnect_reason_name((None,)) == "UNKNOWN"
    assert "999" in _disconnect_reason_name((999,))


def test_naming_a_reason_never_raises() -> None:
    """This runs inside a LiveKit event callback.

    If it raises, the handler that was supposed to record the disconnect
    raises instead, and the evidence is lost the same way it was before —
    except now with a traceback nobody sees.
    """
    from livekit_agent_simulator.livekit.observer import _disconnect_reason_name

    class Hostile:
        def __int__(self) -> int:
            raise RuntimeError("no")

    assert _disconnect_reason_name((Hostile(),))


# ---------------------------------------------------------------------------
# barge-in overlap: the agent declaring `speaking` before VAD agrees
# ---------------------------------------------------------------------------


def test_the_agent_declaring_speaking_before_vad_emits_overlap_evidence() -> None:
    """The 281ms window where a barge-in is invisible.

    Measured on the target repo, 2026-09-30, run 106: the caller published
    220800 bytes at +44484ms; the agent went `speaking` at +45765ms while
    `active_speakers` was still `["lks-caller"]`; the caller was transcribed
    as the single word " You" and the agent then re-asked for the number.

    BOTH fields were already in the log. Nothing joined them, so the report
    could not show that the agent had cut the caller off — it took reading the
    WAV stereo channels by hand to find it. This is the join.
    """
    import time

    from livekit_agent_simulator.livekit.observer import Observer

    class _Writer:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        def emit(self, kind, spec=None, **kwargs) -> None:
            self.events.append((kind, spec or {}))

    obs = Observer.__new__(Observer)
    obs.writer = _Writer()
    obs.agent_state = None
    obs.agent_state_observed = False
    obs.agent_identity = "agent-1"
    obs.agent_is_active_speaker = False
    obs._agent_active_since_mono = None
    obs._agent_has_spoken = False
    obs._active_speaker_identities = ["lks-caller"]
    obs._agent_declared_speaking_mono = None

    # The agent declares speaking while the caller is the active speaker.
    obs._agent_declared_speaking_mono = time.monotonic()
    obs._emit_barge_in_overlap(signal="audio_onset")

    kinds = [k for k, _ in obs.writer.events]
    assert "room.barge_in_overlap" in kinds, obs.writer.events
    spec = dict(obs.writer.events)["room.barge_in_overlap"]
    assert spec["caller_was_active"] is True
    assert spec["active_speakers"] == ["lks-caller"]
    assert spec["signal"] == "audio_onset"
    assert spec["declared_lead_ms"] is not None and spec["declared_lead_ms"] >= 0


def test_no_overlap_when_the_agent_is_not_the_other_speaker() -> None:
    """The caller must actually hold the floor.

    The onset handler fires on every agent utterance; an overlap event that
    ignored this would fire on every turn of a healthy call.
    """
    class _Writer:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        def emit(self, kind, spec=None, **kwargs) -> None:
            self.events.append((kind, spec or {}))

    obs = Observer.__new__(Observer)
    obs.writer = _Writer()
    obs.agent_identity = "agent-1"
    obs.agent_is_active_speaker = False
    obs._active_speaker_identities = ["agent-1"]
    obs._agent_declared_speaking_mono = None

    obs._emit_barge_in_overlap(signal="audio_onset")

    assert obs.writer.events == [], obs.writer.events
