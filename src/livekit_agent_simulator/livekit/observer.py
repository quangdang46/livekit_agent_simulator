"""Passive in-room observer — zero-touch on the agent under test.

Sources:
    L0  room events        participant joins/leaves, tracks, active speakers, disconnect
    L1a lk.transcription   text streams published by AgentSession (agent + user segments)
    L1b custom data topics any payload matching observe.transcript_payload_types (e.g. transcript_turn)
    L2  tool patterns      config-driven match rules over data-topic JSON payloads
    L3  lk.agent.session   SDK tools, state, errors, usage, and final chat history

Attribute keys per LiveKit docs (agents/multimodality/text):
    lk.transcription_final, lk.segment_id
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from livekit import rtc

from ..config import ObserveConfig, ToolEventPattern
from ..logging.event_writer import EventWriter
from .agent_session_observer import AgentSessionObserver

ATTR_FINAL = "lk.transcription_final"
ATTR_SEGMENT_ID = "lk.segment_id"

# The participant attribute LiveKit's agent-worker publishes for its own turn
# state. Values: idle | initializing | listening | thinking | speaking.
# `speaking -> listening` is the authoritative turn end.
AGENT_STATE_ATTRIBUTE_KEY = "lk.agent.state"


def _disconnect_reason_name(args: tuple[object, ...]) -> str:
    """Name the `DisconnectReason` LiveKit passes to the `disconnected` callback.

    Returns a stable, greppable string. Never raises and never returns an
    empty string, because a disconnect whose reason cannot be named is still a
    disconnect and "unknown" is a more useful thing to read than nothing.

    The argument is a protobuf enum value (int) in the shipped SDK, but the
    callback is variadic and the SDK has changed this shape before, so the
    helper accepts whatever arrives and degrades to `repr` rather than
    assuming.
    """
    raw = args[0] if args else None
    if raw is None:
        return "UNKNOWN"
    try:
        from livekit.rtc import DisconnectReason

        # `Name(number)` raises ValueError on an unmapped value, which is
        # exactly the case that should fall through to the string form.
        return DisconnectReason.Name(int(raw))
    except Exception:  # noqa: BLE001 — naming must never break the handler
        return f"UNRECOGNIZED({raw!r})"

# Lower index = higher priority when deduping finals from multiple sources.
# Provider sim-transcript sources (sim.gemini / sim.openai) are the most
# trustworthy caller transcripts; data-topic and lk.transcription are mirrors.
_SIM_TRANSCRIPT_SOURCES = ("sim.gemini", "sim.openai")
_USER_FINAL_PRIORITY = (*_SIM_TRANSCRIPT_SOURCES, "data", "lk.transcription")
_AGENT_FINAL_PRIORITY = ("data", "lk.transcription", *_SIM_TRANSCRIPT_SOURCES)

# A user final arriving within this window of a short agent final is treated
# as a possible split-utterance continuation rather than a new turn — see
# on_transcript()'s same_turn merge logic.
_BACKCHANNEL_GRACE_MS = 700
_BACKCHANNEL_MAX_WORDS = 6


def _lookup_path(payload: dict[str, Any], dotted: str) -> Any:
    cur: Any = payload
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text.strip())


def _similar_text(a: str, b: str) -> bool:
    if a == b:
        return True
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if shorter in longer:
        return len(shorter) / len(longer) >= 0.85
    return False


def _canonical_source(source: str) -> str:
    if source in (*_SIM_TRANSCRIPT_SOURCES, "lk.transcription"):
        return source
    return "data"


def _source_priority_rank(source: str, role: str) -> int:
    order = _USER_FINAL_PRIORITY if role == "user" else _AGENT_FINAL_PRIORITY
    canonical = _canonical_source(source)
    try:
        return order.index(canonical)
    except ValueError:
        return len(order)


class Observer:
    def __init__(
        self,
        room: rtc.Room,
        writer: EventWriter,
        observe: ObserveConfig,
        agent_identity: str,
        sim_identity: str,
        *,
        first_speaker: str = "agent",
        recorder: Any | None = None,
    ) -> None:
        self.room = room
        self.writer = writer
        self.observe = observe
        self.agent_identity = agent_identity
        self.sim_identity = sim_identity
        self.first_speaker = first_speaker
        # Optional: record agent PCM from *this* room (agent-room on SIP legs).
        # Decouples conversation.wav R-channel from Gemini sim-room track subscription.
        self.recorder = recorder
        # Perceived agent speech onset (RMS VAD on the agent R channel). Gated
        # by ObserveConfig.audio_onset.enabled; emits sim.agent.audio_onset with
        # a timestamp backdated to the onset frame (not detection time).
        self._onset_detector: Any | None = None
        if recorder is not None and observe.audio_onset.enabled:
            from ..audio.vad import RmsOnsetDetector

            ao = observe.audio_onset
            self._onset_detector = RmsOnsetDetector(
                sample_rate=16_000,
                win_ms=ao.win_ms,
                threshold=ao.threshold,
                energy_frames=ao.energy_frames,
                exit_frames=ao.exit_frames,
                refractory_ms=ao.refractory_ms,
                on_onset=self._on_agent_onset,
            )

        # Turn tracking: a turn = one user utterance + the agent reply to it.
        self.turn = 0
        self._last_user_final_mono: float | None = None
        self._last_agent_final_mono: float | None = None
        self._last_agent_final_text: str = ""
        self._agent_replied_this_turn = False
        self.last_agent_activity_mono: float = time.monotonic()
        self.agent_disconnected = asyncio.Event()
        self._agent_has_spoken = False
        self._user_has_spoken = False
        self._current_turn_user_norm: str | None = None
        # Any activity (agent OR caller) — the dead-call net measures silence
        # from this, so a caller's first turn gives the agent time to reply.
        self.last_activity_mono: float = time.monotonic()
        # True once the first transcript (either role) has landed; the
        # dead-call net only arms after this.
        self._any_activity = False
        # (role, text) of the last interim we synthesized before a final, so the
        # ordering guard in on_transcript never double-emits for the same turn.
        self._last_interim_key: tuple[str, str] | None = None
        # Roles that already emitted a final in the current turn (late interims
        # for those roles are dropped — see on_transcript).
        self._finalized_roles: set[str] = set()
        # (role, segment_id) of the last final per lk.transcription segment.
        # Run 050: the SDK's lk.transcription delta-stream writer closes
        # asynchronously, so interim chunks of an ALREADY-FINALIZED segment
        # keep arriving after the turn advanced (same segment_id, stale
        # text). The role-level guard above cannot catch them (the role's
        # final set was cleared on begin_turn). Track finality per segment
        # so a stale-segment interim is dropped even across turn boundaries.
        self._finalized_segments: set[tuple[str, str]] = set()

        self.agent_is_active_speaker = False
        self._agent_active_since_mono: float | None = None

        # The agent's own turn state, read from the participant attribute
        # LiveKit's agent-worker publishes (`lk.agent.state`: idle |
        # initializing | listening | thinking | speaking).
        #
        # Why this exists and `agent_is_active_speaker` alone is not enough:
        # `active_speakers_changed` is derived from audio ENERGY and, measured
        # on run 035 (PROBLEMS.md §1), lags the agent's real audio by ~2.4s.
        # That is long enough for a `silence` trigger to satisfy "agent has been
        # silent for delay_ms" while the agent is audibly mid-turn, so the
        # caller publishes over the agent and the utterance is never delivered
        # as a user turn.
        #
        # The agent decides its own turn boundaries, so `speaking ->
        # listening` is the turn end — not an inference from energy. The
        # transcript is NOT a substitute: it arrives AFTER the audio (15.4s vs
        # 17.8s measured), so switching to it would fire the caller EARLIER
        # and make the overlap worse.
        #
        # `None` means "the agent has not published the attribute". That is a
        # distinct third state, not "silent" and not "speaking" — see
        # `agent_state_observed`.
        # Every agent final, in arrival order, with its monotonic stamp.
        # The single `last_agent_final_text` slot above cannot serve
        # `ObserverAgentWait`: a final that lands while
        # `agent_is_active_speaker` is still true (VAD energy, ~2.4s late —
        # PROBLEMS.md §1) is held by the waiter, and the NEXT turn's final
        # overwrites it before the flag clears. On run 013 that put the router
        # exactly one turn behind on every decision. The queue makes turn
        # assignment independent of when the flag clears.
        self._agent_final_queue: list[tuple[str, float, str | None]] = []
        # One utterance sealed as two, by the adapter idle timer.
        # Evidence only; see `_push_agent_final`.
        self._over_sealed: list[dict[str, int]] = []

        self.agent_state: str | None = None
        self.agent_state_observed = False

        # (role, normalized text) -> (source, monotonic time)
        self._recent_finals: dict[tuple[str, str], tuple[str, float]] = {}

        # tool.start events waiting for their tool.end/tool.error (call_id -> event)
        self._open_tools: dict[str, dict[str, Any]] = {}
        self._agent_session = (
            AgentSessionObserver(room, writer, agent_identity)
            if observe.lk_agent_session
            else None
        )
        self._record_tasks: list[asyncio.Task] = []
        self._recording_track_sids: set[str] = set()

    @property
    def agent_replied_this_turn(self) -> bool:
        return self._agent_replied_this_turn

    @property
    def agent_has_spoken(self) -> bool:
        return self._agent_has_spoken

    @property
    def user_has_spoken(self) -> bool:
        return self._user_has_spoken

    @property
    def last_user_final_mono(self) -> float | None:
        return self._last_user_final_mono

    @property
    def last_agent_final_mono(self) -> float | None:
        return self._last_agent_final_mono

    @property
    def last_agent_final_text(self) -> str:
        return self._last_agent_final_text

    def agent_active_duration_ms(self) -> int | None:
        if self._agent_active_since_mono is None:
            return None
        return int((time.monotonic() - self._agent_active_since_mono) * 1000)

    # ------------------------------------------------------------------ attach

    def _push_agent_final(self, text: str, segment_id: str | None = None) -> None:
        """Record an agent final in arrival order.

        Dedup is by SEGMENT ID, never by text.

        Two wrong answers were tried here. Collapsing adjacent duplicate TEXT
        would drop a real turn where the agent genuinely says the same thing
        twice ("yes.") — the driver would then wait for a reply that already
        arrived. Dropping dedup entirely regresses run 039, whose invariant is
        that the same final is never returned twice: a provider that
        re-finalises one segment would be served that segment's text again as
        if it were a new turn.

        Segment id is the discriminator that separates those two cases, and
        the observer already tracks it for exactly this purpose
        (`_finalized_segments`, `spec["same_turn"]`). With no segment id
        there is nothing to compare, so the final is queued as new — a
        conservative choice that can only surface MORE evidence to the
        waiter, never less.
        """
        if segment_id and segment_id in self._finalized_segments:
            return
        # Did the transport seal one utterance as two? Measured on the target
        # repo, 2026-09-30: "Thank you. Could you tell me the exact number"
        # and "…the exact number of" arrived 3.3s apart, the second a strict
        # extension of the first. The agent said one thing; the adapter closed
        # the burst twice, because its `idleTimeout` fires on any audio gap.
        #
        # This is the discriminating signal for that defect and it is available
        # here — the queue holds arrival order. A truncated final is unroutable
        # BY CONSTRUCTION (the tail IS the question), so the router picking
        # something nearby is correct behaviour on an unusable input, not a
        # routing fault.
        #
        # Recorded, not acted on. Whether the flow should wait for a
        # continuation, mark the turn, or re-dispatch is a product decision
        # about how a voice agent behaves when its own output is truncated.
        if self._agent_final_queue:
            prev = self._agent_final_queue[-1][0]
            if len(text) > len(prev) and text.startswith(prev):
                self._over_sealed.append(
                    {
                        "previous_chars": len(prev),
                        "this_chars": len(text),
                        "gap_ms": int(
                            (time.monotonic() - self._agent_final_queue[-1][1]) * 1000
                        ),
                    }
                )
        # Was the agent still speaking when this final arrived?
        #
        # EVIDENCE ONLY, and — measured 2026-09-30 — NOT the instrument for
        # the truncation people are chasing. That truncation is the LiveKit
        # duplex adapter's `idleTimeout` (duplex_adapter.js:248, 800ms default)
        # closing a burst on audio silence and committing a partial transcript
        # as a complete turn. By the time the final reaches us the adapter has
        # already declared the turn over, so `agent_state` is `listening` and
        # this field will be `None` or `listening` on exactly the cases that
        # matter. It catches genuine barge-in, which is a different mechanism.
        #
        # The right signal is the PREFIX relationship recorded below: if this
        # final starts with the previous one, the transport sealed one
        # utterance twice.
        #
        # Both are recorded rather than acted on. Dropping mid-turn finals
        # re-creates the overwrite bug the queue was built to fix; routing them
        # with a caveat still routes half a question. What the flow should DO
        # about a truncated turn is a product decision nobody has made yet.
        self._agent_final_queue.append(
            (text, time.monotonic(), self.agent_state)
        )

    def take_agent_finals(self) -> list[tuple[str, float, str | None]]:
        """Drain queued agent finals, oldest first.

        Each entry is ``(text, monotonic, agent_state_at_arrival)``. The
        third element is the agent's own declared state when the final
        landed — `speaking` means the agent had not finished. See
        `_push_agent_final` for why it is recorded and not acted on.
        """
        out = list(self._agent_final_queue)
        self._agent_final_queue.clear()
        return out

    def attach(self) -> None:
        room = self.room

        @room.on("participant_connected")
        def _on_join(p: rtc.RemoteParticipant) -> None:
            self.writer.emit(
                "room.participant_connected",
                spec={"identity": p.identity, "name": p.name, "kind": str(p.kind)},
                source="room",
                include_dialogue=False,
            )

        @room.on("participant_disconnected")
        def _on_leave(p: rtc.RemoteParticipant) -> None:
            self.writer.emit(
                "room.participant_disconnected",
                spec={"identity": p.identity},
                source="room",
                include_dialogue=False,
            )
            if p.identity == self.agent_identity:
                self.agent_disconnected.set()

        @room.on("track_subscribed")
        def _on_track(
            track: rtc.Track, pub: rtc.RemoteTrackPublication, p: rtc.RemoteParticipant
        ) -> None:
            self.writer.emit(
                "room.track_subscribed",
                spec={"identity": p.identity, "kind": str(track.kind), "sid": track.sid},
                source="room",
                include_dialogue=False,
            )
            if (
                self.recorder is not None
                and track.kind == rtc.TrackKind.KIND_AUDIO
                and p.identity == self.agent_identity
            ):
                self._start_agent_record(track)

        @room.on("participant_attributes_changed")
        def _on_attrs(changed: dict[str, str], participant: rtc.Participant) -> None:
            """Track the agent's own turn state.

            Registered even though `participant_attributes_changed` is not in
            the client SDK's published EventTypes list in some versions — it is
            dispatched by the room implementation, and a handler that never
            fires is harmless. It is the ONLY way to see the agent's own turn
            boundary; `active_speakers_changed` is an energy signal and is
            ~2.4s late (PROBLEMS.md §1).

            `changed` holds only the keys that changed, so this is a dict
            lookup, not a diff walk.
            """
            if participant.identity != self.agent_identity:
                return
            if AGENT_STATE_ATTRIBUTE_KEY not in changed:
                return
            value = changed[AGENT_STATE_ATTRIBUTE_KEY]
            if not value or value == self.agent_state:
                return
            previous = self.agent_state
            self.agent_state = value
            self.agent_state_observed = True
            if value == "speaking" and previous != "speaking":
                self._agent_active_since_mono = time.monotonic()
                self._agent_has_spoken = True
            self.writer.emit(
                "room.agent_state",
                spec={"state": value, "previous": previous},
                source="room",
                include_dialogue=False,
            )

        @room.on("active_speakers_changed")
        def _on_speakers(speakers: list[rtc.Participant]) -> None:
            identities = [s.identity for s in speakers]
            agent_now = self.agent_identity in identities
            if agent_now and not self.agent_is_active_speaker:
                self._agent_active_since_mono = time.monotonic()
            elif not agent_now:
                self._agent_active_since_mono = None
            self.agent_is_active_speaker = agent_now
            if agent_now:
                self.last_agent_activity_mono = time.monotonic()
                # Active-speaker energy is the earliest reliable signal that
                # the agent is talking (transcripts may arrive late or only as
                # finals with realtime providers). Consumers like
                # InterruptRateRunner gate on agent_has_spoken.
                self._agent_has_spoken = True
            self.writer.emit(
                "room.active_speakers",
                spec={"identities": identities},
                source="room",
                include_dialogue=False,
            )

        @room.on("disconnected")
        def _on_disconnected(*args: object) -> None:
            # LiveKit passes a `DisconnectReason` here (room.py:690:
            # `self.emit("disconnected", reason)`) and this handler used to
            # accept `*args` and emit an EMPTY spec, throwing the reason away.
            #
            # That is why a peer session reading run 081 could report
            # "`room.disconnected` with no preceding `room.reason`" — there
            # was never a reason recorded, because there was never a handler
            # for one. The room dropped on the same millisecond the caller
            # published, which is the single most informative fact available
            # about that failure, and it was being discarded at the moment it
            # was cheapest to keep.
            #
            # The enum distinguishes CLIENT_INITIATED from SIGNAL_CLOSE,
            # ROOM_CLOSED, STATE_MISMATCH and the rest — which is exactly the
            # difference between "the harness tore the room down" and "the
            # transport went away underneath us". Those demand different
            # responses and were previously indistinguishable.
            self.writer.emit(
                "room.disconnected",
                spec={"reason": _disconnect_reason_name(args)},
                source="room",
                include_dialogue=False,
            )
            self.agent_disconnected.set()

        @room.on("data_received")
        def _on_data(packet: rtc.DataPacket) -> None:
            topic = packet.topic or ""
            if self.observe.data_topics and topic not in self.observe.data_topics:
                return
            self._handle_data_topic(topic, packet)

        if self.observe.lk_transcription:
            room.register_text_stream_handler("lk.transcription", self._on_transcription_stream)
        if self._agent_session is not None:
            self._agent_session.attach()

        # Agent track may already be subscribed before attach (common on SIP legs).
        if self.recorder is not None:
            for p in room.remote_participants.values():
                if p.identity != self.agent_identity:
                    continue
                for pub in p.track_publications.values():
                    tr = pub.track
                    if tr is not None and tr.kind == rtc.TrackKind.KIND_AUDIO:
                        self._start_agent_record(tr)

    def _start_agent_record(self, track: rtc.Track) -> None:
        """Record agent remote audio into conversation.wav R-channel (16 kHz mono)."""
        if self.recorder is None:
            return
        sid = getattr(track, "sid", None) or id(track)
        key = str(sid)
        if key in self._recording_track_sids:
            return
        self._recording_track_sids.add(key)
        task = asyncio.create_task(
            self._pump_agent_record(track, key),
            name=f"obs-record-agent-{key[:12]}",
        )
        self._record_tasks.append(task)

    async def _pump_agent_record(self, track: rtc.Track, key: str) -> None:
        from livekit import rtc as _rtc

        stream = _rtc.AudioStream(track, sample_rate=16_000, num_channels=1)
        try:
            self.writer.emit(
                "sim.agent_audio_recorded",
                spec={"track_sid": key, "source": "observer.agent_room", "sample_rate": 16_000},
                source="sim",
                include_dialogue=False,
            )
            async for frame_event in stream:
                if self.recorder is None:
                    break
                frame = frame_event.frame
                pcm = bytes(frame.data)
                if pcm:
                    self.recorder.push_agent(pcm, 16_000, track_id=key)
                    if self._onset_detector is not None:
                        # Feed the same agent PCM the recorder saw — onset frame
                        # index is relative to this stream (sample rate 16k).
                        self._onset_detector.push(pcm)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.writer.emit(
                "sim.error",
                spec={
                    "where": "observer.agent_record",
                    "error": f"{type(e).__name__}: {e}",
                    "track_sid": key,
                },
                source="sim",
                include_dialogue=False,
            )
        finally:
            try:
                await stream.aclose()
            except Exception:
                pass

    def _on_agent_onset(self, onset_frame_idx: int) -> None:
        """Emit sim.agent.audio_onset with a timestamp backdated to the onset frame.

        The onset frame index is relative to this agent-stream t0, which is the
        recorder R-channel t0 (``recorder.started_mono``). Corrected run-relative
        time = (audio_t0_relative_to_run) + (onset_frame / sample_rate):
            ts_mono_ms = (started_mono - writer.t0_mono) + onset_to_audio_ms(...)
        This is the *perceived* agent speech onset — never detection time.
        """
        if self.recorder is None:
            return
        started = self.recorder.started_mono
        if started is None:
            return
        from ..audio.vad import onset_to_audio_ms

        audio_t0_mono = int((started - self.writer.t0_mono) * 1000)
        onset_ms = onset_to_audio_ms(onset_frame_idx, 16_000)
        corrected_ts = max(0, audio_t0_mono + onset_ms)
        ao = self.observe.audio_onset
        frames_before = 0
        if self._onset_detector is not None:
            frames_before = int(self._onset_detector.frames_before_first_onset or 0)
        self.writer.emit(
            "sim.agent.audio_onset",
            spec={
                "channel": "agent",
                "sample_rate": 16_000,
                "onset_frame_idx": onset_frame_idx,
                "frames_before_first_onset": frames_before,
                "vad": {
                    "method": ao.vad,
                    "threshold": ao.threshold,
                    "win_ms": ao.win_ms,
                    "energy_frames": ao.energy_frames,
                    "refractory_ms": ao.refractory_ms,
                },
            },
            source="sim",
            include_dialogue=False,
            turn=None,
            # Corrected to the onset frame — detection time is not audio onset.
            ts_mono_ms=corrected_ts,
        )

    async def finalize_session_snapshot(self) -> None:
        if self._agent_session is not None:
            # Drain late RemoteSession frames before snapshot/request (room may already be gone).
            await self._agent_session.drain_ingress(timeout_s=1.5)
            await self._agent_session.fetch_session_snapshot()

    async def drain_session_ingress(self, *, timeout_s: float = 1.5) -> None:
        """Public drain hook for post-disconnect grace (room-teardown tool race)."""
        if self._agent_session is not None:
            await self._agent_session.drain_ingress(timeout_s=timeout_s)

    async def detach(self) -> None:
        for t in self._record_tasks:
            t.cancel()
        if self._record_tasks:
            await asyncio.gather(*self._record_tasks, return_exceptions=True)
        self._record_tasks.clear()
        if self._agent_session is not None:
            await self._agent_session.detach()

    # ------------------------------------------------------------- transcription

    def _on_transcription_stream(self, reader: Any, participant_identity: str) -> None:
        asyncio.ensure_future(self._read_transcription(reader, participant_identity))

    async def _read_transcription(self, reader: Any, participant_identity: str) -> None:
        try:
            text = await reader.read_all()
        except Exception as e:
            self.writer.emit(
                "observer.error",
                spec={"where": "lk.transcription", "error": f"{type(e).__name__}: {e}"},
                source="lk.transcription",
                include_dialogue=False,
            )
            return
        attrs = dict(getattr(reader.info, "attributes", {}) or {})
        final = attrs.get(ATTR_FINAL, "").lower() == "true"
        segment_id = attrs.get(ATTR_SEGMENT_ID)
        if not text.strip():
            return

        role = "agent" if participant_identity == self.agent_identity else "user"
        self.on_transcript(role, text, final, segment_id=segment_id, source="lk.transcription")

    def _accept_final(self, role: str, text: str, source: str) -> bool:
        """Drop duplicate finals from lower-priority sources within the dedupe window."""
        norm = _normalize_text(text)
        if not norm:
            return False
        key = (role, norm)
        now = time.monotonic()
        window_s = self.observe.transcript_dedupe_window_ms / 1000
        prev = self._recent_finals.get(key)
        if prev is not None:
            prev_source, prev_mono = prev
            if now - prev_mono <= window_s:
                if _source_priority_rank(source, role) >= _source_priority_rank(prev_source, role):
                    return False
        self._recent_finals[key] = (source, now)
        return True

    def _role_has_final(self, role: str) -> bool:
        """True when this role already emitted a final in the current turn."""
        return role in self._finalized_roles

    def any_activity_occurred(self) -> bool:
        """True once any transcript (either role) has been observed."""
        return self._any_activity

    # Shared entry point — also used by the Gemini bridge (sim-side transcription).
    def on_transcript(
        self,
        role: str,
        text: str,
        final: bool,
        segment_id: str | None = None,
        source: str = "lk.transcription",
    ) -> None:
        now_ms = int(time.time() * 1000)
        self.writer.update_dialogue(role, text, final, at_ms=now_ms)

        spec: dict[str, Any] = {"text": text, "final": final}
        if segment_id:
            spec["segment_id"] = segment_id

        if role == "agent":
            self.last_agent_activity_mono = time.monotonic()
            # Real-time providers (OpenAI Realtime / Gemini Live) can deliver the
            # final transcript late, long after audio started. Mark the agent as
            # having spoken on ANY agent transcript (interim counts) so
            # consumers like InterruptRateRunner do not wait for the final.
            self._agent_has_spoken = True
        # Caller activity also counts: the dead-call net must not fire while
        # the agent is still working on its first reply (the agent has not
        # spoken yet, so last_agent_activity_mono would be stale).
        self.last_activity_mono = time.monotonic()
        self._any_activity = True

        if not final:
            # Drop late interims for a role whose final already landed — async
            # providers (OpenAI bridge) can deliver a trailing `.delta` after
            # `.done`, which previously produced interim-after-final within a
            # turn. Consumers must never see that inversion.
            #
            # Run 050 proved the same inversion happens via lk.transcription
            # delta streams: the SDK's delta-stream writer closes
            # asynchronously (flushTaskImpl), so interim chunks of an
            # ALREADY-FINALIZED segment keep arriving AFTER the turn advanced
            # (same segment_id, stale text). The role-level guard above cannot
            # catch them — the role's final set was cleared on begin_turn —
            # so finality is also tracked per (role, segment_id) below.
            if self._role_has_final(role):
                return
            if (
                source == "lk.transcription"
                and segment_id
                and (role, segment_id) in self._finalized_segments
            ):
                return
            self.writer.emit(f"transcript.{role}.interim", spec=spec, source=source)
            return

        # Ordering guard (OpenAI bridge only): a final may arrive before the
        # trailing interim of the same utterance (async `.delta` still pending
        # when `.done` lands). Emit a final-flavored interim first so consumers
        # never see interim-after-final within a turn.
        if source == "sim.openai" and self._last_interim_key != (role, text):
            self._last_interim_key = (role, text)
            self.writer.emit(
                f"transcript.{role}.interim",
                spec={**spec, "final": False},
                source=source,
            )

        if not self._accept_final(role, text, source):
            return

        if role == "user":
            norm = _normalize_text(text)
            # Same text as the turn's first final, from a DIFFERENT reporter.
            #
            # A caller utterance is reported twice: the room's own STT
            # (`lk.transcription`, rank 3) and the agent republishing its
            # transcript (`voice_ai.transcript`, rank 2). `_accept_final`
            # deliberately lets the better-ranked data-topic source through —
            # that is intentional, the agent's transcript is cleaner than LK ASR.
            # But when the TEXT is identical the second event carries no new
            # information, and `merge_as_same_turn` below would treat it as a
            # split-utterance continuation, so the run summary APPENDED it and
            # every caller line rendered doubled ("hello. ... hello. ...").
            #
            # Genuine repeats do not reach here: a same-source, same-text final
            # is already dropped by `_accept_final`'s dedupe window above.
            if norm and norm == self._current_turn_user_norm:
                return
            is_echo_of_prior_turn = self._agent_replied_this_turn and _similar_text(
                norm, self._current_turn_user_norm or ""
            )
            if is_echo_of_prior_turn:
                return
            merge_as_same_turn = (
                self._last_user_final_mono is not None and not self._agent_replied_this_turn
            )
            if (
                not merge_as_same_turn
                and self._last_user_final_mono is not None
                and self._agent_replied_this_turn
                and self._last_agent_final_mono is not None
            ):
                # STT/VAD can split one utterance into two finals with a
                # short/backchannel-like agent reply interjected in between
                # (e.g. "mm-hm", a premature partial answer). That reply is
                # not the turn-ending answer — treat the second half as a
                # continuation of the same turn instead of starting a new
                # one, so latency and transcript checks see the whole
                # utterance rather than a truncated first half.
                agent_gap_ms = (self._last_agent_final_mono - self._last_user_final_mono) * 1000
                agent_word_count = len((self._last_agent_final_text or "").split())
                if 0 <= agent_gap_ms <= _BACKCHANNEL_GRACE_MS and (
                    agent_word_count <= _BACKCHANNEL_MAX_WORDS
                ):
                    merge_as_same_turn = True
            if merge_as_same_turn:
                self.writer.emit(
                    "transcript.user.final",
                    spec={**spec, "same_turn": True},
                    source=source,
                )
                # Don't let the interjected reply count as this turn's
                # answer — keep waiting for the real one.
                self._agent_replied_this_turn = False
                return
            self._user_has_spoken = True
            self.turn += 1
            self._current_turn_user_norm = norm
            self.writer.begin_turn(self.turn)
            self._finalized_roles.clear()
            self._last_user_final_mono = time.monotonic()
            self._agent_replied_this_turn = False
            self._finalized_roles.add("user")
            if source == "lk.transcription" and segment_id:
                self._finalized_segments.add((role, segment_id))
            self.writer.emit("transcript.user.final", spec=spec, source=source)
        else:
            # A caller_contract single-path run (first_speaker=user) speaks
            # its opening `say:` via PublishSink directly into the mixer —
            # never through on_transcript(role=user) — so _user_has_spoken is
            # still False when the agent audibly answers that opening line.
            # The OLD behavior tagged it transcript.agent.preamble and
            # returned WITHOUT setting last_agent_final_*, which made
            # ObserverAgentWait blind to a real answer (run 011: AGENT_TIMEOUT
            # despite audible agent speech). A preamble event is still
            # emitted for turn-accounting, but the final-text fields are
            # ALWAYS recorded so agent evidence is never dropped.
            if self.turn == 0 and self.first_speaker == "user" and not self._user_has_spoken:
                self._last_agent_final_mono = time.monotonic()
                self._last_agent_final_text = text
                self._push_agent_final(text, segment_id)
                self.writer.emit(
                    "transcript.agent.preamble",
                    spec={**spec, "note": "agent spoke before user; not counted as a turn"},
                    source=source,
                )
                return
            if self.turn == 0:
                self.turn = 1
                self.writer.begin_turn(self.turn)
            if not self._agent_replied_this_turn and self._last_user_final_mono is not None:
                spec["turn_taking_ms"] = int(
                    (time.monotonic() - self._last_user_final_mono) * 1000
                )
            self._agent_replied_this_turn = True
            self._last_agent_final_mono = time.monotonic()
            self._last_agent_final_text = text
            self._push_agent_final(text, segment_id)
            self._finalized_roles.add("agent")
            if source == "lk.transcription" and segment_id:
                self._finalized_segments.add((role, segment_id))
            self.writer.emit("transcript.agent.final", spec=spec, source=source)

    # --------------------------------------------------------------- data topics

    def _handle_data_topic(self, topic: str, packet: rtc.DataPacket) -> None:
        sender = packet.participant.identity if packet.participant else None
        try:
            payload = json.loads(packet.data.decode("utf-8"))
        except Exception:
            self.writer.emit(
                "data.raw",
                spec={"topic": topic, "bytes": len(packet.data), "sender": sender},
                source=topic or "data",
                include_dialogue=False,
            )
            return

        emitted_tool = self._match_tool_patterns(topic, payload)
        parsed = self._parse_transcript_payload(payload)
        if parsed is not None:
            role, text, turn_id = parsed
            self.on_transcript(
                role, text, final=True, source=topic or "data", segment_id=turn_id
            )
            return
        if not emitted_tool:
            self.writer.emit(
                "data.message",
                spec={"topic": topic, "sender": sender, "payload": payload},
                source=topic or "data",
            )

    def _parse_transcript_payload(
        self, payload: dict[str, Any]
    ) -> tuple[str, str, str | None] | None:
        """Generic transcript_turn shape — any data topic, not hardcoded to one worker.

        Returns ``(role, text, turn_id)``. The third element is EVIDENCE ONLY.

        `turnId` sits at the top level of the wire payload, next to `turn`, and
        was being dropped. That is why every `voice_ai.transcript` row in a
        report carries no segment id (`seg=-`) while `lk.transcription` rows
        carry a real one — so on a data-channel transcript it was impossible to
        tell "the same utterance published twice" from "two utterances". Three
        sessions reasoned wrongly about run 062 before that was noticed.

        It is namespaced (`data:<turnId>`) so it can never collide with a
        LiveKit transcriber segment id, which shares the same
        `_finalized_segments` key space.

        It is deliberately NOT wired into the segment dedup. `_finalized_segments`
        is only populated for `source == "lk.transcription"`, so passing a
        data-channel id there changes nothing today; and if that gate were ever
        opened, a worker re-publishing a corrected final under the same id would
        be silently dropped. Evidence yes, control no — the actual duplicate
        (a worker sealing one utterance at 9, then 55, then 69 characters) mints
        a NEW id each time, so no id-based rule can collapse it.
        """
        if payload.get("type") not in self.observe.transcript_payload_types:
            return None
        if payload.get("interim"):
            return None
        turn = payload.get("turn")
        if not isinstance(turn, dict):
            return None
        role = turn.get("role")
        text = turn.get("text")
        if role not in ("user", "agent") or not isinstance(text, str) or not text.strip():
            return None
        turn_id = payload.get("turnId")
        if not isinstance(turn_id, str) or not turn_id.strip():
            turn_id = None
        else:
            turn_id = f"data:{turn_id}"
        return role, text.strip(), turn_id

    def _match_tool_patterns(self, topic: str, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        for pattern in self.observe.tool_event_patterns:
            if self._pattern_matches(pattern, topic, payload):
                self._emit_tool_event(pattern.emit, topic, payload)
                return True
        return False

    @staticmethod
    def _pattern_matches(pattern: ToolEventPattern, topic: str, payload: dict[str, Any]) -> bool:
        for key, expected in pattern.match.items():
            if key == "topic":
                if topic != expected:
                    return False
                continue
            if _lookup_path(payload, key) != expected:
                return False
        return True

    def _emit_tool_event(self, emit_kind: str, topic: str, payload: dict[str, Any]) -> None:
        name = payload.get("tool") or payload.get("name") or _lookup_path(payload, "spec.name")
        call_id = (
            payload.get("call_id")
            or payload.get("toolCallId")
            or _lookup_path(payload, "spec.call_id")
        )
        spec: dict[str, Any] = {"name": name, "call_id": call_id, "payload": payload}

        if emit_kind == "tool.start":
            event = self.writer.emit("tool.start", spec=spec, source=topic)
            if call_id:
                self._open_tools[str(call_id)] = event
            return

        parent_id: str | None = None
        if call_id:
            start = self._open_tools.pop(str(call_id), None)
            if start is not None:
                parent_id = start["event_id"]
                spec["duration_ms"] = (
                    int((time.monotonic() - self.writer.run_start_mono) * 1000)
                    - start["ts_mono_ms"]
                )
        if emit_kind == "tool.error":
            spec["error"] = (
                payload.get("error") or payload.get("message") or _lookup_path(payload, "spec.error")
            )
        self.writer.emit(emit_kind, spec=spec, source=topic, parent_event_id=parent_id)
