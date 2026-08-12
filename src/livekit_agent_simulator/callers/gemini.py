"""Gemini Live simulated caller bridged into the LiveKit room.

Wire rules (verified for gemini-3.1-flash-live-preview, native audio):
    - response_modalities MUST be [AUDIO]; requesting TEXT closes the socket with 1011.
    - Input audio: raw PCM16 mono @16000 Hz via send_realtime_input(audio=Blob(...,
      mime_type="audio/pcm;rate=16000")).
    - Output audio: PCM16 mono @24000 Hz in server_content.model_turn parts inline_data.
    - Caller/agent text comes from input_audio_transcription / output_audio_transcription.
    - server_content.interrupted signals barge-in (agent audio interrupted the sim, or vice versa).

LiveKit side:
    - Agent audio in: rtc.AudioStream(track, sample_rate=16000) — SDK resamples 48k→16k.
    - Sim audio out: rtc.AudioSource(24000, 1) — no manual resampling; WebRTC handles playback.
"""

from __future__ import annotations

import array
import asyncio
import math
import sys
import time
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Sequence

from google import genai
from google.genai import types
from livekit import rtc

from ..audio.local_recorder import LocalConversationRecorder
from ..audio.mic_mixer import ParallelMicMixer
from ..audio.pcm_cue import load_wav_pcm, resolve_cue_asset
from ..audio.degradation import EffectFn
from ..config import SimConfig
from .end_call import (
    END_CALL_TOKEN,
    contains_end_call_signal,
    contains_farewell_signal,
    should_end_call_on_turn,
    strip_end_call_signal,
    strip_farewell_signal,
)

if TYPE_CHECKING:
    from ..livekit.observer import Observer
    from ..logging.event_writer import EventWriter

GEMINI_IN_RATE = 16_000
GEMINI_OUT_RATE = 24_000

# Gate agent→Gemini PCM and commit turns with activity_start/activity_end.
# Auto VAD stays OFF: continuous WebRTC silence + auto VAD never reliably
# committed agent turns for freestyle; audio_stream_end alone unlocked hearing
# (017) but not talkativeness. Manual activity markers are the Live API
# contract for forcing generation (ai.google.dev Live capabilities).
_AGENT_SPEECH_RMS_THRESHOLD = 100.0
# Docs: ≥500ms end-of-speech for manual client VAD quality.
_AGENT_STREAM_END_SILENCE_MS = 650
_AGENT_SPEECH_START_FRAMES = 1
_AGENT_TRAILING_PAD_MS = 120.0

__all__ = [
    "END_CALL_TOKEN",
    "GeminiCallerBridge",
    "pcm16_mono_rms",
    "resolve_voice_gain",
    "script_speak_directive",
]


def pcm16_mono_rms(pcm: bytes) -> float:
    """RMS of little-endian PCM16 mono bytes (0.0 if empty)."""
    if len(pcm) < 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    acc = 0.0
    for s in samples:
        acc += float(s) * float(s)
    return math.sqrt(acc / len(samples))


def script_speak_directive(text: str, *, hangup_farewell: bool = False) -> str:
    """Realtime-input text that drives a Script ``say`` through Gemini Live TTS.

    Keep this message to a single job: speak the milestone line verbatim.
    Freestyle-after-cue belongs in system instruction only — putting "continue
    naturally" in the same realtime turn made Gemini paraphrase/expand the line
    (and inflated natural_* metrics when say-matching failed).
    Hang-up farewell is the exception: speak once, then stay quiet for disconnect.

    Role lock matters: agent audio is bridged into Live as input, so a bare
    "speak this" kick often continues in the *assistant* persona. Explicitly
    separate "the other party you just heard" from "you (the caller)".
    """
    line = str(text or "").strip()
    if hangup_farewell:
        return (
            "SIMULATOR CUE — ignore silence rules for this one turn only. "
            "You are the HUMAN CALLER (not the assistant). "
            "Speak the following goodbye aloud now as the phone caller, exactly once, "
            "then stop and wait silently for disconnect:\n"
            f"{line}"
        )
    return (
        "PRIVATE SIMULATOR CUE — do not read these instructions aloud. "
        "You are UNMISTAKABLY the HUMAN CALLER on this phone call. "
        "Any other voice you just heard is the assistant (the other party) — "
        "never speak as them, never greet callers, never offer to help or check "
        "availability for someone else. "
        "Ignore silence rules for this one turn only. "
        "Speak aloud now, exactly once, ONLY the caller line between <<< and >>>. "
        "Verbatim: no paraphrase, no extra words before or after, no added fillers.\n"
        f"<<<\n{line}\n>>>\n"
        # Do not say "stay silent" / "end immediately" — that over-conditioned Live
        # into cue-only mute between milestones. Freestyle-after lives in SI.
        "After that exact line, stop this cue turn."
    )


def _inject_matches_say(heard: str, say: str) -> bool:
    """True when Live output transcription is close enough to the Script say."""
    from ..web.speech_origin import _mostly_script_say

    return _mostly_script_say(heard, say)


# Freestyle bleed: Live sometimes continues as the assistant after hearing them.
# Portable English staff-cues — not product-specific names.
_ASSISTANT_PERSONA_CUES = (
    "thanks for calling",
    "thank you for calling",
    "how can i help",
    "how may i help",
    "let me check that for you",
    "let me check on that for you",
    "i'd be happy to help",
    "i would be happy to help",
    "we're here to help",
    "we are here to help",
    "i'll check that for you",
    "i will check that for you",
)


def looks_like_assistant_persona(text: str) -> bool:
    """True when caller STT looks like staff/assistant speech (role-flip)."""
    t = " ".join(str(text or "").lower().split())
    if not t:
        return False
    return any(cue in t for cue in _ASSISTANT_PERSONA_CUES)


def _is_voice_cue_asset(asset: str | None) -> bool:
    """True for voice.* refs (spoken script lines), not noise.*."""
    if not asset:
        return False
    name = str(asset).strip().lower()
    if name.startswith("builtin:"):
        name = name[len("builtin:") :]
    if name.startswith("@"):
        name = name[1:]
    return name.startswith("voice.")


