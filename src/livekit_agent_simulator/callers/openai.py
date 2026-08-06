"""OpenAI Realtime simulated caller bridged into the LiveKit room.

Wire rules (verified against the OpenAI Realtime WebSocket GA interface):
    - Endpoint: ``wss://api.openai.com/v1/realtime?model=<model>``.
    - Auth: ``Authorization: Bearer <simulator.api_key>`` (no beta header on GA).
    - ``session.update``: ``output_modalities=["audio"]``, input/output audio
      ``audio/pcm`` @ 24000 Hz, ``semantic_vad`` turn detection, caller input
      transcription via ``gpt-4o-mini-transcribe``, voice from config.
    - Input audio: base64 PCM16 mono @24000 Hz via ``input_audio_buffer.append``.
      Server VAD chunks it into "user" turns and auto-creates responses.
    - Output audio: ``response.output_audio.delta`` base64 PCM16 mono @24000 Hz.
      The server does NOT know what we played — on barge (``speech_started``)
      we must clear the mixer ourselves and (best-effort) ``conversation.item.truncate``.
    - Caller transcript: ``response.audio_transcript.delta/.done`` (what the model
      says as the caller). Agent transcript: ``conversation.item.input_audio_transcription.*``.

LiveKit side: agent audio in resampled to 24 kHz; sim audio out via the shared
``ParallelMicMixer`` at 24 kHz (same as Gemini's output rate).

The bridge satisfies the ``CallerBridge`` protocol (``callers/base.py``) and is
selected by ``simulator.provider: openai``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from livekit import rtc

from ..audio.local_recorder import LocalConversationRecorder
from ..audio.mic_mixer import ParallelMicMixer
from ..audio.pcm_cue import load_wav_pcm, resolve_cue_asset
from ..config import SimConfig
from .end_call import (
    END_CALL_TOKEN,
    contains_end_call_signal,
    contains_farewell_signal,
    should_end_call_on_turn,
    strip_end_call_signal,
    strip_farewell_signal,
)
from .gemini import (
    _is_voice_cue_asset,
    pcm16_mono_rms,
    script_speak_directive,
    resolve_voice_gain,
)

if TYPE_CHECKING:
    from ..livekit.observer import Observer
    from ..logging.event_writer import EventWriter

OPENAI_IN_RATE = 24_000
OPENAI_OUT_RATE = 24_000

# GA Realtime voices (OpenAI docs). Voice cannot change after first audio response.
OPENAI_VOICES = frozenset(
    {"alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar"}
)
_OPENAI_DEFAULT_VOICE = "marin"

# Agent→model speech gate mirrors Gemini's manual VAD. OpenAI's server VAD
# chunks the agent audio we append, but we still hold a small silence budget so
# continuous agent re-prompts do not instantly trigger a caller reply. The
# plan keeps server VAD authoritative for turn creation.
_AGENT_SPEECH_RMS_THRESHOLD = 100.0
_AGENT_TRAILING_PAD_MS = 120.0
_AGENT_STREAM_END_SILENCE_MS = 650

# Best-effort `conversation.item.truncate` bookkeeping — how far into the last
# assistant item we played before barge, in milliseconds.
_TRUNCATE_GRACE_MS = 200


def _openai_voice_name(voice: str | None) -> str:
    v = str(voice or "").strip().lower()
    if not v or v not in OPENAI_VOICES:
        # Fail fast at connect with a clear, actionable error (plan risk row).
        raise ValueError(
            f"voice '{voice}' is not a valid OpenAI Realtime voice. "
            f"Use one of: {', '.join(sorted(OPENAI_VOICES))}"
        )
    return v


class OpenAICallerBridge:
    """Owns the OpenAI Realtime WebSocket + the LiveKit audio tracks of the sim caller.

    Mirrors ``GeminiCallerBridge`` (same ``CallerBridge`` contract) so Script /
    nudge / interrupt policy / orchestrator work unchanged.
    """

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
    ) -> None:
        self.cfg = cfg
        self.room = room
        self.observer = observer
        self.writer = writer
        self.persona_system_prompt = persona_system_prompt
        self.first_speaker = first_speaker
        self.recorder = recorder
        if not 0.0 <= float(voice_gain) <= 1.0:
            raise ValueError(f"voice_gain must be between 0.0 and 1.0 (got {voice_gain})")
        self._voice_gain = float(voice_gain)
        self._silent_mode = bool(silent_mode)
        self._midcall_cues = list(midcall_cues or [])

        self.end_call = asyncio.Event()
        self.transport_dropped = False
        self._agent_track_queue: asyncio.Queue[rtc.RemoteAudioTrack] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._source: rtc.AudioSource | None = None
        self._mixer: ParallelMicMixer | None = None
        self._ws: Any | None = None
        self._sim_out_text = ""
        self._sim_out_item_id: str | None = None
        # Played-audio bookkeeping for `conversation.item.truncate` on barge.
        self._last_item_start_mono: float | None = None
        self._last_item_ms_played = 0
        self._suppress_output_until_mono: float | None = None
        self._script_hold_until_mono: float | None = None
        self._script_hold_grace_s: float = 20.0
        self._inject_playback_gain: float = 1.0
        self._inject_turn_active: bool = False
        self._inject_heard_text: str = ""
        self._agent_audio_paused: bool = False
        self._mute_persona_audio = False
        self._script_pending: Callable[[], bool] | None = None
        self._script_hangup_farewell = False
        # Track whether the socket is usable for sends (reconnect guard).
        self._send_ok = False

    # ------------------------------------------------------------------ setup

    def bind_script_pending(self, is_pending: Callable[[], bool] | None) -> None:
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
        self._script_hangup_farewell = True
        self._suppress_output_until_mono = None
        self._mute_persona_audio = False

    def end_script_hangup_farewell(self) -> None:
        self._script_hangup_farewell = False

    async def drain_persona_speech(self, *, timeout_s: float = 4.0) -> None:
        await self._drain_persona_speech(timeout_s=timeout_s)

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

        for p in self.room.remote_participants.values():
            if p.identity != agent_identity:
                continue
            for pub in p.track_publications.values():
                if pub.track is not None and pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                    self._agent_track_queue.put_nowait(pub.track)

    def watch_sip_audio_tracks(self) -> None:
        """Subscribe to any remote SIP (or non-local) audio on sim_room (hairpin)."""

        def _maybe_queue(p: rtc.RemoteParticipant, track: rtc.Track) -> None:
            if track.kind != rtc.TrackKind.KIND_AUDIO:
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
        """Subscribe to agent audio on a *different* room (SIP 2-room)."""

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
                "note": "OpenAI ears on agent-room WebRTC (sim-room SIP track missing)",
            },
            source="sim",
            include_dialogue=False,
        )

    async def publish_mic(self) -> rtc.AudioSource:
        self._source = rtc.AudioSource(OPENAI_OUT_RATE, 1)
        track = rtc.LocalAudioTrack.create_audio_track("lks-mic", self._source)
        await self.room.local_participant.publish_track(
            track,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )
        if self.recorder is not None:
            self.recorder.mark_start()
        self._mixer = ParallelMicMixer(
            self._source,
            sample_rate=OPENAI_OUT_RATE,
            recorder=self.recorder,
        )
        self._mixer.start()
        self.writer.emit(
            "sim.mic_published",
            spec={"sample_rate": OPENAI_OUT_RATE, "mixer": "parallel", "provider": "openai"},
            source="sim",
            include_dialogue=False,
        )
        return self._source

    # -------------------------------------------------------------------- run

    async def run(self) -> None:
        voice = self.cfg.simulator.voice
        voice_name = _openai_voice_name(voice.voice)
        url = f"wss://api.openai.com/v1/realtime?model={voice.model}"
        headers = {"Authorization": f"Bearer {self.cfg.simulator.api_key}"}
        session_update = self._build_session_update(voice_name)

        source = await self.publish_mic()
        ws = await self._connect_ws_with_retry(url, headers)
        try:
            self._ws = ws
            self._send_ok = True
            self.writer.emit(
                "sim.openai_connected",
                spec={
                    "model": voice.model,
                    "voice": voice_name,
                    "language": voice.language,
                    "voice_gain": self._voice_gain,
                    "silent_mode": bool(getattr(self, "_silent_mode", False)),
                },
                source="sim",
                include_dialogue=False,
            )
            await self._send(ws, session_update)
            await self._emit_bootstrap_cues(ws)

            self._tasks = [
                asyncio.create_task(self._pump_agent_audio(ws), name="agent->openai"),
                asyncio.create_task(self._pump_openai_events(ws, source), name="openai->lk"),
            ]
            try:
                await self.end_call.wait()
            finally:
                self._ws = None
                self._send_ok = False
                for t in self._tasks:
                    t.cancel()
                await asyncio.gather(*self._tasks, return_exceptions=True)
                if self._mixer is not None:
                    await self._mixer.aclose()
                    self._mixer = None
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    def _build_session_update(self, voice_name: str) -> dict[str, Any]:
        """GA ``session.update`` payload (see docs/openai-realtime-caller.md).

        NOTE: ``session.audio.output.language`` is NOT a valid GA parameter — the
        server rejects it with ``Unknown parameter`` and the model never responds.
        The caller's language is conveyed via the persona system prompt
        (``persona_system_prompt`` already carries the locale) and the input
        transcription model; there is no output-language knob.
        """
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": self.persona_system_prompt,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": OPENAI_IN_RATE},
                        "transcription": {"model": "gpt-4o-mini-transcribe"},
                        "turn_detection": {
                            "type": "semantic_vad",
                            "eagerness": "medium",
                        },
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": OPENAI_OUT_RATE},
                        "voice": voice_name,
                    },
                },
            },
        }

    async def _connect_ws_with_retry(
        self, url: str, headers: dict[str, str]
    ) -> Any:
        """Open the OpenAI WebSocket, retrying transient transport drops.

        No SDK-level reconnect exists; once dialogue begins we do not reconnect
        (that would drop the persona's mid-call context). Mirrors Gemini's
        handshake-retry. Each drop emits a diagnostic event.
        """
        from websockets.asyncio.client import connect

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                ws = await connect(
                    url, additional_headers=headers, max_size=None
                )
                return ws
            except Exception as e:  # noqa: BLE001
                is_transport = _is_transport_error(e)
                self.writer.emit(
                    "sim.openai_socket_drop",
                    spec={
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "error": f"{type(e).__name__}: {e}",
                        "retryable": is_transport,
                    },
                    source="sim",
                    include_dialogue=False,
                )
                if not is_transport or attempt == max_attempts:
                    raise
                await asyncio.sleep(min(2.0 * attempt, 6.0))
        raise RuntimeError("unreachable")  # pragma: no cover

    async def _send(self, ws: Any, payload: dict[str, Any]) -> None:
        if ws is None or not self._send_ok:
            return
        await ws.send(json.dumps(payload))

    async def _emit_bootstrap_cues(self, ws: Any) -> None:
        """Emit connect-time midcall texts (``kind=bootstrap`` only).

        OpenAI needs an explicit ``response.create`` after the user item — with
        server VAD alone the model only responds after it hears audio; a text-only
        bootstrap item would never be spoken. Mirror the Gemini default: when
        ``first_speaker`` is the caller (or a bootstrap cue exists), kick the model.
        """
        for cue in self._midcall_cues:
            kind = getattr(cue, "kind", "") or ""
            if kind != "bootstrap":
                continue
            text = str(getattr(cue, "text", "") or "").strip()
            if not text:
                continue
            await self._send(ws, _user_text_item(text))
            await self._send(ws, {"type": "response.create"})
            self.writer.emit(
                "sim.caller_midcall",
                spec={"kind": kind, "label": getattr(cue, "label", None), "text": text[:240]},
                source="sim",
                include_dialogue=False,
            )

    async def inject_reground(self, *, label: str | None = None) -> None:
        if self._ws is None:
            return
        for cue in self._midcall_cues:
            if getattr(cue, "kind", "") != "reground":
                continue
            text = str(getattr(cue, "text", "") or "").strip()
            if not text:
                continue
            await self._send(self._ws, _user_text_item(text))
            await self._send(self._ws, {"type": "response.create"})
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
        """No-op — OpenAI server VAD owns freestyle after cues (no role-flip risk)."""
        return

    async def nudge_freestyle_answer(self, agent_hint: str = "") -> None:
        """Non-text activation: commit the agent audio turn + request a response.

        With server VAD the audio buffer may already be committed; sending an
        explicit ``response.create`` asks the model to reply to the last agent
        utterance without injecting any caller text (mirrors Gemini's
        activity_end-only nudge).
        """
        _ = agent_hint
        if self._ws is None or not self._send_ok:
            return
        if self._silent_mode or self._script_hangup_farewell:
            return
        if self._inject_turn_active or self._agent_audio_paused:
            return
        if self._persona_output_suppressed():
            return
        await self._send(
            self._ws,
            {
                "type": "input_audio_buffer.commit",
                "event_id": f"evt_nudge_{int(time.monotonic() * 1000)}",
            },
        )
        await self._send(self._ws, {"type": "response.create"})

    def stop(self) -> None:
        self.end_call.set()
        if self._mixer is not None:
            self._mixer.clear_noise()
            self._mixer.stop()

    def sim_hang_up(self) -> None:
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
        if duration_ms <= 0:
            return
        until = time.monotonic() + duration_ms / 1000
        prev = self._script_hold_until_mono
        self._script_hold_until_mono = until if prev is None else max(prev, until)
        self._script_hold_grace_s = max(self._script_hold_grace_s, float(grace_s))
        if mute_persona:
            self.suppress_persona_output(duration_ms)

    def scripted_silence_active(self) -> bool:
        if self._script_hold_until_mono is None:
            return False
        grace = self._script_hold_grace_s
        if time.monotonic() <= self._script_hold_until_mono + grace:
            return True
        self._script_hold_until_mono = None
        return False

    def _allow_persona_room_audio(self) -> bool:
        if self._script_hangup_farewell:
            return True
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

        ``delivery`` semantics match the Script contract: ``room_pcm`` plays a
        WAV cue into the sim mic; the text path uses ``conversation.item.create``
        (role=user) + ``response.create`` — an exact "say this" primitive, so we
        do NOT need Gemini's role-lock prose for the OpenAI path.
        """
        if getattr(self, "_silent_mode", False) and not self._script_hangup_farewell:
            self.writer.emit(
                "sim.silent_mode_skip_inject",
                spec={"label": label, "delivery": delivery, "text": (text or "")[:120]},
                source="sim",
                include_dialogue=False,
            )
            return
        if delivery == "room_pcm":
            await self._inject_room_pcm(
                text, label=label, delivery=delivery, asset=asset,
                scenario_dir=scenario_dir, gain=gain, loop=loop,
            )
            return
        # Text path: exact user-turn inject via conversation.item.create.
        if self._ws is not None and not self._script_hangup_farewell:
            heard_ok = await self._inject_openai_text(
                text, label=label, delivery=delivery, gain=gain
            )
            if heard_ok:
                return
        # Fallback: local TTS into the sim mic (same as Gemini).
        if self._mixer is not None:
            local_ms = await self._inject_sapi_fallback(text, label=label, gain=gain)
            if local_ms > 0:
                await self._drain_persona_speech(timeout_s=8.0)
                await asyncio.sleep(0.2)
                return
        raise RuntimeError(
            "openai_text inject failed: local TTS unavailable and OpenAI session failed"
        )

    async def _inject_room_pcm(
        self,
        text: str,
        *,
        label: str,
        delivery: str,
        asset: str | None,
        scenario_dir: Path | None,
        gain: float,
        loop: bool,
    ) -> None:
        if self._mixer is None or self._source is None:
            raise RuntimeError("Sim mic/mixer not ready — cannot play room_pcm cue")
        if not asset:
            raise ValueError("room_pcm cue requires asset")
        wav_path = resolve_cue_asset(
            asset,
            scenario_dir=scenario_dir,
            project_root=self.cfg.project_root,
            cues_config=getattr(self.cfg, "cues", None),
        )
        pcm, rate, channels = load_wav_pcm(wav_path)
        if channels != 1:
            raise ValueError("Only mono room_pcm assets are supported")
        if rate != OPENAI_OUT_RATE:
            raise ValueError(
                f"room_pcm asset rate {rate} != sim mic {OPENAI_OUT_RATE} "
                f"(resample cue WAV): {wav_path}"
            )
        duration_s = max(0.05, len(pcm) / 2 / rate)
        vocal = _is_voice_cue_asset(asset)
        if vocal:
            if loop:
                raise ValueError("loop is not supported for voice.* speech assets")
            self.suppress_persona_output(int(duration_s * 1000) + 400)
            speech_gain = max(0.0, min(1.0, float(gain) * self._voice_gain))
            self._mixer.push_speech(pcm, gain=speech_gain)
            self._mixer.end_speech_turn()
            mix = "speech"
            await asyncio.sleep(duration_s)
        else:
            self._mixer.push_noise(pcm, gain=gain, loop=loop)
            mix = "parallel_loop" if loop else "parallel"
            if not loop:
                await asyncio.sleep(duration_s)
            else:
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

    async def _inject_openai_text(
        self, text: str, *, label: str, delivery: str, gain: float
    ) -> bool:
        """Speak a Script line via OpenAI conversation.item.create + response.create.

        Returns True when the model produced audio we could verify; False when
        it stayed silent (caller falls back to local TTS).
        """
        if self._ws is None or not self._send_ok:
            return False
        self._inject_playback_gain = max(0.0, min(1.0, float(gain) * self._voice_gain))
        self._inject_turn_active = True
        self._inject_heard_text = ""
        self._agent_audio_paused = True
        try:
            speak = script_speak_directive(text, hangup_farewell=bool(self._script_hangup_farewell))
            await asyncio.sleep(0.15)
            await self._send(self._ws, _user_text_item(speak))
            await self._send(self._ws, {"type": "response.create"})
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
            while time.monotonic() < deadline:
                if self.end_call.is_set():
                    break
                if self._mixer is not None:
                    queued = self._mixer.speech_queued_ms()
                    if asyncio.iscoroutine(queued):
                        queued = await queued
                    if (queued or 0) > 0:
                        return True
                await asyncio.sleep(0.05)
            return False
        finally:
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
        self._mixer.push_speech(pcm, gain=speech_gain)
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

    # -------------------------------------------------------- agent -> openai

    async def _pump_agent_audio(self, ws: Any) -> None:
        """Forward the agent's audio track (resampled to 24k) into OpenAI.

        Server VAD chunks the appended audio and auto-creates responses, so this
        pump only needs to base64-append PCM (no activity-marker bookkeeping).
        A light speech gate still avoids pushing pure silence continuously.
        """
        while True:
            track = await self._agent_track_queue.get()
            self.writer.emit(
                "sim.agent_audio_bridged",
                spec={"track_sid": track.sid, "provider": "openai"},
                source="sim",
                include_dialogue=False,
            )
            stream = rtc.AudioStream(track, sample_rate=OPENAI_IN_RATE, num_channels=1)
            try:
                async for frame_event in stream:
                    frame = frame_event.frame
                    pcm = bytes(frame.data)
                    obs_recording = bool(
                        getattr(self.observer, "_recording_track_sids", None)
                    )
                    if self.recorder is not None and not obs_recording:
                        self.recorder.push_agent(pcm, OPENAI_IN_RATE)
                    if self._agent_audio_paused:
                        continue
                    samples = max(1, len(pcm) // 2)
                    frame_ms = 1000.0 * samples / float(OPENAI_IN_RATE)
                    rms = pcm16_mono_rms(pcm)
                    obs_speaking = bool(
                        getattr(self.observer, "agent_is_active_speaker", False)
                    )
                    energy_speaking = rms >= _AGENT_SPEECH_RMS_THRESHOLD
                    speaking = obs_speaking or energy_speaking
                    if not speaking and frame_ms >= _AGENT_STREAM_END_SILENCE_MS:
                        # Long silence — skip to avoid flooding the buffer.
                        continue
                    await self._send(
                        ws,
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(pcm).decode("ascii"),
                        },
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.writer.emit(
                    "sim.error",
                    spec={"where": "agent->openai", "error": f"{type(e).__name__}: {e}"},
                    source="sim",
                    include_dialogue=False,
                )
            finally:
                await stream.aclose()

    # ------------------------------------------------------- openai -> livekit

    async def _pump_openai_events(
        self, ws: Any, source: rtc.AudioSource
    ) -> None:
        """Play OpenAI audio into the room; log transcripts and interruptions."""
        try:
            while not self.end_call.is_set():
                async for raw in ws:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    self._dispatch_event(etype, event, source)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            if _is_transport_error(e):
                self.transport_dropped = True
                self.writer.emit(
                    "sim.openai_socket_drop",
                    spec={
                        "phase": "mid_call",
                        "error": f"{type(e).__name__}: {e}",
                        "retryable": False,
                    },
                    source="sim",
                    include_dialogue=False,
                )
            self.writer.emit(
                "sim.error",
                spec={"where": "openai->lk", "error": f"{type(e).__name__}: {e}"},
                source="sim",
                include_dialogue=False,
            )
            self.end_call.set()

    def _dispatch_event(
        self, etype: str, event: dict[str, Any], source: rtc.AudioSource
    ) -> None:
        if etype == "error":
            err = event.get("error") or {}
            self.writer.emit(
                "sim.error",
                spec={
                    "where": "openai_server",
                    "error": f"{type(err).__name__ if err else 'Error'}: "
                    f"{(err.get('message') or err.get('code') or err) if isinstance(err, dict) else err}",
                },
                source="sim",
                include_dialogue=False,
            )
        elif etype == "input_audio_buffer.speech_started":
            # Barge: server cancels the in-flight response; we stop local playback.
            self._on_speech_started()
        elif etype == "response.output_audio.delta":
            delta = event.get("delta")
            if delta and self._allow_persona_room_audio():
                pcm = base64.b64decode(delta)
                self._track_item_playback(event)
                asyncio.ensure_future(self._play_pcm(pcm))
        elif etype == "response.audio_transcript.delta":
            chunk = event.get("delta") or ""
            if chunk:
                self._on_output_transcript_delta(chunk)
        elif etype == "response.audio_transcript.done":
            self._on_output_transcript_done()
        elif etype == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "message" and item.get("role") == "assistant":
                self._last_item_start_mono = time.monotonic()
                self._last_item_ms_played = 0
        elif etype == "response.done":
            self._on_response_done()

    def _on_speech_started(self) -> None:
        """Agent audio started while the model was speaking — a real caller barge."""
        self.writer.emit(
            "interruption",
            spec={
                "by": "agent",
                "note": "OpenAI output interrupted by agent audio (input_audio_buffer.speech_started)",
            },
            source="sim",
        )
        if self._mixer is not None:
            self._mixer.clear_speech()
            self._mixer.end_speech_turn()
        # Best-effort truncate: remove the unplayed tail from the model's context.
        if self._last_item_start_mono is not None:
            played_ms = int((time.monotonic() - self._last_item_start_mono) * 1000)
            await_truncate = self._try_send_truncate(played_ms)
            if await_truncate is not None:
                asyncio.ensure_future(await_truncate)
        self._last_item_start_mono = None
        self._sim_out_text = ""
        self._mute_persona_audio = False

    def _try_send_truncate(self, played_ms: int) -> Any | None:
        if self._ws is None or not self._send_ok or self._sim_out_item_id is None:
            return None
        item_id = self._sim_out_item_id
        audio_end_ms = max(0, played_ms - _TRUNCATE_GRACE_MS)

        async def _truncate() -> None:
            await self._send(
                self._ws,
                {
                    "type": "conversation.item.truncate",
                    "item_id": item_id,
                    "content_index": 0,
                    "audio_end_ms": audio_end_ms,
                },
            )

        return _truncate()

    def _track_item_playback(self, event: dict[str, Any]) -> None:
        item_id = (event.get("item_id") or "").strip() or self._sim_out_item_id
        if item_id:
            self._sim_out_item_id = item_id
        self._last_item_ms_played += 1

    def _on_output_transcript_delta(self, chunk: str) -> None:
        if not self._allow_persona_room_audio():
            return
        self._sim_out_text += chunk
        if self._inject_turn_active:
            self._inject_heard_text += chunk
        pending = self._script_steps_pending()
        early_bye = contains_farewell_signal(self._sim_out_text)
        scripted_farewell = self._script_hangup_farewell
        if (early_bye or contains_end_call_signal(self._sim_out_text)) and not scripted_farewell:
            self._mute_persona_audio = True
            if pending and early_bye:
                self.suppress_persona_output(4000)
        log_text = (
            strip_farewell_signal(self._sim_out_text)
            if pending
            else strip_end_call_signal(self._sim_out_text)
        )
        if log_text:
            self.observer.on_transcript("user", log_text, final=False, source="sim.openai")

    def _on_output_transcript_done(self) -> None:
        # Final transcript for the completed output item. Commit even when the
        # text carries a farewell / [END_CALL] (the delta handler may have muted
        # the mic already — the final turn must still reach the observer).
        text = " ".join(self._sim_out_text.split()).strip()
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
                self.observer.on_transcript("user", clean, final=True, source="sim.openai")
            self._sim_out_text = ""
            if (
                pending
                and (ended or farewell)
                and not self._script_hangup_farewell
            ):
                self._mute_persona_audio = True
                self.suppress_persona_output(5000)
                self.writer.emit(
                    "sim.script_deferred_end_call",
                    spec={"text": clean, "reason": "script_steps_pending"},
                    source="sim.openai",
                )
                self._mute_persona_audio = False
                return
            if should_end_call_on_turn(
                pending_script=pending,
                ended=ended,
                farewell=farewell,
                scripted_farewell=self._script_hangup_farewell,
            ):
                self._mute_persona_audio = True
                asyncio.ensure_future(self._drain_persona_speech(timeout_s=3.0))
                self.writer.emit(
                    "sim.end_call_token",
                    spec={"text": clean, "reason": "end_call_token" if ended else "farewell"},
                    source="sim.openai",
                )
                self.end_call.set()
                return
            self._mute_persona_audio = False
        else:
            self._sim_out_text = ""
            self._mute_persona_audio = False

    def _on_response_done(self) -> None:
        if self._mixer is not None:
            self._mixer.end_speech_turn()
        if not self._inject_turn_active:
            self._inject_playback_gain = 1.0
        self._last_item_start_mono = None
        self._last_item_ms_played = 0
        self._sim_out_item_id = None
        self._sim_out_text = ""

    # ------------------------------------------------------------- audio out

    def _mute_hang_up_audio(self) -> None:
        self._mute_persona_audio = True

    async def _drain_persona_speech(self, *, timeout_s: float = 3.0) -> None:
        if self._mixer is not None:
            await self._mixer.wait_speech_drain(timeout_s=timeout_s)
            return
        await asyncio.sleep(min(0.35, timeout_s))

    async def _play_pcm(self, pcm: bytes) -> None:
        if not pcm:
            return
        if (
            self._mute_persona_audio
            and not self._inject_turn_active
            and not self._script_hangup_farewell
        ):
            return
        if self._inject_turn_active:
            gain = self._inject_playback_gain
        else:
            gain = self._voice_gain
        if self._mixer is not None:
            self._mixer.push_speech(pcm, gain=gain)
            return
        source = self._source
        if source is None:
            return
        samples = len(pcm) // 2
        if samples == 0:
            return
        if self.recorder is not None:
            self.recorder.push_sim(pcm, OPENAI_OUT_RATE)
        frame = rtc.AudioFrame(
            data=pcm,
            sample_rate=OPENAI_OUT_RATE,
            num_channels=1,
            samples_per_channel=samples,
        )
        await source.capture_frame(frame)


def _is_transport_error(e: Exception) -> bool:
    """True when a websockets exception looks like a transport drop (retryable)."""
    from websockets.exceptions import ConnectionClosed

    if isinstance(e, ConnectionClosed):
        return True
    name = type(e).__name__
    return (
        isinstance(e, ConnectionError)
        or "1006" in str(e)
        or "abnormal closure" in str(e).lower()
        or "ConnectionClosed" in name
        or "ConnectionLost" in name
        or "TimedOut" in name
    )


def _user_text_item(text: str) -> dict[str, Any]:
    """GA ``conversation.item.create`` — an exact user-turn text item."""
    return {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


__all__ = [
    "OPENAI_IN_RATE",
    "OPENAI_OUT_RATE",
    "OPENAI_VOICES",
    "OpenAICallerBridge",
    "_is_transport_error",
    "_openai_voice_name",
    "_user_text_item",
]