def resolve_voice_gain(persona: dict[str, Any] | None) -> float:
    """Linear gain for sim *speech* (freestyle + inject). Noise beds are unaffected.

    Persona.speech_conditions.voice_gain | voice_volume | volume in [0.0, 1.0].
    Default 1.0. Quiet-caller STT stress typically uses 0.25–0.45.
    Gemini Live has no native volume API — this scales PCM after the model.
    """
    if not isinstance(persona, dict):
        return 1.0
    sc = persona.get("speech_conditions") or persona.get("speechConditions") or {}
    if not isinstance(sc, dict):
        return 1.0
    raw = sc.get("voice_gain", sc.get("voice_volume", sc.get("volume", 1.0)))
    try:
        gain = float(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(
            "Persona.speech_conditions.voice_gain must be a number between 0.0 and 1.0"
        ) from e
    if not 0.0 <= gain <= 1.0:
        raise ValueError(
            "Persona.speech_conditions.voice_gain must be between 0.0 and 1.0 "
            f"(got {gain})"
        )
    return gain


class GeminiCallerBridge:
    """Owns the Gemini Live session + the LiveKit audio tracks of the simulated caller."""

    def __init__(
        self,
        cfg: SimConfig,
        room: rtc.Room,
        observer: "Observer",
        writer: "EventWriter",
        persona_system_prompt: str,
        first_speaker: str,
        recorder: LocalConversationRecorder | None = None,
        midcall_cues: list | None = None,
        voice_gain: float = 1.0,
        silent_mode: bool = False,
        audio_effects: Sequence[EffectFn] = (),
    ) -> None:
        self.cfg = cfg
        self.room = room
        self.observer = observer
        self.writer = writer
        self.persona_system_prompt = persona_system_prompt
        self.first_speaker = first_speaker
        self.recorder = recorder
        # Quiet-caller STT stress: scale freestyle + speech inject PCM (not noise).
        # Gemini Live has no native volume API — post-scale PCM after the model.
        if not 0.0 <= float(voice_gain) <= 1.0:
            raise ValueError(f"voice_gain must be between 0.0 and 1.0 (got {voice_gain})")
        self._voice_gain = float(voice_gain)
        # Coval Silent Mode: never freestyle-speak; hang-up farewell still allowed.
        self._silent_mode = bool(silent_mode)
        # Post-mix audio degradation (packet loss / echo / phone / static) for the sim mic.
        self._audio_effects: list[EffectFn] = list(audio_effects)
        # Dialog steering texts from CallerPolicy (bootstrap / reground); not PCM Script.
        self._midcall_cues = list(midcall_cues or [])

        self.end_call = asyncio.Event()
        # True when the Gemini Live socket died mid-call (transport drop), so the
        # orchestrator can distinguish a natural hang-up from a connection failure
        # instead of masking it as `sim_end_call`.
        self.transport_dropped = False
        # Gemini Live session resumption (connection ~10-min cap, Google docs).
        # The server sends session_resumption_update(new_handle) periodically and
        # go_away before closing; we save the handle and reconnect so calls can
        # exceed ~10 min without losing conversation context.
        self._resume_handle: str | None = None
        self._reconnect_required = asyncio.Event()
        self._reconnect_count = 0
        self._session_generation = 0
        self._agent_track_queue: asyncio.Queue[rtc.RemoteAudioTrack] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._source: rtc.AudioSource | None = None
        # Parallel speech+noise into one AudioSource (single writer, multi-layer mix).
        self._mixer: ParallelMicMixer | None = None
        self._sim_out_text = ""
        self._live_session: Any | None = None
        self._suppress_output_until_mono: float | None = None
        # Caller-audio onset latch: first push_speech per utterance emits
        # sim.caller.audio_source_start; reset when a new utterance begins.
        self._user_audio_source_emitted: bool = False
        # Scripted user long-silence: hold persona + pause dead_call until this mono time (+ grace).
        self._script_hold_until_mono: float | None = None
        self._script_hold_grace_s: float = 20.0
        # Linear gain for script-injected gemini_text playback (reset on turn_complete).
        self._inject_playback_gain: float = 1.0
        self._inject_turn_active: bool = False
        self._inject_heard_text: str = ""
        self._inject_playout_done: asyncio.Event = asyncio.Event()
        self._agent_audio_paused: bool = False
        # Agent→Gemini stream gate (manual VAD): True while activity_start is open.
        self._agent_stream_open: bool = False
        self._agent_speech_frames: int = 0
        self._agent_silence_ms: float = 0.0
        # Drop persona PCM after hang-up token / spoken "end call" is detected.
        self._mute_persona_audio = False
        # When Script steps remain, freestyle bye/[END_CALL] must not tear the room down.
        self._script_pending: Callable[[], bool] | None = None
        # True while Script hang_up is injecting a spoken farewell (must not mute it).
        self._script_hangup_farewell = False

    def bind_script_pending(self, is_pending: Callable[[], bool] | None) -> None:
        """Wire ScriptRunner.has_pending_steps (or equivalent). None = no script gate."""
        self._script_pending = is_pending

    def _script_steps_pending(self) -> bool:
        fn = self._script_pending
        if fn is None:
            return False
        try:
            return bool(fn())
        except Exception:
            return False

    def begin_script_hangup_farewell(self) -> None:
        """Allow Script goodbye TTS past suppress/mute gates."""
        self._script_hangup_farewell = True
        self._suppress_output_until_mono = None
        self._mute_persona_audio = False

    def end_script_hangup_farewell(self) -> None:
        self._script_hangup_farewell = False

    async def drain_persona_speech(self, *, timeout_s: float = 4.0) -> None:
        """Wait for queued sim speech to leave the mic (goodbye playout)."""
        await self._drain_persona_speech(timeout_s=timeout_s)

    # ------------------------------------------------------------------ setup

    def watch_agent_tracks(self, agent_identity: str) -> None:
        """Subscribe to a specific remote participant's audio (WebRTC agent path)."""

        def _maybe_queue(p: rtc.RemoteParticipant, track: rtc.Track) -> None:
            if p.identity == agent_identity and track.kind == rtc.TrackKind.KIND_AUDIO:
                self._agent_track_queue.put_nowait(track)

        @self.room.on("track_subscribed")
        def _on_track(
            track: rtc.Track, pub: rtc.RemoteTrackPublication, p: rtc.RemoteParticipant
        ) -> None:
            _maybe_queue(p, track)

        # Track may already be subscribed before this handler attaches.
        for p in self.room.remote_participants.values():
            if p.identity != agent_identity:
                continue
            for pub in p.track_publications.values():
                if pub.track is not None and pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                    self._agent_track_queue.put_nowait(pub.track)

    def watch_sip_audio_tracks(self) -> None:
        """Subscribe to any remote SIP (or non-local) audio on sim_room (hairpin path).

        On Cloud hairpin, agent audio arrives as the SIP participant track in sim-room.
        We accept the first remote audio track that is not our own publish.
        """

        def _is_sip_like(p: rtc.RemoteParticipant) -> bool:
            kind = getattr(p, "kind", None)
            try:
                from livekit.protocol.models import ParticipantInfo

                kind_name = ParticipantInfo.Kind.Name(kind) if kind is not None else ""
                if kind_name == "SIP":
                    return True
            except Exception:
                pass
            attrs = getattr(p, "attributes", None) or {}
            if isinstance(attrs, dict) and any(str(k).startswith("sip.") for k in attrs):
                return True
            # Fallback: any remote participant audio on sim-room (hairpin leg).
            return True

        def _maybe_queue(p: rtc.RemoteParticipant, track: rtc.Track) -> None:
            if track.kind != rtc.TrackKind.KIND_AUDIO:
                return
            if not _is_sip_like(p):
                return
            self._agent_track_queue.put_nowait(track)

        @self.room.on("track_subscribed")
        def _on_track(
            track: rtc.Track, pub: rtc.RemoteTrackPublication, p: rtc.RemoteParticipant
        ) -> None:
            _maybe_queue(p, track)

        for p in self.room.remote_participants.values():
            for pub in p.track_publications.values():
                if pub.track is not None and pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                    _maybe_queue(p, pub.track)

    def watch_agent_tracks_on_room(
        self, room: rtc.Room, agent_identity: str
    ) -> None:
        """Subscribe to agent audio on a *different* room than Gemini mic (SIP 2-room).

        Outbound hairpin often never places a SIP track in sim-room (same DID as
        agent inbound). Gemini still needs agent PCM to continue the conversation —
        feed it from agent-room where the LiveKit agent publishes WebRTC audio.
        """

        def _maybe_queue(p: rtc.RemoteParticipant, track: rtc.Track) -> None:
            if p.identity != agent_identity:
                return
            if track.kind != rtc.TrackKind.KIND_AUDIO:
                return
            self._agent_track_queue.put_nowait(track)

        @room.on("track_subscribed")
        def _on_track(
            track: rtc.Track, pub: rtc.RemoteTrackPublication, p: rtc.RemoteParticipant
        ) -> None:
            _maybe_queue(p, track)

        for p in room.remote_participants.values():
            if p.identity != agent_identity:
                continue
            for pub in p.track_publications.values():
                if pub.track is not None and pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                    self._agent_track_queue.put_nowait(pub.track)

        self.writer.emit(
            "sim.agent_listen_room",
            spec={
                "agent_identity": agent_identity,
                "listen": "agent_room",
                "note": "Gemini ears on agent-room WebRTC (sim-room SIP track missing)",
            },
            source="sim",
            include_dialogue=False,
        )

    async def publish_mic(self) -> rtc.AudioSource:
        self._source = rtc.AudioSource(GEMINI_OUT_RATE, 1)
        track = rtc.LocalAudioTrack.create_audio_track("lks-mic", self._source)
        await self.room.local_participant.publish_track(
            track,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )
        if self.recorder is not None:
            self.recorder.mark_start()
        # Mixer owns capture_frame; speech (Gemini) + noise (room_pcm) mix in parallel.
        self._mixer = ParallelMicMixer(
            self._source,
            sample_rate=GEMINI_OUT_RATE,
            recorder=self.recorder,
            effects=self._audio_effects,
        )
        self._mixer.start()
        self.writer.emit(
            "sim.mic_published",
            spec={"sample_rate": GEMINI_OUT_RATE, "mixer": "parallel"},
            source="sim",
            include_dialogue=False,
        )
        return self._source

    # -------------------------------------------------------------------- run

    async def run(self) -> None:
        # websockets 16 defaults ping_interval=20s / ping_timeout=20s: Gemini Live
        # sometimes misses a pong during long model processing, the library then
        # closes with 1011 "keepalive ping timeout" → APIError 1006 → the caller
        # marks the drop retryable=false and the run dies mid-call (~4 min in).
        # Raise both via HttpOptions.async_client_args (passed through as
        # ws_connect kwargs by google-genai live.connect).
        client = genai.Client(
            api_key=self.cfg.simulator.api_key,
            http_options=types.HttpOptions(
                async_client_args={"ping_interval": 30, "ping_timeout": 60},
            ),
        )
        voice = self.cfg.simulator.voice

        # publish_mic() publishes the sim mic track + starts the mixer ONCE —
        # it must NOT be re-run on a session reconnect (would double-publish).
        source = await self.publish_mic()

        session_cm: Any | None = None
        try:
            while True:
                # Manual activity markers (auto VAD disabled). Speech-gated PCM +
                # activity_end commits the agent turn so Live generates caller freestyle.
                # session_resumption: first connect uses no handle; reconnects (after
                # go_away) pass the saved handle to resume conversation context past
                # the ~10-min connection cap.
                config = types.LiveConnectConfig(
                    response_modalities=[types.Modality.AUDIO],  # AUDIO only — TEXT → 1011 close
                    input_audio_transcription=types.AudioTranscriptionConfig(),
                    output_audio_transcription=types.AudioTranscriptionConfig(),
                    realtime_input_config=types.RealtimeInputConfig(
                        automatic_activity_detection=types.AutomaticActivityDetection(
                            disabled=True,
                        ),
                    ),
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice.voice)
                        ),
                        language_code=voice.language,
                    ),
                    system_instruction=types.Content(
                        parts=[types.Part(text=self.persona_system_prompt)]
                    ),
                    session_resumption=types.SessionResumptionConfig(
                        handle=self._resume_handle,
                    ),
                )

                session_cm, session = await self._connect_live_with_retry(
                    client, voice.model, config
                )
                try:
                    self._live_session = session
                    self.writer.emit(
                        "sim.gemini_connected",
                        spec={
                            "model": voice.model,
                            "voice": voice.voice,
                            "language": voice.language,
                            "voice_gain": self._voice_gain,
                            "silent_mode": bool(getattr(self, "_silent_mode", False)),
                            "resume": bool(self._resume_handle),
                        },
                        source="sim",
                        include_dialogue=False,
                    )
                    await self._emit_bootstrap_cues(session)

                    self._tasks = [
                        asyncio.create_task(self._pump_agent_audio(session), name="agent->gemini"),
                        asyncio.create_task(self._pump_gemini_events(session, source), name="gemini->lk"),
                    ]
                    try:
                        await self.end_call.wait()
                    finally:
                        await self._flush_agent_audio_stream(
                            session, reason="session_teardown"
                        )
                        self._live_session = None
                        for t in self._tasks:
                            t.cancel()
                        await asyncio.gather(*self._tasks, return_exceptions=True)
                finally:
                    # Close this connection's SDK context manager before reconnecting.
                    try:
                        await session_cm.__aexit__(None, None, None)
                    except Exception:
                        pass
                    session_cm = None

                # Normal end (stop / sim_hang_up / end-call token / fatal drop) → done.
                if not self._reconnect_required.is_set():
                    break

                # Gemini sent go_away (connection about to be reset, ~10-min cap)
                # OR mid-call transport drop (1006/1011 with a resumption handle) —
                # resume the session on a fresh connection with the saved handle.
                self._reconnect_required.clear()
                self.end_call.clear()
                self._session_generation += 1
                self._reconnect_count += 1
                if self._reconnect_count > 2:
                    # Bounded mid-call reconnect: a server that keeps resetting the
                    # socket won't recover; stop hammering and end the call.
                    self.end_call.set()
                    break
                self.writer.emit(
                    "sim.gemini_reconnecting",
                    spec={"generation": self._session_generation},
                    source="sim",
                    include_dialogue=False,
                )
        finally:
            if session_cm is not None:
                try:
                    await session_cm.__aexit__(None, None, None)
                except Exception:
                    pass
            # Mixer is torn down once, after the whole session (incl. reconnects).
            if self._mixer is not None:
                await self._mixer.aclose()
                self._mixer = None

    async def _connect_live_with_retry(
        self, client: Any, model: str, config: Any
    ) -> tuple[Any, Any]:
        """Open the Gemini Live session, retrying transient transport drops.

        ``client.aio.live.connect()`` returns an *async context manager*; its
        ``__aenter__`` performs the WebSocket handshake and yields the live
        session. We enter it exactly once (consuming the generator's first
        yield), and return ``(cm, session)`` so the caller holds the manager for
        teardown while using the session for dialogue. The google-genai SDK has
        no built-in reconnect (``receive()`` TODO b/365983264) and websockets'
        20s ping timeout can tear the socket down with no close frame ->
        ``APIError 1006`` / ``ConnectionClosedError`` within the first ~20-40s.
        Retry the *handshake* a bounded number of times with backoff before
        giving up; once dialogue has begun we do not reconnect (that would drop
        the persona's mid-call context). Each drop is emitted as a diagnostic
        event so reports can distinguish transport failures from natural
        hang-ups.
        """
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            cm = client.aio.live.connect(model=model, config=config)
            try:
                session = await cm.__aenter__()
                return cm, session
            except Exception as e:
                is_transport = (
                    isinstance(e, ConnectionError)
                    or "1006" in str(e)
                    or "1008" in str(e)  # known Gemini preview-model transient (tool-call crash)
                    or "abnormal closure" in str(e).lower()
                    or "ConnectionClosed" in type(e).__name__
                )
                self.writer.emit(
                    "sim.gemini_socket_drop",
                    spec={
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "error": f"{type(e).__name__}: {e}",
                        "retryable": is_transport,
                    },
                    source="sim",
                    include_dialogue=False,
                )
                try:
                    await cm.__aexit__(*sys.exc_info())
                except Exception:
                    pass
                if not is_transport or attempt == max_attempts:
                    raise
                await asyncio.sleep(min(2.0 * attempt, 6.0))
        raise RuntimeError("unreachable")  # pragma: no cover

    async def _flush_agent_audio_stream(self, session: Any, *, reason: str) -> None:
        """End agent activity with ``activity_end`` (manual VAD) so Live generates."""
        if not self._agent_stream_open or session is None:
            self._agent_stream_open = False
            self._agent_speech_frames = 0
            self._agent_silence_ms = 0.0
            return
        try:
            await session.send_realtime_input(activity_end=types.ActivityEnd())
            self.writer.emit(
                "sim.gemini_activity",
                spec={"edge": "activity_end", "reason": reason},
                source="sim",
                include_dialogue=False,
            )
        except Exception as e:  # noqa: BLE001
            # A redundant/early `activity_end` after the model already ended its
            # turn (or while audio is mid-flight) makes Gemini close the socket
            # with 1007 "invalid frame payload data". The stream is effectively
            # already closed — this must NOT be treated as a fatal sim.error
            # (the research: activity_end after model turn → 1007, harmless).
            err = f"{type(e).__name__}: {e}"
            self.writer.emit(
                "sim.gemini_activity",
                spec={"edge": "activity_end_skipped", "reason": reason, "error": err},
                source="sim",
                include_dialogue=False,
            )
        self._agent_stream_open = False
        self._agent_speech_frames = 0
        self._agent_silence_ms = 0.0

    async def _emit_bootstrap_cues(self, session: Any) -> None:
        """Emit connect-time midcall texts (``kind=bootstrap`` only).

        Default policy: speak-first kick for dialogue ``user`` without Script;
        never bootstrap when Script owns the open line (avoids double-open).
        """
        for cue in self._midcall_cues:
            kind = getattr(cue, "kind", "") or ""
            if kind != "bootstrap":
                continue
            text = str(getattr(cue, "text", "") or "").strip()
            if not text:
                continue
            await session.send_realtime_input(text=text)
            self.writer.emit(
                "sim.caller_midcall",
                spec={
                    "kind": kind,
                    "label": getattr(cue, "label", None),
                    "text": text[:240],
                },
                source="sim",
                include_dialogue=False,
            )

    async def inject_reground(self, *, label: str | None = None) -> None:
        """Inject the first reground MidcallCue (goal focus). No-op if none / session down."""
        if self._live_session is None:
            return
        for cue in self._midcall_cues:
            if getattr(cue, "kind", "") != "reground":
                continue
            text = str(getattr(cue, "text", "") or "").strip()
            if not text:
                continue
            await self._live_session.send_realtime_input(text=text)
            self.writer.emit(
                "sim.caller_midcall",
                spec={
                    "kind": "reground",
                    "label": label or getattr(cue, "label", None),
                    "text": text[:240],
                },
                source="sim",
                include_dialogue=False,
            )
            return

    async def release_after_milestone(self) -> None:
        """No-op placeholder — do not send midcall text after Script says.

        Earlier versions sent a 'resume conversation' realtime text after each
        milestone. On Gemini Live that text is a user turn and frequently caused
        role-flip (caller speaking as the agent) or double-opens. Freestyle after
        cues is owned by SI + ``nudge_freestyle_answer`` on unanswered questions.
        """
        return

    async def nudge_freestyle_answer(self, agent_hint: str = "") -> None:
        """Non-text activation: ``activity_end`` so manual VAD commits the agent turn.

        Midcall ``send_realtime_input(text=...)`` restores talkativeness but
        role-flips the caller into the assistant. Ending an *open* agent activity
        asks Live to generate from audio context only — no persona text.

        No-op when the stream is already closed (redundant ends caused Live 1006).
        """
        _ = agent_hint
        if self._live_session is None:
            return
        if self._silent_mode or self._script_hangup_farewell:
            return
        if self._inject_turn_active or self._agent_audio_paused:
            return
        if self._persona_output_suppressed():
            return
        if not self._agent_stream_open:
            return
        session = self._live_session
        try:
            await self._flush_agent_audio_stream(session, reason="freestyle_nudge")
        except Exception as e:  # noqa: BLE001 — pacing must not die on nudge
            self.writer.emit(
                "sim.error",
                spec={
                    "where": "nudge_freestyle_answer",
                    "error": f"{type(e).__name__}: {e}",
                },
                source="sim",
                include_dialogue=False,
            )

    def stop(self) -> None:
        self.end_call.set()
        if self._mixer is not None:
            self._mixer.clear_noise()
            self._mixer.stop()

    def sim_hang_up(self) -> None:
        """Called by ScriptRunner action=hang_up : hard disconnect from the room."""
        if self._mixer is not None:
            self._mixer.clear_noise()
        self.writer.emit(
            "sim.hang_up",
            spec={"source": "script", "by": "sim"},
            source="sim",
            include_dialogue=False,
        )
        self.end_call.set()

    def suppress_persona_output(self, duration_ms: int) -> None:
        """Block Gemini audio/text to the room after a scripted PCM cue (caller silence)."""
        if duration_ms <= 0:
            return
        until = time.monotonic() + duration_ms / 1000
        prev = self._suppress_output_until_mono
        self._suppress_output_until_mono = until if prev is None else max(prev, until)

    def begin_scripted_user_silence(
        self,
        duration_ms: int,
        *,
        grace_s: float = 20.0,
        mute_persona: bool = False,
    ) -> None:
        """Hold dead_call grace for a wait step; optionally mute freestyle TTS.

        Default ``mute_persona=False`` is pacing without forcing the caller mute.
        Pass ``mute_persona=True`` for intentional dead-air / unresponsive tests.
        """
        if duration_ms <= 0:
            return
        until = time.monotonic() + duration_ms / 1000
        prev = self._script_hold_until_mono
        self._script_hold_until_mono = until if prev is None else max(prev, until)
        self._script_hold_grace_s = max(self._script_hold_grace_s, float(grace_s))
        if mute_persona:
            self.suppress_persona_output(duration_ms)

    def scripted_silence_active(self) -> bool:
        """True while scripted silence is holding or within post-hold grace (agent may re-engage)."""
        if self._script_hold_until_mono is None:
            return False
        grace = self._script_hold_grace_s
        if time.monotonic() <= self._script_hold_until_mono + grace:
            return True
        self._script_hold_until_mono = None
        return False

    def _allow_persona_room_audio(self) -> bool:
        """Whether Gemini Live PCM may enter the room as caller audio.

        Script still owns barge/hang-up timing, but freestyle answers between
        cues are allowed (main-compatible). Farewell / END_CALL freestyle is
        muted separately via ``_mute_hang_up_audio`` + deferred end_call.
        Script inject and hang-up farewell always pass.

        Silent mode: freestyle is always blocked (dead-air / unresponsive caller).
        """
        if self._script_hangup_farewell:
            return True
        # gemini_text Script inject drives TTS through the same PCM path.
        if self._inject_turn_active:
            return True
        if getattr(self, "_silent_mode", False):
            return False
        if self._mute_persona_audio or self._persona_output_suppressed():
            return False
        return True

    def _persona_output_suppressed(self) -> bool:
        if self._suppress_output_until_mono is None:
            return False
        if time.monotonic() >= self._suppress_output_until_mono:
            self._suppress_output_until_mono = None
            return False
        return True

    async def inject_cue(
        self,
        text: str,
        *,
        label: str = "script",
        delivery: str = "gemini_text",
        asset: str | None = None,
        scenario_dir: Path | None = None,
        gain: float = 1.0,
        loop: bool = False,
    ) -> None:
        """Inject caller speech while the agent is talking.

        ``loop=True`` (room_pcm noise only) starts a continuous ambient bed that
        re-queues until hang-up / mixer stop. Does not block the Script runner.
        """
        # Silent mode: no speech/noise inject. Hang-up farewell sets _script_hangup_farewell.
        if getattr(self, "_silent_mode", False) and not self._script_hangup_farewell:
            self.writer.emit(
                "sim.silent_mode_skip_inject",
                spec={"label": label, "delivery": delivery, "text": (text or "")[:120]},
                source="sim",
                include_dialogue=False,
            )
            return
        # A scripted line is its own caller utterance — arm the audio-onset latch.
        self._reset_user_audio_source_latch()
        if delivery == "room_pcm":
            if self._mixer is None or self._source is None:
                raise RuntimeError("Sim mic/mixer not ready — cannot play room_pcm cue")
            if not asset:
                raise ValueError("room_pcm cue requires asset")
            if loop and _is_voice_cue_asset(asset):
                raise ValueError("loop is for noise/ambient beds, not voice.* speech assets")
            wav_path = resolve_cue_asset(
                asset,
                scenario_dir=scenario_dir,
                project_root=self.cfg.project_root,
                cues_config=getattr(self.cfg, "cues", None),
            )
            pcm, rate, channels = load_wav_pcm(wav_path)
            if channels != 1:
                raise ValueError("Only mono room_pcm assets are supported")
            if rate != GEMINI_OUT_RATE:
                raise ValueError(
                    f"room_pcm asset rate {rate} != sim mic {GEMINI_OUT_RATE} "
                    f"(resample cue WAV): {wav_path}"
                )
            duration_s = max(0.05, len(pcm) / 2 / rate)
            # Vocal speech (voice.*): play on speech layer + suppress free persona TTS so
            # goodbye/[END_CALL] cannot override the scripted words (SoT = mic audio).
            # Noise layers stay on push_noise so they can ride under persona speech.
            vocal = _is_voice_cue_asset(asset)
            if vocal:
                if loop:
                    raise ValueError("loop is not supported for voice.* speech assets")
                self.suppress_persona_output(int(duration_s * 1000) + 400)
                speech_gain = max(0.0, min(1.0, float(gain) * self._voice_gain))
                self._emit_user_audio_source_start(gain=speech_gain, via="inject_voice_wav")
                self._mixer.push_speech(pcm, gain=speech_gain)
                # Complete WAV — not burst TTS; drain without jitter waterline hold.
                self._mixer.end_speech_turn()
                mix = "speech"
                await asyncio.sleep(duration_s)
            else:
                # Noise beds use step gain only (not quiet-caller voice_gain).
                self._mixer.push_noise(pcm, gain=gain, loop=loop)
                mix = "parallel_loop" if loop else "parallel"
                if not loop:
                    # One-shot: wait for playout so subsequent Script timing stays honest.
                    await asyncio.sleep(duration_s)
                else:
                    # Continuous bed: arm quickly so Script/freestyle can continue under noise.
                    await asyncio.sleep(min(0.05, duration_s))
            self.writer.emit(
                "sim.script_inject",
                spec={
                    "text": text,
                    "label": label,
                    "delivery": delivery,
                    "asset": str(wav_path),
                    "mix": mix,
                    "duration_ms": int(duration_s * 1000),
                    "gain": gain,
                    "voice_gain": self._voice_gain,
                    "loop": bool(loop),
                },
                source="script",
                include_dialogue=False,
            )
            return

        # Script say lines: local TTS primary. Gemini Live realtime-text kicks
        # for milestones caused role-flip and left the session passive so
        # between-cue freestyle never answered agent questions. Keep Live free
        # for freestyle; use SAPI for verbatim Script identity.
        # Hang-up farewell still prefers Live when available (same voice as chat).
        if self._mixer is not None and not self._script_hangup_farewell:
            local_ms = await self._inject_sapi_fallback(text, label=label, gain=gain)
            if local_ms > 0:
                await self._drain_persona_speech(timeout_s=8.0)
                await asyncio.sleep(0.2)
                return

        if self._live_session is not None:
            try:
                await self._inject_gemini_text(text, label=label, delivery=delivery, gain=gain)
                return
            except Exception as gemini_err:  # noqa: BLE001
                self.writer.emit(
                    "sim.script.error",
                    spec={
                        "step_id": label,
                        "label": label,
                        "delivery": delivery,
                        "error": (
                            f"gemini_text failed ({type(gemini_err).__name__}: "
                            f"{gemini_err}); trying sapi_fallback"
                        ),
                    },
                    source="sim.script",
                    include_dialogue=False,
                )

        if self._mixer is not None:
            local_ms = await self._inject_sapi_fallback(text, label=label, gain=gain)
            if local_ms > 0:
                await self._drain_persona_speech(timeout_s=8.0)
                await asyncio.sleep(0.2)
                return

        raise RuntimeError(
            "gemini_text inject failed: local TTS unavailable and Gemini Live failed"
        )

    async def _inject_gemini_text(
        self,
        text: str,
        *,
        label: str,
        delivery: str,
        gain: float,
    ) -> None:
        """Speak a Script line via Gemini Live (same voice as freestyle caller)."""
        if self._live_session is None:
            raise RuntimeError("Gemini live session not ready for inject")
        self._inject_playback_gain = max(0.0, min(1.0, float(gain) * self._voice_gain))
        self._inject_turn_active = True
        self._inject_heard_text = ""
        self._agent_audio_paused = True
        # Fired when the model's turn_complete lands for this injected turn —
        # the only safe point to resume agent audio (earlier collides → 1007).
        self._inject_playout_done = asyncio.Event()
        speak_directive = script_speak_directive(
            text, hangup_farewell=bool(self._script_hangup_farewell)
        )
        try:
            # Close any open agent-audio activity first — sending activity_start
            # while the agent stream is still open collides on the Live session
            # and the server closes the socket with 1007 (invalid payload).
            if self._agent_stream_open:
                await self._flush_agent_audio_stream(
                    self._live_session, reason="inject_before_text"
                )
            # Brief settle so Live is not mid-agent-audio when the cue arrives.
            # Manual VAD: wrap the cue text in activity_start/end so Live generates TTS.
            await asyncio.sleep(0.15)
            await self._live_session.send_realtime_input(
                activity_start=types.ActivityStart()
            )
            await self._live_session.send_realtime_input(text=speak_directive)
            await self._live_session.send_realtime_input(
                activity_end=types.ActivityEnd()
            )
            self.writer.emit(
                "sim.script_inject",
                spec={
                    "text": text,
                    "label": label,
                    "delivery": delivery,
                    "gain": gain,
                    "voice_gain": self._voice_gain,
                    "effective_gain": self._inject_playback_gain,
                    "attempt": 1,
                },
                source="script",
                include_dialogue=False,
            )
            deadline = time.monotonic() + 2.8
            saw_ms = 0
            mismatch = False
            while time.monotonic() < deadline:
                if self.end_call.is_set():
                    break
                if self._mixer is not None:
                    saw_ms = int(self._mixer.speech_queued_ms() or 0)
                    if saw_ms > 0:
                        break
                heard = " ".join(self._inject_heard_text.split())
                if len(heard.split()) >= 5 and not _inject_matches_say(heard, text):
                    mismatch = True
                    break
                await asyncio.sleep(0.05)
            if mismatch:
                if self._mixer is not None:
                    self._mixer.clear_speech()
                self.writer.emit(
                    "sim.script.error",
                    spec={
                        "step_id": label,
                        "label": label,
                        "delivery": delivery,
                        "error": "gemini_text inject role/say mismatch; aborting for sapi_fallback",
                        "heard": self._inject_heard_text[:240],
                        "expected": text[:240],
                    },
                    source="sim.script",
                    include_dialogue=False,
                )
                raise RuntimeError(
                    "gemini_text inject spoke off-script (likely role-flip)"
                )
            if saw_ms <= 0:
                if self._script_hangup_farewell:
                    # Farewell is best-effort — hang_up fires regardless. Don't
                    # fail the whole script for a goodbye that never played.
                    return
                raise RuntimeError(
                    "gemini_text inject produced no mic audio (model stayed silent)"
                )
            # Drain while watching STT — abort early if Live role-flips mid-utterance.
            drain_deadline = time.monotonic() + 8.0
            while time.monotonic() < drain_deadline:
                if self.end_call.is_set():
                    break
                heard_mid = " ".join(self._inject_heard_text.split())
                if len(heard_mid.split()) >= 5 and not _inject_matches_say(heard_mid, text):
                    if self._mixer is not None:
                        self._mixer.clear_speech()
                    self.writer.emit(
                        "sim.script.error",
                        spec={
                            "step_id": label,
                            "label": label,
                            "delivery": delivery,
                            "error": "gemini_text inject mid-utterance off-script; sapi_fallback",
                            "heard": heard_mid[:240],
                            "expected": text[:240],
                        },
                        source="sim.script",
                        include_dialogue=False,
                    )
                    raise RuntimeError(
                        "gemini_text inject mid-utterance off-script (likely role-flip)"
                    )
                if self._mixer is not None and self._mixer.speech_queued_ms() <= 0:
                    # Wait for trailing output transcription (often lags audio).
                    stt_deadline = time.monotonic() + 1.6
                    while time.monotonic() < stt_deadline:
                        heard_wait = " ".join(self._inject_heard_text.split())
                        if heard_wait:
                            break
                        await asyncio.sleep(0.05)
                    break
                await asyncio.sleep(0.05)
            else:
                await self._drain_persona_speech(timeout_s=0.5)
            await asyncio.sleep(0.15)
            heard_final = " ".join(self._inject_heard_text.split())
            if not heard_final:
                if self._script_hangup_farewell:
                    # Farewell: audio already played (saw_ms > 0); STT lag is
                    # expected at teardown — do not fail the goodbye.
                    return
                if self._mixer is not None:
                    self._mixer.clear_speech()
                self.writer.emit(
                    "sim.script.error",
                    spec={
                        "step_id": label,
                        "label": label,
                        "delivery": delivery,
                        "error": "gemini_text inject STT missing after audio; sapi_fallback",
                        "expected": text[:240],
                    },
                    source="sim.script",
                    include_dialogue=False,
                )
                raise RuntimeError(
                    "gemini_text inject STT missing (cannot verify caller identity)"
                )
            if not _inject_matches_say(heard_final, text):
                if self._mixer is not None:
                    self._mixer.clear_speech()
                self.writer.emit(
                    "sim.script.error",
                    spec={
                        "step_id": label,
                        "label": label,
                        "delivery": delivery,
                        "error": "gemini_text inject final STT off-script; sapi_fallback",
                        "heard": heard_final[:240],
                        "expected": text[:240],
                    },
                    source="sim.script",
                    include_dialogue=False,
                )
                raise RuntimeError(
                    "gemini_text inject final STT off-script (likely role-flip)"
                )
        finally:
            # Resume agent audio only after the model's turn_complete for the
            # injected text lands — resuming earlier collides with the still-
            # open Live activity and closes the socket (1007).
            try:
                await asyncio.wait_for(
                    self._inject_playout_done.wait(), timeout=8.0
                )
            except asyncio.TimeoutError:
                pass
            self._agent_audio_paused = False
            self._inject_turn_active = False
            self._inject_playback_gain = 1.0
            self._inject_heard_text = ""
    async def _inject_sapi_fallback(
        self, text: str, *, label: str, gain: float
    ) -> int:
        """Play local TTS into the sim mic for Script says. Returns queued ms."""
        if self._mixer is None:
            return 0
        from ..audio.sapi_tts import TARGET_RATE, synthesize_pcm16_mono

        pcm = await asyncio.to_thread(synthesize_pcm16_mono, text, rate=TARGET_RATE)
        if not pcm:
            return 0
        duration_s = max(0.05, len(pcm) / 2 / TARGET_RATE)
        self.suppress_persona_output(int(duration_s * 1000) + 400)
        speech_gain = max(0.0, min(1.0, float(gain) * self._voice_gain))
        self._emit_user_audio_source_start(gain=speech_gain, via="sapi_fallback")
        self._mixer.push_speech(pcm, gain=speech_gain)
        # Complete local TTS buffer — drain without mid-turn underrun hold.
        self._mixer.end_speech_turn()
        self.writer.emit(
            "sim.script_inject",
            spec={
                "text": text,
                "label": label,
                "delivery": "sapi",
                "gain": gain,
                "voice_gain": self._voice_gain,
                "effective_gain": speech_gain,
                "duration_ms": int(duration_s * 1000),
            },
            source="script",
            include_dialogue=False,
        )
        await asyncio.sleep(duration_s)
        return int(duration_s * 1000)

    # -------------------------------------------------------- agent -> gemini

    async def _pump_agent_audio(self, session: genai.live.AsyncSession) -> None:
        """Forward the agent's audio track (resampled to 16k) into Gemini.

        Recording of agent audio prefers Observer on agent-room (see run_orchestrator).
        We still push_agent here as a fallback for single-room WebRTC when Observer
        and bridge share the same track path (duplicate pushes are fine — wall-clock
        recorder pads gaps; overlapping audio is rare because only one pump runs).

        Auto VAD is disabled. We speech-gate frames and bookend agent speech with
        ``activity_start`` / ``activity_end`` so Live commits the turn and generates
        caller freestyle (see Live API capabilities — custom VAD).
        """
        while True:
            track = await self._agent_track_queue.get()
            self.writer.emit(
                "sim.agent_audio_bridged",
                spec={"track_sid": track.sid},
                source="sim",
                include_dialogue=False,
            )
            stream = rtc.AudioStream(track, sample_rate=GEMINI_IN_RATE, num_channels=1)
            try:
                async for frame_event in stream:
                    frame = frame_event.frame
                    pcm = bytes(frame.data)
                    # R-channel: Observer records from agent-room when attached with
                    # recorder. Fallback here only if Observer is not recording any track
                    # (single-room WebRTC still works if observer record fails to start).
                    obs_recording = bool(
                        getattr(self.observer, "_recording_track_sids", None)
                    )
                    if self.recorder is not None and not obs_recording:
                        self.recorder.push_agent(pcm, GEMINI_IN_RATE)
                    if self._agent_audio_paused:
                        # Do NOT flush activity_end while an inject owns the
                        # Live activity — closing it mid-inject collides with
                        # the inject's own activity_start/end and the server
                        # closes the socket with 1007. Just drop the frames;
                        # the inject coroutine resumes + reflushes afterwards.
                        if self._agent_stream_open and not self._inject_turn_active:
                            await self._flush_agent_audio_stream(
                                session, reason="agent_audio_paused"
                            )
                        continue
                    samples = max(1, len(pcm) // 2)
                    frame_ms = 1000.0 * samples / float(GEMINI_IN_RATE)
                    rms = pcm16_mono_rms(pcm)
                    obs_speaking = bool(
                        getattr(self.observer, "agent_is_active_speaker", False)
                    )
                    energy_speaking = rms >= _AGENT_SPEECH_RMS_THRESHOLD
                    speaking = obs_speaking or energy_speaking
                    if speaking:
                        self._agent_speech_frames += 1
                        self._agent_silence_ms = 0.0
                        if self._agent_speech_frames >= _AGENT_SPEECH_START_FRAMES:
                            if not self._agent_stream_open:
                                await session.send_realtime_input(
                                    activity_start=types.ActivityStart()
                                )
                                self._agent_stream_open = True
                                self.writer.emit(
                                    "sim.gemini_activity",
                                    spec={
                                        "edge": "activity_start",
                                        "reason": "agent_speech",
                                    },
                                    source="sim",
                                    include_dialogue=False,
                                )
                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=pcm,
                                    mime_type=f"audio/pcm;rate={GEMINI_IN_RATE}",
                                )
                            )
                    elif self._agent_stream_open:
                        self._agent_silence_ms += frame_ms
                        # Brief trailing pad only — then activity_end.
                        if self._agent_silence_ms <= _AGENT_TRAILING_PAD_MS:
                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=pcm,
                                    mime_type=f"audio/pcm;rate={GEMINI_IN_RATE}",
                                )
                            )
                        if self._agent_silence_ms >= _AGENT_STREAM_END_SILENCE_MS:
                            await self._flush_agent_audio_stream(
                                session, reason="agent_silence"
                            )
                    else:
                        self._agent_speech_frames = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.writer.emit(
                    "sim.error",
                    spec={"where": "agent->gemini", "error": f"{type(e).__name__}: {e}"},
                    source="sim",
                    include_dialogue=False,
                )
            finally:
                await stream.aclose()
                await self._flush_agent_audio_stream(session, reason="agent_track_ended")

    # -------------------------------------------------------- gemini -> livekit

    async def _pump_gemini_events(
        self, session: genai.live.AsyncSession, source: rtc.AudioSource
    ) -> None:
        """Play Gemini audio into the room; log transcriptions and interruptions."""
        try:
            while not self.end_call.is_set():
                async for response in session.receive():
                    # Gemini session resumption: the server periodically sends a
                    # resumable handle so the client can reconnect (a fresh
                    # WebSocket) and keep the conversation context past the ~10-min
                    # connection cap. Save it; run() uses it on go_away reconnect.
                    if response.session_resumption_update is not None:
                        upd = response.session_resumption_update
                        if upd.resumable and upd.new_handle:
                            self._resume_handle = upd.new_handle
                            self.writer.emit(
                                "sim.gemini_resumption_handle",
                                spec={"resumable": True, "handle_set": True},
                                source="sim",
                                include_dialogue=False,
                            )

                    # Server will close the connection soon (connection cap). Signal
                    # run() to resume the session on a fresh connection. Graceful —
                    # do NOT set end_call or transport_dropped.
                    if response.go_away is not None:
                        self.writer.emit(
                            "sim.gemini_go_away",
                            spec={"time_left": response.go_away.time_left},
                            source="sim",
                            include_dialogue=False,
                        )
                        self._reconnect_required.set()
                        return

                    sc = response.server_content
                    if sc is None:
                        continue

                    if sc.interrupted:
                        self.writer.emit(
                            "interruption",
                            spec={"by": "agent", "note": "Gemini output interrupted by agent audio"},
                            source="sim",
                        )

                    # Caller-side transcriptions: what the sim heard itself say (output)
                    # and what it heard from the agent (input).
                    if sc.output_transcription and sc.output_transcription.text:
                        if self._allow_persona_room_audio():
                            chunk = sc.output_transcription.text
                            if self._inject_turn_active:
                                self._inject_heard_text += chunk
                            self._sim_out_text += chunk
                            # Freestyle role-flip: Live continues as the assistant after
                            # hearing them. Cut mic ASAP — midcall text kicks make it worse.
                            if (
                                not self._inject_turn_active
                                and not self._script_hangup_farewell
                                and looks_like_assistant_persona(self._sim_out_text)
                            ):
                                if self._mixer is not None:
                                    self._mixer.clear_speech()
                                self.suppress_persona_output(4000)
                                self.writer.emit(
                                    "sim.caller_role_flip_suppressed",
                                    spec={
                                        "heard": self._sim_out_text.strip()[:240],
                                        "note": "freestyle matched assistant-persona cues",
                                    },
                                    source="sim",
                                    include_dialogue=False,
                                )
                                self._sim_out_text = ""
                                continue
                            pending = self._script_steps_pending()
                            early_bye = contains_farewell_signal(self._sim_out_text)
                            scripted_farewell = self._script_hangup_farewell
                            if (
                                (early_bye or contains_end_call_signal(self._sim_out_text))
                                and not scripted_farewell
                            ):
                                # Mute ASAP so freestyle bye does not push more PCM to the agent.
                                self._mute_hang_up_audio()
                                if pending and early_bye:
                                    self.suppress_persona_output(4000)
                            log_text = (
                                strip_farewell_signal(self._sim_out_text)
                                if pending
                                else strip_end_call_signal(self._sim_out_text)
                            )
                            self.observer.on_transcript(
                                "user",
                                log_text,
                                final=False,
                                source="sim.gemini",
                            )
                    if sc.input_transcription and sc.input_transcription.text:
                        # Agent speech as heard by the sim. lk.transcription is the primary
                        # agent transcript source; keep this as a low-priority mirror.
                        self.writer.emit(
                            "sim.heard_agent",
                            spec={"text": sc.input_transcription.text},
                            source="sim.gemini",
                        )

                    if sc.model_turn:
                        for part in sc.model_turn.parts or []:
                            blob = part.inline_data
                            if blob and blob.data and self._allow_persona_room_audio():
                                await self._play_pcm(blob.data)

                    if sc.turn_complete:
                        if self._mixer is not None:
                            # Allow silence pad / drain — stop mid-utterance underrun hold.
                            self._mixer.end_speech_turn()
                        inject_turn = self._inject_turn_active
                        # inject_cue owns clearing _inject_turn_active after drain.
                        if inject_turn:
                            # The injected text turn fully played out — safe to
                            # let the inject coroutine resume agent audio.
                            self._inject_playout_done.set()
                        if not inject_turn:
                            self._inject_playback_gain = 1.0
                        # TTL suppress / scripted silence only — do not drop freestyle
                        # answers while Script steps remain (caller may reply to questions).
                        if (
                            not inject_turn
                            and not self._script_hangup_farewell
                            and self._persona_output_suppressed()
                        ):
                            self._sim_out_text = ""
                            self._mute_persona_audio = False
                            continue
                        text = self._sim_out_text.strip()
                        if text:
                            ended = contains_end_call_signal(text)
                            farewell = contains_farewell_signal(text)
                            pending = self._script_steps_pending()
                            clean = (
                                strip_farewell_signal(text)
                                if pending
                                else strip_end_call_signal(text)
                            )
                            if clean:
                                self.observer.on_transcript(
                                    "user", clean, final=True, source="sim.gemini"
                                )
                            self._sim_out_text = ""
                            # A freestyle utterance committed — arm the caller-audio
                            # onset latch so the next utterance emits again.
                            self._reset_user_audio_source_latch()
                            if (
                                pending
                                and (ended or farewell)
                                and not self._script_hangup_farewell
                            ):
                                # Script still owns hang-up — do not tear down the session.
                                self._mute_persona_audio = True
                                self.suppress_persona_output(5000)
                                self.writer.emit(
                                    "sim.script_deferred_end_call",
                                    spec={"text": clean, "reason": "script_steps_pending"},
                                    source="sim.gemini",
                                )
                                self._mute_persona_audio = False
                                continue
                            if should_end_call_on_turn(
                                pending_script=pending,
                                ended=ended,
                                farewell=farewell,
                                scripted_farewell=self._script_hangup_farewell,
                            ):
                                # Dialogue: soft bye or [END_CALL] — one goodbye ends the call.
                                self._mute_persona_audio = True
                                await self._drain_persona_speech(timeout_s=3.0)
                                self.writer.emit(
                                    "sim.end_call_token",
                                    spec={
                                        "text": clean,
                                        "reason": "end_call_token" if ended else "farewell",
                                    },
                                    source="sim.gemini",
                                )
                                self.end_call.set()
                                return
                            self._mute_persona_audio = False
                        else:
                            self._mute_persona_audio = False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            is_transport = (
                isinstance(e, ConnectionError)
                or "1006" in str(e)
                or "1008" in str(e)  # known Gemini preview-model transient (tool-call crash)
                or "1011" in str(e)  # server internal error — transient; resumable w/ handle
                or "abnormal closure" in str(e).lower()
                or "ConnectionClosed" in type(e).__name__
            )
            if is_transport:
                # transport_dropped only when we are NOT going to resume (no handle
                # or reconnect already queued); a resumable drop is retryable.
                self.transport_dropped = (
                    self._resume_handle is None or self._reconnect_required.is_set()
                )
                self.writer.emit(
                    "sim.gemini_socket_drop",
                    spec={
                        "phase": "mid_call",
                        "error": f"{type(e).__name__}: {e}",
                        "retryable": self._resume_handle is not None
                        and not self._reconnect_required.is_set(),
                    },
                    source="sim",
                    include_dialogue=False,
                )
            self.writer.emit(
                "sim.error",
                spec={"where": "gemini->lk", "error": f"{type(e).__name__}: {e}"},
                source="sim",
                include_dialogue=False,
            )
            # Mid-call transport drop (1006 / 1011 / ConnectionClosed) — Gemini
            # closed the socket for a transient reason. If a resumption handle is
            # available, signal run() to reconnect and resume the session instead
            # of killing the call. Once the socket has died, `send_realtime_input`
            # is no longer safe to call, so run() must *not* emit per-connection
            # cues on the resume — the persona's context survives via the handle.
            if (
                is_transport
                and self._resume_handle is not None
                and not self._reconnect_required.is_set()
            ):
                self.transport_dropped = False
                self._reconnect_required.set()
                return
            self.end_call.set()

    def _mute_hang_up_audio(self) -> None:
        """Stop queueing further hang-up chatter; keep goodbye already buffered."""
        self._mute_persona_audio = True

    async def _drain_persona_speech(self, *, timeout_s: float = 3.0) -> None:
        if self._mixer is not None:
            await self._mixer.wait_speech_drain(timeout_s=timeout_s)
            return
        # Fallback path has no queue — small settle for in-flight capture_frame.
        await asyncio.sleep(min(0.35, timeout_s))

    async def _play_pcm(self, pcm: bytes) -> None:
        """Queue Gemini TTS onto the parallel mixer (mixes with active noise layers)."""
        if not pcm:
            return
        # Script inject / hang-up farewell must still reach the mic even if freestyle
        # hang-up mute was latching from a prior deferred goodbye.
        # Script inject / hang-up farewell must still reach the mic even if freestyle
        # hang-up mute was latching from a prior deferred goodbye.
        if (
            self._mute_persona_audio
            and not self._inject_turn_active
            and not self._script_hangup_farewell
        ):
            return
        # Inject path already baked voice_gain into _inject_playback_gain.
        # Freestyle applies quiet-caller voice_gain only.
        if self._inject_turn_active:
            gain = self._inject_playback_gain
        else:
            gain = self._voice_gain
        if self._mixer is not None:
            self._emit_user_audio_source_start(gain=gain, via="freestyle_tts")
            self._mixer.push_speech(pcm, gain=gain)
            return
        # Fallback if mixer not started (should not happen after publish_mic).
        source = self._source
        if source is None:
            return
        samples = len(pcm) // 2
        if samples == 0:
            return
        if self.recorder is not None:
            self.recorder.push_sim(pcm, GEMINI_OUT_RATE)
        frame = rtc.AudioFrame(
            data=pcm,
            sample_rate=GEMINI_OUT_RATE,
            num_channels=1,
            samples_per_channel=samples,
        )
        await source.capture_frame(frame)

    def _emit_user_audio_source_start(self, *, gain: float, via: str) -> None:
        """Emit sim.caller.audio_source_start once per caller utterance.

        Fired at the first push_speech of an utterance (the simulated caller's
        speech onset at the source — NOT the perceived onset, which is measured
        on the agent R channel). ``via`` records which path produced it.
        """
        # Defensive: tests may construct the bridge without __init__.
        writer = getattr(self, "writer", None)
        if writer is None:
            return
        if getattr(self, "_user_audio_source_emitted", False):
            return
        self._user_audio_source_emitted = True
        writer.emit(
            "sim.caller.audio_source_start",
            spec={"provider": "gemini", "voice_gain": self._voice_gain, "gain": float(gain), "via": via},
            source="sim.gemini",
            include_dialogue=False,
        )

    def _reset_user_audio_source_latch(self) -> None:
        """Arm the latch for the next caller utterance."""
        self._user_audio_source_emitted = False
