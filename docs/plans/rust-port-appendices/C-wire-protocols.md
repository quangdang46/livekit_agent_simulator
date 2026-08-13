# Appendix C — Wire protocols, script runtime, and observer contracts (Rust port)

> Companion to `docs/plans/PLAN-20260813-rust-full-port.md` and `docs/rust-port-research.md`.
> Ground truth: the Python sources read in full (callers/base.py, callers/gemini.py, callers/openai.py,
> callers/end_call.py, callers/factory.py, callers/__init__.py, audio/mic_mixer.py, audio/local_recorder.py,
> script/{models,runtime,verify,summary,farewell,hang_up_gate}.py, script_parse.py,
> livekit/observer.py, livekit/agent_session_observer.py, logging/event_writer.py, audio/vad.py, config.py),
> plus two independent wire verifications (google-genai 2.11.0 SDK source + executed converters;
> gemini-live 0.1.8 crate source at /tmp/glive/gemini-live-0.1.8; OpenAI Realtime official GA docs;
> livekit 0.8.3 / livekit-data-stream 0.1.2 / livekit-api 0.6.3 Rust SDK at /tmp/rust-sdks-verify.bak, commit 1a477bc).
> Everything below is implementer-ready: the Rust port MUST reproduce these bytes, timings, and behaviors
> exactly. Any divergence is a port bug. Do not invent anything beyond what is stated here.

## 0. Rate and audio-shape cheat sheet

| Property | Gemini bridge | OpenAI bridge |
|---|---|---|
| Agent audio in (room → model) | `rtc.AudioStream(track, sample_rate=16_000, num_channels=1)` — SDK resamples 48k→16k | `rtc.AudioStream(track, sample_rate=24_000, num_channels=1)` — SDK resamples 48k→24k |
| Sim audio out (model → room) | `rtc.AudioSource(24_000, 1)` mono PCM16 LE @ 24 kHz | `rtc.AudioSource(24_000, 1)` mono PCM16 LE @ 24 kHz |
| Model input PCM | 16 kHz PCM16 LE mono, blob mime `audio/pcm;rate=16000` | 24 kHz PCM16 LE mono, base64 in `input_audio_buffer.append` |
| Model output PCM | 24 kHz PCM16 LE mono, `model_turn.parts[*].inline_data` | 24 kHz PCM16 LE mono, `response.output_audio.delta` |
| Base64 variant | urlsafe (SDK `_common.py:693`); standard also accepted | standard |
| Turn detection | OFF — manual `activity_start`/`activity_end` markers (Live API contract) | OFF — `turn_detection: null`, manual `input_audio_buffer.commit` + `response.create` |
| Track name / source | `"lks-mic"`, `TrackSource.SOURCE_MICROPHONE` (same both) | same |

Both bridges publish the mic **once** (`publish_mic()` before the session loop — must never be re-run on a
Gemini reconnect, would double-publish), create ONE `ParallelMicMixer` over ONE `AudioSource`, and call
`recorder.mark_start()` right after the track publish (pins recorder t=0).

Mixer contract (audio/mic_mixer.py, both bridges share): `ParallelMicMixer(source, *, sample_rate, recorder=None,
frame_ms=10, speech_preroll_ms=150, effects=())`; validates `sample_rate > 0`, `source.sample_rate == sample_rate`
(else ValueError), `speech_preroll_ms >= 0`. Single writer loop: every 10 ms, `_pop_frame()` mixes the speech
queue + noise tracks (saturating sum, `scale_pcm16_samples` = `int(round(s*gain))` saturated to `[-32768, 32767]`),
applies `for fx in effects: pcm = fx(pcm)` ("post-mix degradation effects — applied to the exact bytes the agent
hears; recorder captures post-effects"), then `await source.capture_frame(frame)` ALWAYS (even silence, so the
playout clock stays steady while noise plays), then `recorder.push_sim(pcm, sample_rate)` (post-effects). Speech
preroll: while a turn is active and not yet playing, emit silence until >= 150 ms buffered; mid-turn underrun
re-enters the waterline (no zero-punch); `end_speech_turn()` drains with silence pad. Key API:
`begin_speech_turn()`/`end_speech_turn()`, `push_speech(pcm, *, gain=1.0)` (auto-opens the turn on first chunk),
`clear_speech()`, `push_noise(pcm, *, gain=1.0, loop=False)`, `clear_noise()`, `noise_remaining_ms()`,
`speech_queued_ms()`, `wait_speech_drain(timeout_s=3.0)` (queue empty AND turn inactive), `wait_noise_drain`,
`stop()`, `aclose()` (2.0 s wait then cancel).

Rust mirror: `NativeAudioSource::new(AudioSourceOptions::default(), 24000, 1, queue_size_ms)` +
`LocalAudioTrack::create_audio_track("lks-mic", RtcAudioSource::Native(source))` +
`local_participant.publish_track(track, TrackPublishOptions { source: TrackSource::Microphone, ..Default::default() })`.
Note: `TrackPublishOptions` has NO `name` field (commented out in the crate) — the track name comes from
`create_audio_track`. NativeAudioSource has no `capture_stream()`/`is_push_based()` — the push model is implicit;
the port owns a producer task that calls `capture_frame(&AudioFrame{..})` on its own cadence.

---

## 1. Appendix: Gemini caller wire format

### 1.1 Connect URL

The google-genai SDK builds the endpoint from `client.aio.live.connect(model=..., config=...)` — fixed URL,
key passed as **query param, not header**:

```
wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=<api_key>
```

WebSocket client args (must be replicated — without them the library's default 20 s ping kills calls ~4 min in
with 1011 "keepalive ping timeout"): `ping_interval=30`, `ping_timeout=60`.

### 1.2 Setup message (exact JSON template)

Sent as the first frame `{"setup": {...}}`. Python's SDK serializes speechConfig inner keys **snake_case**
(verified by executing the SDK converters; the server accepts both spellings):

```json
{
  "setup": {
    "model": "<model>",
    "generationConfig": {
      "responseModalities": ["AUDIO"],
      "speechConfig": {
        "voice_config": {"prebuilt_voice_config": {"voice_name": "<voice>"}},
        "language_code": "<language>"
      }
    },
    "systemInstruction": {"parts": [{"text": "<persona_system_prompt>"}], "role": "user"},
    "tools": [],
    "realtimeInputConfig": {"automatic_activity_detection": {"disabled": true}}
  }
}
```

Non-negotiables (each is a hard-won behavioral requirement):
- `responseModalities: ["AUDIO"]` ONLY — requesting TEXT closes the socket with **1011**. Hard-code it.
- `automatic_activity_detection.disabled: true` — manual activity markers; auto VAD stays OFF because
  "continuous WebRTC silence + auto VAD never reliably committed agent turns for freestyle". Not sent by
  default by the SDK; the caller must send the disable.
- `speechConfig.language_code` — this is the **vendored-crate patch point** (research `rust-port-research.md`
  §2.3): `gemini-live 0.1.8`'s `SpeechConfig` (src/types/config.rs:56-60, `#[serde(rename_all = "camelCase")]`)
  has only `voice_config`. Patch: add
  `#[serde(skip_serializing_if = "Option::is_none")] pub language_code: Option<String>`
  on `SpeechConfig` (NOT on VoiceConfig/PrebuiltVoiceConfig — correct the research doc's line 204 if it says
  otherwise). The crate then serializes camelCase (`voiceConfig/prebuiltVoiceConfig/voiceName/languageCode`),
  which the server accepts; exact byte parity with Python is NOT required here, only field presence.
- `inputAudioTranscription {}` and `outputAudioTranscription {}`: both default-enabled — send empty objects if
  the crate requires explicit opt-in.
- `sessionResumption.handle`: `None` on first connect, the saved handle on reconnect. Auto-injected by the
  crate's `setup_for_handshake` on reconnect (session.rs:634-644).
- No tools.

### 1.3 Realtime input (agent audio → Gemini) and activity markers

Pump source: `rtc.AudioStream(track, sample_rate=16_000, num_channels=1)` (SDK resamples 48k→16k). Frames are
raw PCM16 LE mono. Activity gate (manual VAD): speaking = `observer.agent_is_active_speaker` OR
`rms >= 100.0` (`_AGENT_SPEECH_RMS_THRESHOLD = 100.0`). Constants: `_AGENT_SPEECH_START_FRAMES = 1`,
`_AGENT_TRAILING_PAD_MS = 120.0`, `_AGENT_STREAM_END_SILENCE_MS = 650` (docs: >=500 ms end-of-speech for manual
client VAD quality).

Wire messages (all via the session):
- Audio blob: `{"realtime_input": {"audio": {"data": "<base64>", "mimeType": "audio/pcm;rate=16000"}}}` — raw
  PCM16 LE mono @ 16 kHz; urlsafe base64.
- `activity_start`: `{"realtime_input": {"activityStart": {}}}` — separate call, NOT wrapped with audio.
- `activity_end`: `{"realtime_input": {"activityEnd": {}}}` — separate call.
- `audioStreamEnd`: `{"realtime_input": {"audioStreamEnd": true}}` — auto-VAD-mode only (semantic "mic
  muted/stream ended"); with manual VAD use `activity_end`, NEVER `audioStreamEnd`.

Pump state machine (must be replicated exactly):
1. Stream closed (`_agent_stream_open == False`). First speaking frame (>= 1 frame) → send `activity_start`,
   set `_agent_stream_open=True`, emit `sim.gemini_activity {edge:"activity_start", reason:"agent_speech"}`.
2. While open and speaking → send audio blobs per frame.
3. While open and silent → keep forwarding pad frames while `_agent_silence_ms <= 120.0`; then when silence
   reaches `>= 650 ms`, flush: send `activity_end`, emit `sim.gemini_activity {edge:"activity_end", reason:...}`.
   Also flush on session teardown (`reason:"session_teardown"`), on agent track end
   (`reason:"agent_track_ended"`), before inject text (`reason:"inject_before_text"` — sending activity_start
   while the agent stream is still open collides and the server closes the socket with 1007), and on
   freestyle nudge (`reason:"freestyle_nudge"`).
4. Inject in flight (`_inject_turn_active`): agent frames are dropped WITHOUT flushing activity_end — closing
   it mid-inject collides with the inject's own activity_start/end and the server closes the socket with 1007.
   But if paused and NOT inject-active, flush once (`reason:"agent_audio_paused"`).
5. Redundant/early `activity_end` failure (after the model already ended its turn / while audio is mid-flight)
   → server closes with 1007 "invalid frame payload data". The bridge catches it, emits
   `sim.gemini_activity {edge:"activity_end_skipped", reason, error}`, resets state — and MUST NOT treat it as
   fatal (documented research finding: harmless).

### 1.4 Output (Gemini → room)

Receive loop bounded by `asyncio.wait_for(session.receive().__anext__(), timeout=15.0)`; TimeoutError →
continue unless `end_call` is set (this polling lets `bridge.stop()` tear down promptly even when the old
session's socket is dead after a mid-call reconnect).

- `session_resumption_update` (`response.session_resumption_update`): "the server periodically sends a
  resumable handle so the client can reconnect (a fresh WebSocket) and keep the conversation context past the
  ~10-min connection cap". If `resumable and new_handle`: save handle, emit
  `sim.gemini_resumption_handle {resumable: True, handle_set: True}`.
- `go_away`: emit `sim.gemini_go_away {time_left}`, set reconnect-required, return. "Graceful — do NOT set
  `end_call` or `transport_dropped`."
- `interrupted`: emit `interruption` spec `{"by": "agent", "note": "Gemini output interrupted by agent audio"}`
  source="sim" — this is the barge signal; there is NO `mixer.clear_speech()` on this path (Gemini handles its
  own barge state; the mixer only gets `end_speech_turn` on turn_complete).
- `output_transcription.text` (the caller's own speech as the sim's voice, source names `sim.gemini`):
  - If `_allow_persona_room_audio()`: append to `_sim_out_text` (and `_inject_heard_text` when an inject turn).
  - Role-flip suppression (only when not inject and not script hang-up farewell): if
    `looks_like_assistant_persona(self._sim_out_text)` → `_mixer.clear_speech()`, `suppress_persona_output(4000)`,
    emit `sim.caller_role_flip_suppressed {heard, note:"freestyle matched assistant-persona cues"}`, reset
    `_sim_out_text=""`, continue.
  - Assistant-persona cue tuple (exact, lowercase match): `("thanks for calling", "thank you for calling",
    "how can i help", "how may i help", "let me check that for you", "let me check on that for you",
    "i'd be happy to help", "i would be happy to help", "we're here to help", "we are here to help",
    "i'll check that for you", "i will check that for you")`.
  - Early-bye mute: if (early_bye or contains_end_call_signal) and not scripted_farewell →
    `_mute_hang_up_audio()`; if pending_script and early_bye → `suppress_persona_output(4000)`.
  - Log: `observer.on_transcript("user", log_text, final=False, source="sim.gemini")` where log_text =
    `strip_farewell_signal` if pending else `strip_end_call_signal`.
- `input_transcription.text` (agent as heard): emit `sim.heard_agent {"text": ...}` source="sim.gemini"
  (comment: "lk.transcription is the primary agent transcript source; keep this as a low-priority mirror").
  NOT fed to on_transcript.
- `model_turn.parts[*].inline_data`: if blob data non-empty and `_allow_persona_room_audio()` → playout.
  Output PCM16 mono @ 24 kHz.
- `turn_complete`: `_mixer.end_speech_turn()` (allow silence pad / drain); if inject turn →
  `_inject_playout_done.set()`; if not inject → `_inject_playback_gain = 1.0`; if not inject and not farewell
  and `_persona_output_suppressed()` → `_sim_out_text=""`, `_mute_persona_audio=False`, continue. Final
  commit: `ended = contains_end_call_signal(text)`, `farewell = contains_farewell_signal(text)`,
  `pending = script steps pending`, `clean = strip_farewell_signal if pending else strip_end_call_signal`;
  `observer.on_transcript("user", clean, final=True, source="sim.gemini")`; `_sim_out_text=""`;
  reset the audio-source latch. Then:
  - If pending and (ended or farewell) and not script_farewell: `_mute_persona_audio=True`,
    `suppress_persona_output(5000)`, emit `sim.script_deferred_end_call {text: clean,
    reason: "script_steps_pending"}` source="sim.gemini", then `_mute_persona_audio=False`, continue (NO teardown).
  - Else if `should_end_call_on_turn(pending_script=pending, ended=ended, farewell=farewell,
    scripted_farewell=self._script_hangup_farewell)`: `_mute_persona_audio=True`,
    `await self._drain_persona_speech(timeout_s=3.0)`, emit `sim.end_call_token {text: clean,
    reason: "end_call_token" if ended else "farewell"}` source="sim.gemini", `self.end_call.set()`, return.
  - Else `_mute_persona_audio=False`.

`_play_pcm` (the 24 kHz output path): skip if `_mute_persona_audio and not _inject_turn_active and not
_script_hangup_farewell` ("Script inject / hang-up farewell must still reach the mic even if freestyle
hang-up mute was latching from a prior deferred goodbye"). Gain = `_inject_playback_gain` if inject active
else `_voice_gain`. Mixer path: `_emit_user_audio_source_start(gain=gain, via="freestyle_tts")` (latched —
fires at the FIRST push_speech of an utterance only), `self._mixer.push_speech(pcm, gain=gain)`. Fallback
(no mixer): `recorder.push_sim(pcm, 24000)` then `await source.capture_frame(rtc.AudioFrame(data=pcm,
sample_rate=24000, num_channels=1, samples_per_channel=samples))`. Gemini `_play_pcm` is AWAITED INLINE
(model_turn playout is serialized per part); OpenAI's is dispatched via `ensure_future` (no ordering guarantee).

**Resampling note (answers the plan's question):** Python does NOT resample Gemini output — the sim mic
`AudioSource` is created at 24 kHz and the WebRTC stack handles playback-side resampling to the room. Model
output PCM (24 kHz) goes into the mixer at 24 kHz as-is. The only resamples in the system are on the INPUT
side (SDK `AudioStream` resamples the agent's 48 kHz room audio to 16 kHz for Gemini / 24 kHz for OpenAI).
The Rust port must mirror this: mixer + AudioSource at 24 kHz, model output injected without conversion.

### 1.5 Transport drop classification + reconnect

Classification (both bridges): transport iff `isinstance(e, ConnectionError)` OR `"1006" in str(e)` OR
`"abnormal closure" in str(e).lower()` OR type name contains `"ConnectionClosed"`. Gemini ADDS `"1008"`
(known Gemini preview-model transient — tool-call crash) and `"1011"` (server internal error — transient,
resumable with handle). OpenAI ADDS `"ConnectionLost"` or `"TimedOut"` in the type name.

Gemini mid-call drop: `self.transport_dropped = (self._resume_handle is None or self._reconnect_required.is_set())`;
emit `sim.gemini_socket_drop {phase:"mid_call", error, retryable: handle is not None and not reconnect_queued}`;
ALWAYS also emit `sim.error {where:"gemini->lk", error}`. If transport AND handle available AND no reconnect
queued: `transport_dropped=False`, set reconnect-required, return ("Once the socket has died,
`send_realtime_input` is no longer safe to call, so run() must *not* emit per-connection cues on the resume —
the persona's context survives via the handle"). Otherwise `end_call.set()`.

Reconnect loop: on go_away or resumable drop → teardown session cm, clear reconnect, `end_call.clear()`,
generation += 1, reconnect_count += 1. If `reconnect_count > 2`: "Bounded mid-call reconnect: a server that
keeps resetting the socket won't recover; stop hammering and end the call." → `end_call.set()`, break. Else
emit `sim.gemini_reconnecting {generation}` and reconnect passing the saved handle. Mixer torn down ONCE after
the whole session incl. reconnects; `publish_mic()` called ONCE before the loop.

Handshake retry (`_connect_live_with_retry`): `max_attempts=3`, retryable by the same classification
(ConnectionError | "1006" | "1008" | "abnormal closure" | ConnectionClosed type name); backoff
`min(2.0*attempt, 6.0)` → 2 s, 4 s (3rd attempt final, no sleep); emit `sim.gemini_socket_drop {attempt,
max_attempts, error, retryable}`. "once dialogue has begun we do not reconnect (that would drop the persona's
mid-call context)" — pre-dialogue only.

Crate divergence to manage (research `rust-port-research.md` §2.3, confirmed against 0.1.8 source):
`gemini-live`'s `ReconnectPolicy` (session.rs:71-87: `{enabled: bool default true, base_backoff 500ms,
max_backoff 5s, max_attempts Some(10)}`) does NOT distinguish GoAway vs ConnectionLost — an internal
`DisconnectReason` enum (session.rs:408-414) collapses both into one reconnect branch (session.rs:436-462),
and a Close frame maps to ConnectionLost (session.rs:500-506). Patch knob (recommended, ~10 lines): add
`pub reconnect_on_drop: bool` (default false) to ReconnectPolicy and branch: GoAway → always reconnect
(inject resume handle); ConnectionLost → reconnect only if `reconnect_on_drop`, else emit
`ServerEvent::Closed{reason}` + `SessionStatus::Closed` (mirroring Python: `transport_dropped=True`, no
mid-call reconnect). Also patch `RawFrame::Close` handling in src/transport.rs:311-324 (currently drops the
close code, keeps only the reason) to surface code+reason so the caller can classify per the table below.

### 1.6 Close-code handling table (Gemini)

| Code | Meaning (ground truth) | Python handling | Rust handling |
|---|---|---|---|
| 1006 | Abnormal closure / keepalive-ping timeout | Transport: retryable IF resume handle available (else `transport_dropped=True`, `end_call`) | Same; classification string set incl. "1006" |
| 1008 | Policy violation (also known Gemini preview-model transient) | Transport: same retryable logic as 1006 | Same — include "1008" in the transport classifier |
| 1011 | Server internal error (also text-only native-audio setups; also the "keepalive ping timeout" symptom) | Transport: retryable with handle | Same — include "1011" |
| 1007 | Invalid frame payload data — redundant/early `activity_end` or activity markers mid-open-stream; also activityStart in text-only gemini-live-2.5 sessions | `sim.gemini_activity {edge:"activity_end_skipped", ...}` — harmless, NOT fatal; also guarded preventively (no flush while inject active; flush before inject text) | Same — swallow, emit the skipped event, never a hard fail |
| 1000 + go_away | Graceful | Reconnect via handle; no end_call/transport_dropped | Same |

Close codes 1008/1011/1007 semantics are corroborated at community level (python-genai issue #1720,
discuss.ai.google.dev) — no official Google close-code doc exists for the Live API.

### 1.7 `script_speak_directive` (exact text — port verbatim)

Non-farewell variant (used for `gemini_text` Script say lines; do NOT add "stay silent" instructions — that
over-conditioned Live into cue-only mute between milestones; freestyle-after lives in SI):

```
PRIVATE SIMULATOR CUE — do not read these instructions aloud. You are UNMISTAKABLY the HUMAN CALLER on this phone call. Any other voice you just heard is the assistant (the other party) — never speak as them, never greet callers, never offer to help or check availability for someone else. Ignore silence rules for this one turn only. Speak aloud now, exactly once, ONLY the caller line between <<< and >>>. Verbatim: no paraphrase, no extra words before or after, no added fillers.
<<<
{line}
>>>
After that exact line, stop this cue turn.
```

Farewell variant (`hangup_farewell=True`):

```
SIMULATOR CUE — ignore silence rules for this one turn only. You are the HUMAN CALLER (not the assistant). Speak the following goodbye aloud now as the phone caller, exactly once, then stop and wait silently for disconnect:
{line}
```

The OpenAI text-inject path ALSO uses this Gemini prose (role-lock) for its say line — `_user_text_item`
creates `{"type":"conversation.item.create","item":{"type":"message","role":"user","content":
[{"type":"input_text","text": speak_directive}]}}` then `response.create` ("an exact 'say this' primitive, so
we do NOT need Gemini's role-lock prose for the OpenAI path" — the prose is still sent verbatim, see
`callers/openai.py` `inject_cue`).

### 1.8 Gemini inject details (gemini_text delivery)

`_inject_gemini_text`: `_inject_playback_gain = clamp(gain * voice_gain)`; `_inject_turn_active=True`;
`_inject_heard_text=""`; `_agent_audio_paused=True`; fresh `_inject_playout_done` Event. If `_agent_stream_open`:
flush (reason="inject_before_text"). `await asyncio.sleep(0.15)`; then
`send_realtime_input(activity_start=...)`, `send_realtime_input(text=speak_directive)`,
`send_realtime_input(activity_end=...)` ("Manual VAD: wrap the cue text in activity_start/end so Live
generates TTS"). Emit `sim.script_inject {..., attempt: 1}` source="script". Wait loop deadline **2.8 s** for
`_mixer.speech_queued_ms() > 0` or STT mismatch (`len(heard.split()) >= 5 and not _inject_matches_say(heard,
text)`); on mismatch: `_mixer.clear_speech()`, emit `sim.script.error` "gemini_text inject role/say mismatch;
aborting for sapi_fallback" `{heard[:240], expected[:240]}`, raise. If no audio (`saw_ms <= 0`): farewell →
return best-effort, else raise "gemini_text inject produced no mic audio (model stayed silent)". Drain
deadline **8.0 s** watching STT for mid-utterance role-flip (same >=5-word mismatch → clear_speech, raise);
when `speech_queued_ms() <= 0` wait up to **1.6 s** for trailing STT, break. Then 0.15 s settle; heard_final:
empty → farewell ? return : raise "gemini_text inject STT missing (cannot verify caller identity)"; mismatch →
clear_speech + raise. finally: `await asyncio.wait_for(self._inject_playout_done.wait(), timeout=8.0)`
(TimeoutError → pass), `_agent_audio_paused=False`, `_inject_turn_active=False`,
`_inject_playback_gain=1.0`, `_inject_heard_text=""` ("Resume agent audio only after the model's
turn_complete for the injected text lands — resuming earlier collides with the still-open Live activity and
closes the socket (1007)").

SAPI fallback (`_inject_sapi_fallback`, script identity primary when mixer exists and not hangup farewell):
synthesize via `synthesize_pcm16_mono(text, rate=TARGET_RATE)` in a thread; `duration_s = max(0.05,
len(pcm)/2/TARGET_RATE)`; `suppress_persona_output(int(duration_s*1000)+400)`; `speech_gain = clamp(gain *
voice_gain)`; `_emit_user_audio_source_start(gain=speech_gain, via="sapi_fallback")`; `push_speech` +
`end_speech_turn()`; emit `sim.script_inject {delivery:"sapi", effective_gain}`; sleep duration_s. Comment:
"Script say lines: local TTS primary. Gemini Live realtime-text kicks for milestones caused role-flip and
left the session passive... Keep Live free for freestyle; use SAPI for verbatim Script identity. Hang-up
farewell still prefers Live when available (same voice as chat)." Final failure →
`RuntimeError("gemini_text inject failed: local TTS unavailable and Gemini Live failed")`.

### 1.9 Bootstrap / reground / nudge / role gates

- Bootstrap cues: only `kind == "bootstrap"` cues; `await session.send_realtime_input(text=text)` — a
  realtime-input TEXT turn, NOT wrapped in activity markers; emit `sim.caller_midcall {kind, label, text[:240]}`.
  "Default policy: speak-first kick for dialogue `user` without Script; never bootstrap when Script owns the
  open line (avoids double-open)."
- `inject_reground`: no-op if no live session; only the FIRST cue with `kind=="reground"` → text turn + event.
- `release_after_milestone`: no-op — "Earlier versions sent a 'resume conversation' realtime text after each
  milestone. On Gemini Live that text is a user turn and frequently caused role-flip... or double-opens."
- `nudge_freestyle_answer`: no-op if no session; skip if silent_mode / hangup farewell / inject active / agent
  paused / persona suppressed; no-op when `not self._agent_stream_open` ("redundant ends caused Live 1006");
  else flush agent stream (`reason="freestyle_nudge"`); exceptions → `sim.error {where:"nudge_freestyle_answer", error}`.
- Suppress logic (shared both bridges): `_suppress_output_until_mono` monotonic deadline extended to max
  (`suppress_persona_output(ms)`: `<= 0` no-op; `until = max(prev, now + ms/1000)`); `_persona_output_suppressed()`
  self-clears on expiry; `begin_scripted_user_silence(duration_ms, grace_s=20.0, mute_persona=False)` extends
  `_script_hold_until_mono` (max) and `_script_hold_grace_s` (max), and with mute_persona also suppresses;
  `scripted_silence_active()` True while `now <= hold + grace`, self-clears on expiry.
- `_allow_persona_room_audio()`: True if `_script_hangup_farewell`; True if `_inject_turn_active`; False if
  `_silent_mode` ("Silent mode: freestyle is always blocked (dead-air / unresponsive caller)"); False if
  `_mute_persona_audio or _persona_output_suppressed()`; else True. "Script still owns barge/hang-up timing,
  but freestyle answers between cues are allowed (main-compatible)."

### 1.10 Voice gain

`resolve_voice_gain(persona)`: read `persona.speech_conditions.voice_gain | voice_conditions.voice_gain |
speechConditions.voice_gain | ... | volume` (specifically `sc = persona.get("speech_conditions") or
persona.get("speechConditions") or {}`; `raw = sc.get("voice_gain", sc.get("voice_volume",
sc.get("volume", 1.0)))`); must parse float; ValueError unless `0.0 <= gain <= 1.0` ("Persona.speech_conditions.voice_gain
must be a number between 0.0 and 1.0"); default 1.0. "Quiet-caller STT stress typically uses 0.25–0.45".
"Gemini Live has no native volume API — this scales PCM after the model." Applies ONLY to sim speech
(freestyle + inject): freestyle gain = `_voice_gain`; inject speech_gain = `clamp(gain * voice_gain)`. Noise
beds use step gain only (`push_noise(gain=gain)`, not voice_gain).

### 1.11 Gemini event payload catalog (source="sim" unless noted)

`sim.gemini_connected {model, voice, language, voice_gain, silent_mode, resume}`;
`sim.caller_midcall {kind, label, text[:240]}`; `sim.mic_published {sample_rate: 24000, mixer: "parallel"}`;
`sim.agent_audio_bridged {track_sid}`; `sim.agent_listen_room {agent_identity, listen: "agent_room", note}`;
`sim.gemini_activity {edge, reason}`; `sim.gemini_socket_drop {attempt|phase, max_attempts?, error, retryable}`;
`sim.gemini_resumption_handle {resumable: True, handle_set: True}`; `sim.gemini_go_away {time_left}`;
`sim.gemini_reconnecting {generation}`; `sim.caller.audio_source_start {provider: "gemini", voice_gain, gain,
via}` source="sim.gemini"; `sim.caller_role_flip_suppressed`; `sim.script_deferred_end_call` source="sim.gemini";
`sim.end_call_token` source="sim.gemini"; `sim.hang_up {source: "script", by: "sim"}`; `sim.heard_agent`
source="sim.gemini"; `sim.silent_mode_skip_inject`; `interruption {by: "agent", ...}` source="sim";
`sim.error {where, error}`.

---

## 2. Appendix: OpenAI caller wire format

### 2.1 Connect URL + auth

```
url = wss://api.openai.com/v1/realtime?model=<model>
headers = { "Authorization": "Bearer <api_key>" }   # no beta header on GA
websockets.asyncio.client.connect(url, additional_headers=headers, max_size=None)
```

`max_size=None` = no message-size limit — replicate on tokio-tungstenite (max message size unset/large).
NO reconnect loop — one socket for the whole call; mid-call transport drop sets `transport_dropped=True`
(retryable: False) and `end_call.set()` (run dies). Session resumption does not exist for OpenAI.

### 2.2 session.update (exact JSON)

```json
{
  "type": "session.update",
  "session": {
    "type": "realtime",
    "instructions": "<persona_system_prompt>",
    "output_modalities": ["audio"],
    "audio": {
      "input": {
        "format": {"type": "audio/pcm", "rate": 24000},
        "transcription": {"model": "gpt-4o-mini-transcribe"},
        "turn_detection": null
      },
      "output": {
        "format": {"type": "audio/pcm", "rate": 24000},
        "voice": "<voice_name>"
      }
    }
  }
}
```

- `turn_detection: null` — "VAD OFF (push-to-talk): we stream the agent's audio into the input buffer;
  server VAD/semantic VAD would treat that agent speech as a caller interruption, cancel the in-flight model
  response, and the caller turn would never finalize. Manual control commits + creates responses at
  well-defined turn boundaries instead."
- NEVER send an output-language parameter — "session.audio.output.language is NOT a valid GA parameter — the
  server rejects it with `Unknown parameter` and the model never responds. The caller's language is conveyed
  via the persona system prompt and the input transcription model; there is no output-language knob."
- Voice: `_openai_voice_name(voice) = str(voice or "").strip().lower() or "marin"` — "Voice cannot change
  after first audio response."

### 2.3 Client→server events

- `{"type": "input_audio_buffer.append", "audio": "<base64 PCM16 24k>"}` — agent room audio (from
  `rtc.AudioStream(track, sample_rate=24_000, num_channels=1)`, resampled 48k→24k), pushed per frame while
  speaking. No activity markers at all (server VAD chunks).
- `{"type": "input_audio_buffer.commit", "event_id": "evt_commit_<monotonic_ms>"}`
- `{"type": "response.create", "event_id": "evt_resp_<monotonic_ms>"}` (nudge path: `"evt_nudge_..."`)
- `{"type": "input_audio_buffer.clear", "event_id": "evt_clear_<monotonic_ms>"}` — after every commit:
  "VAD is OFF (push-to-talk): the input buffer is not auto-cleared. Clear it so the next agent turn's audio
  starts from an empty buffer instead of mixing with the committed turn's residual PCM."
- `{"type": "conversation.item.create", "item": {"type": "message", "role": "user", "content":
  [{"type": "input_text", "text": "<speak_directive>"}]}}` — the say-line primitive, then `response.create`.
- `{"type": "conversation.item.truncate", "item_id": "<id>", "content_index": 0,
  "audio_end_ms": max(0, played_ms - 200)}` — barge truncation; `_TRUNCATE_GRACE_MS = 200` = "how far into
  the last assistant item we played before barge, in milliseconds".
- `_send` guard: if ws None or not `_send_ok` → return; `if payload.get("type") == "response.create" and
  self._response_in_flight: return` ("the server rejects response.create while another response is in flight.
  Drop the request instead of erroring."); `_response_in_flight=True` after sending a response.create.

### 2.4 Agent-audio manual VAD pump (input side)

`speaking = obs_speaking or rms >= 100.0` → `_agent_last_speech_mono = now` (and `_agent_commit_pending=False`
if it was set — "New speech after a (possibly brief) gap — a fresh agent turn; clear the pending-commit
latch"). Else if `_agent_last_speech_mono is not None and (now - last)*1000 >= 650` →
`_agent_commit_pending=True`, `_agent_last_speech_mono=None`, spawn `_commit_and_respond()` ("This is the
VAD-off manual turn boundary; the transcription-completed path can't drive it (it needs the commit first —
the deadlock that dead-silenced)"). Skip sending when `not speaking and frame_ms >= 650` ("Long silence —
skip to avoid flooding the buffer"). Else append (base64). Paused (inject) → `continue` (no flush logic).

`_commit_and_respond` (on commit boundary AND on caller output done): skip if ws None/not send_ok; skip if
`_script_hangup_farewell or _mute_persona_audio`; skip if `_response_in_flight`; else
commit → create → clear (event ids above); `_response_in_flight=True`; exceptions swallowed.

### 2.5 Server→client events

- `error` → `sim.error {where: "openai_server", error: message or code or err}`.
- `input_audio_buffer.speech_started` → `_on_speech_started()` — "Agent audio started while the model was
  speaking — a real caller barge." Emit `interruption {by: "agent", note: "OpenAI output interrupted by
  agent audio (input_audio_buffer.speech_started)"}`; `_mixer.clear_speech(); _mixer.end_speech_turn()`;
  best-effort truncate: if `_last_item_start_mono is not None`: `played_ms = (now - start)*1000`;
  `_try_send_truncate(played_ms)` → `conversation.item.truncate` with `audio_end_ms = max(0, played_ms - 200)`;
  then reset item-start/latch state, `_mute_persona_audio=False`. (`speech_stopped` is not consumed.)
- `response.output_audio.delta` → if delta and `_allow_persona_room_audio()`: `pcm = base64.b64decode(delta)`;
  track item playback (`_sim_out_item_id`, `_last_item_ms_played += 1`); `asyncio.ensure_future(self._play_pcm(pcm))`
  — the events pump does NOT await playback; also no `end_speech_turn` per delta.
- `response.audio_transcript.delta` → append chunk to `_sim_out_text` (+ `_inject_heard_text` if inject);
  arm one-shot watchdog `_out_done_watchdog` (sleep 6.0 s; if `_sim_out_text.strip()` →
  `_on_output_done_fallback()` → `_on_output_transcript_done()`); early-bye mute (same as Gemini); log
  `observer.on_transcript("user", log_text, final=False, source="sim.openai")`.
- `response.audio_transcript.done` → `_on_output_transcript_done()`: cancel watchdog; join-strip; commit
  (ended/farewell/pending/clean exactly like Gemini's turn_complete, source="sim.openai"); if pending and
  (ended or farewell) and not farewell → `sim.script_deferred_end_call` + suppress 5000 + mute latch toggle,
  NO teardown; if `should_end_call_on_turn(...)` → mute, `ensure_future(_drain_persona_speech(timeout_s=3.0))`,
  `sim.end_call_token {text, reason}`, `end_call.set()`; else unmute + `ensure_future(_commit_and_respond())`
  ("Manual turn hand-off (VAD disabled): the caller just finished speaking. Commit any buffered agent audio +
  request the next model response so the conversation advances deterministically."). Empty text → reset only.
- `response.created` → `_response_in_flight=True`; `response.cancelled` | `response.failed` →
  `_response_in_flight=False`.
- `response.output_item.added` → if item.type=="message" and role=="assistant": `_last_item_start_mono =
  time.monotonic(); _last_item_ms_played = 0`.
- `conversation.item.input_audio_transcription.delta` → `_agent_in_text += chunk`; log interim
  `observer.on_transcript("agent", text, final=False, source="sim.openai")`.
- `conversation.item.input_audio_transcription.completed` → `_agent_in_text = transcript` if non-empty; log
  final; emit `sim.heard_agent {text}` source="sim.openai" ("Agent speech as heard by the sim... Drives
  turn-tracking / caller-policy; without it the report shows `heard=0` and the caller never appears to react
  to the agent's turns"); reset `_agent_in_text=""`; `ensure_future(_commit_and_respond())`.
- `conversation.item.input_audio_transcription.failed` → `sim.error {where:"openai_agent_transcription"}`.
- `response.done` → `_response_in_flight=False`; `_mixer.end_speech_turn()`; if not inject →
  `_inject_playback_gain=1.0`; reset item-state and `_sim_out_text=""`. "Do NOT cancel the output-done
  watchdog here: response.done fires for the *agent's* barge response too, while the caller's own `.done`
  may still be pending (STT lag). Cancelling would strand the caller turn un-finalized → dead call."

Mid-call drop: on exception, if transport (classifier §1.5 + `"ConnectionLost"`/`"TimedOut"` in type name) →
`transport_dropped=True`; emit `sim.openai_socket_drop {phase:"mid_call", error, retryable: False}`; emit
`sim.error {where:"openai->lk"}`; `end_call.set()`. Receive loop is unbounded (`async for raw in ws`) — stop
relies on task cancel after `end_call.wait()` returns.

### 2.6 OpenAI inject

Text path (say-lines): `_user_text_item(speak)` + `response.create`; verify loop 2.8 s for
`speech_queued_ms() > 0` (`queued` may be a coroutine — await if so); True on audio → success; False on
silence → sapi fallback → else `RuntimeError("openai_text inject failed: local TTS unavailable and OpenAI
session failed")`. `hangup_farewell` SKIPS the OpenAI text path and goes straight to sapi. No STT-mismatch
abort on the OpenAI path (the item is exact text; only "did any audio play" is verified). `room_pcm` inject:
identical to Gemini at 24 kHz (mono-only; rate must equal 24000 else ValueError; voice.* → speech layer +
suppress(duration+400) + gain*voice_gain; noise → parallel layer, loop beds sleep min(0.05, duration_s)).

### 2.7 OpenAI event payload catalog (source="sim" unless noted)

`sim.openai_connected {model, voice, language, voice_gain, silent_mode}`; `sim.mic_published {sample_rate:
24000, mixer: "parallel", provider: "openai"}`; `sim.openai_socket_drop {attempt|phase, max_attempts?, error,
retryable}`; `sim.agent_audio_bridged {track_sid, provider: "openai"}`; `sim.agent_listen_room` (note "OpenAI
ears on agent-room WebRTC (sim-room SIP track missing)"); `sim.script_inject` (source="script");
`sim.script.error` (source="sim.script"); `sim.caller.audio_source_start {provider: "openai", ...}`
(source="sim.openai"); `sim.script_deferred_end_call` / `sim.end_call_token` (source="sim.openai");
`sim.heard_agent` (source="sim.openai"); `interruption` (source="sim").

---

## 3. Appendix: Script runtime semantics

### 3.1 Step model (`ScriptStep`, frozen dataclass)

| Field | Type | Default | Notes |
|---|---|---|---|
| `id` | str | required | |
| `trigger` | str | required | `agent_speaking` \| `silence` \| `time` |
| `delay_ms` | int | required | |
| `say` | str | `""` | |
| `label` | str | `""` | |
| `once` | bool | `True` | |
| `min_agent_active_ms` | int | `400` | |
| `delivery` | str | `"gemini_text"` | `gemini_text` \| `room_pcm` |
| `asset` | str \| None | `None` | |
| `silence_after_cue_ms` | int | `0` | |
| `action` | str | `"speak"` | `speak` \| `wait` \| `hang_up` \| `dtmf` |
| `mute_persona` | bool \| None | `None` | `None`/`False` = pace only (caller keeps answering); `True` = mute freestyle (dead-air tests). Only meaningful for `wait` + silence_after_cue_ms |
| `digits` | str | `""` | DTMF string, only when action=dtmf; chars `0-9*#w` (`w` = 120 ms gap) |
| `loop` | bool | `False` | continuous ambient bed for room_pcm noise; only valid with delivery=room_pcm |
| `require_agent_spoke_first` | bool | `True` | silence trigger: only start counting idle after agent has spoken once |
| `require_agent_reply_this_turn` | bool | `True` | hang_up: do not fire while user spoke and agent has not answered that turn yet |
| `defer_on_open_question` | bool | `True` | hang_up: defer while last agent final still expects a caller reply; after `open_question_idle_ms` of no reply, hang_up may proceed (ghost hang) |
| `open_question_idle_ms` | int | `20000` | |
| `barge_in` | bool | `False` | |
| `with_blip` | bool | `True` | barge_in + gemini_text: play builtin `noise.blip` first |
| `gain` | float | `1.0` | linear 0.0-1.0; applies to gemini_text TTS and room_pcm |
| `interrupt_class` | str \| None | `None` | `correction` \| `backchannel` \| `noise` \| `dtmf` \| `silence` \| `escalate` |
| `overlay` | str \| None | `None` | `fixture` (PCM/barge/noise) \| `line` (forced say) \| None → auto |

Parse pipeline (script_parse.py — error message strings are load-bearing, tests + user-facing):
- id falls back `label` then `f"step-{i}"`; `say` accepts `text` alias; trigger default `agent_speaking`;
  action default `speak` (unknown action error message literally says "action must be speak|wait");
  speak with empty say → parse error; delivery default `gemini_text` (validated to the 2 values);
  room_pcm speak requires asset.
- Defaults `delay_ms=800`, `min_agent_active_ms=400`.
- **barge_in FORCES `trigger="agent_speaking"`, `action="speak"`** and changes defaults
  (`delay_ms=250`, `min_agent_active_ms=200`); accepts `interrupt` alias.
- DTMF: digits from `digits`/`digit`, charset `set("0123456789*#w")` enforced ONLY when action==dtmf
  (a speak step carrying digits is not validated); `say = "[DTMF: <digits>]"`;
  backward compat: dtmf with trigger not in (silence, time) → trigger="time".
- `with_blip` default = `barge_in and delivery != "room_pcm"`.
- `gain`/`volume` alias, both must be numbers in 0.0-1.0.
- `loop`/`repeat` (`repeat is True` → loop; `repeat` present and not in (None, 0, 1, "0", "1") → hard parse
  error "use loop=true for continuous ambient beds (repeat count is not supported)"); `loop` requires
  delivery=room_pcm and action=speak; asset normalized (strip `builtin:`/`@` prefixes), starts with `voice.`
  → "loop is for noise/ambient beds, not voice.* speech assets".
- `interrupt_class` via `normalize_interrupt_class(raw, barge_in)`: None/blank → `"correction"` if barge_in
  else None; key lowercased with `-`/space → `_`; aliases: `true_correction`/`correct`/`barge`→correction;
  `ack`/`uhhuh`/`uh_huh`→backchannel; `false_positive`/`false_interrupt`/`click`→noise; `digit`/`digits`→dtmf;
  `human`/`handoff`/`safety`→escalate; unknown → ValueError listing supported classes.
- `overlay`/`speech_role` alias; lowercased, `-`→`_`; `forced_line`/`forced`/`say` → `"line"`; only
  fixture|line accepted. `effective_overlay(step)`: explicit overlay wins; else `barge_in` OR
  delivery==room_pcm OR class in (noise, backchannel, dtmf, silence) → `"fixture"`; else action==speak with
  non-empty say → `"line"`; else `"fixture"`.
- `counts_for_recovery_barge(barge_in, interrupt_class)`: False unless barge_in; else
  `(interrupt_class or "correction") in {"correction", "escalate"}` — drives recovery asserts /
  barge_recovery_rate.

### 3.2 Runner loop (runtime.py)

`ScriptRunner` state: `_stop` event, `_fired: set[str]`, `_firing: set[str]`, `_trigger_since /
_trigger_gap_since: dict[str, float]`, `_active_speaker_gap_tolerance_ms = 1200`,
`_armed_step_index = 0`, `_await_post_cue_gap`, `_post_cue_gap_since`, `_await_agent_reply_since`,
`_post_speak_settle_ms = 900`, `_post_speak_reply_budget_s = 45.0`, `_hang_up_defer_emitted: set[str]`,
`_hang_up_defer_since: dict[str, float]`, `_last_freestyle_nudge_mono`.

- Loop tick every **50 ms**. ONLY the step at `_armed_step_index` is evaluated; skip if
  `once and id in _fired` or `id in _firing`.
- `has_pending_steps()` = `not _stop.is_set() and _armed_step_index < len(steps)`.
- Empty steps → `run()` returns immediately.
- Wait-states: agent-reply wait (after a spoken milestone): aged >= 45.0 s → clear, arm proceeds; else
  `_maybe_nudge_freestyle(min_quiet_ms=1600)`; requires a NEW agent final after the wait start, then 900 ms
  settle of agent not active (`_post_speak_settle_ms`). Post-cue gap (agent_speaking-trigger non-speak
  steps): 1200 ms of agent-quiet after the cue.
- `_trigger_active`: agent_speaking → `observer.agent_is_active_speaker`; silence → False if
  `require_agent_spoke_first and not agent_has_spoken`, else `not agent_is_active_speaker`; time → True.
- Trigger debounce: >= 1200 ms of inactive resets `_trigger_since`/`_trigger_gap_since` (brief flicker does
  not reset).
- Delay: `elapsed = now - _trigger_since[id]` (wall-clock since trigger became continuously active);
  **if trigger == "agent_speaking": `need = min_agent_active_ms + delay_ms`**; hang_up steps that ever
  deferred: `elapsed_ms = max(elapsed_ms, need)` — fires the instant the gate opens (delay already served).
- On elapsed for hang_up: `bridge.suppress_persona_output(1500)` (wrap-up: mute freestyle so Gemini cannot
  invent loops while Script bye awaits), then `_hang_up_ready(step)`; if not ready → continue WITHOUT
  popping the trigger timestamp (popping would reset elapsed and the step could never fire again).
- `_fire` finally block: `_firing.discard`; `once` → `_fired.add`; **if `inject_error and action == "speak"`:
  `_armed_step_index = len(steps)` (abort chain) AND `bridge.sim_hang_up()`**; else `_armed_step_index += 1`;
  if more steps remain and action==speak and not barge_in → `_await_agent_reply_since = now` (freestyle
  window: agent answers the milestone first); else `_await_post_cue_gap = (trigger == "agent_speaking")`.
  Then clear trigger timers.
- `silence_after_cue_ms`: computed at fire time; drives `begin_scripted_user_silence` + paced hold ONLY for
  action=wait; for speak/dtmf/hang_up it appears in the emitted spec only and is NOT applied as a mute
  ("Do NOT apply silence_after_cue_ms as a multi-second freestyle mute — that contradicted hybrid SI
  ('continue until next cue'). Intentional caller silence: action=wait + silence_after_cue_ms").

Actions:
- **speak**: if not barge_in → `_wait_agent_idle(timeout_s=6.0)` (never talks over agent). If
  barge_in and with_blip and delivery != room_pcm → inject `"[barge blip]"` label `f"{label or id}-blip"`
  delivery=room_pcm asset=`builtin:noise.blip` (blip failure → `sim.script.error`, chain continues). Then
  `bridge.inject_cue(say, label, delivery, asset, scenario_dir, gain, loop)`. Any exception →
  `inject_error = f"{TypeName}: {e}"` + `sim.script.error`. If `barge_in and during_agent_speech and no
  inject_error` → emit `interruption` spec `{"by":"sim","barge_in":True,"class":icls,"false_positive":
  icls in ("noise","backchannel"),"step_id","label","say","note":"Script barge-in while agent was active
  speaker"}` source=sim.script.
- **wait**: `hold_silence_ms > 0` → `mute = False if step.mute_persona is None else bool(step.mute_persona)`;
  `bridge.begin_scripted_user_silence(hold_silence_ms, mute_persona=mute)` (TypeError fallback: if mute,
  duration-only); if no begin_scripted_user_silence and mute → `suppress_persona_output(hold_silence_ms)`.
  Then paced hold: sleep in <= 0.25 s slices, nudge only when `not mute`.
- **hang_up**: `silent = bridge._silent_mode`; if silent → `say_text=""`; elif empty → locale default
  farewell (en/en-us/en-gb → "Okay, thanks. Bye."; vi/vi-vn → "Cảm ơn bạn. Tạm biệt."; ja/ja-jp →
  "ありがとうございます。失礼します。" — match on exact or base-lang, fallback en). `_wait_agent_idle(5.0)`;
  if say_text → `begin_script_hangup_farewell()`; `inject_cue(...)` (error captured, not raised); emit
  `sim.script.hang_up` + `sim.hang_up` (spec below); drain:
  `drain_s = min(10.0, max(5.0, 1.2 + words*0.45))`; `bridge.drain_persona_speech(timeout_s=drain_s)` else
  `asyncio.sleep(min(4.0, drain_s))`; `await asyncio.sleep(0.55)`; finally `end_script_hangup_farewell()`.
  Emit `sim.script.hang_up` + `sim.hang_up` a SECOND time (verify matches step_id on this kind), then
  `bridge.sim_hang_up()` and return.
- **dtmf**: `digits = step.digits or ""`; DMAP `{"0":0.."9":9,"*":10,"#":11}`; per char: `"w"` →
  sleep 0.12 s; digit → `await local.publish_dtmf(code=DMAP[ch], digit=ch)` then sleep 0.15 s; unknown char
  or publish exception → `inject_error`, break. No `_wait_agent_idle` on dtmf (digits may land mid-agent-speech).

`_maybe_nudge_freestyle` (SI): non-text kick — `audio_stream_end` only; guards: not agent active, bridge has
`nudge_freestyle_answer`, `agent_left_open_turn(last_agent_final_text)`, `last_agent_final_mono` set, no user
final after agent final, quiet >= 1600 ms, >= 3.5 s since last nudge; exceptions swallowed → False.

### 3.3 hang_up gate (hang_up_gate.py)

`agent_left_open_turn(text)`: blank → False. Lowercase. `_CLOSING_MARKERS` = ("goodbye","good bye",
"bye for now","bye.","bye!","have a great","have a good","take care","thank you for calling",
"thanks for calling","call ended","hanging up") — closing wins (→ False). `"?"` anywhere → True. Else True
iff any `_OPEN_PROMPT_MARKERS` substring (44 markers): "what's your","what is your","what was your",
"which car","what sort of","may i have","can i have","could you","can you tell","can you give","can you
hear","still there","are you there","please provide","please tell","your name","full name","phone number",
"email address","card number","how can i help","anything else","shall we","would you like","do you want",
"are you ready","whereabouts". Docstring: "Heuristic only — used to defer Script hang_up, not to ban
barge-in."

`_hang_up_ready(step)` — reason candidates in order:
1. `require_agent_reply_this_turn` and `user_has_spoken and not agent_replied_this_turn` →
   "awaiting_agent_reply".
2. `defer_on_open_question` and `agent_left_open_turn(last_agent_final_text)` and NOT (`last_user_final_mono`
   and `last_agent_final_mono` exist and user_t > agent_t) → "open_agent_question".
No reason → discard defer state, return True. **Defer budget**: `budget_ms = max(0, open_question_idle_ms)`
(default 20000); `started` set on FIRST defer, NEVER reset by new agent finals ("single wall-clock budget
from the first defer... New agent questions must not reset that budget (otherwise freestyle loops never hang
up)"); `deferred_ms >= budget_ms` → emit `sim.script.hang_up_deferred` `{step_id, reason:
"defer_budget_exhausted", prior_reason, deferred_ms, budget_ms, last_agent_final: (text or "")[:240]}`
source=sim.script include_dialogue=False, return True (ghost hang allowed). Else first defer → emit same
event with `reason` (one-shot via `_hang_up_defer_emitted`), return False.

`_wait_agent_idle(timeout_s=5.0)`: poll 50 ms until `not agent_is_active_speaker` or stop or deadline.

### 3.4 End-call / farewell heuristics (end_call.py — port regexes byte-for-byte)

- `END_CALL_TOKEN = "[END_CALL]"`.
- `_SPOKEN_END_RE = re.compile(r"(?i)(?:\[\s*end[_\s\-]*call\s*\]|\bend[_\s\-]*call\b|\bhang[_\s\-]*up\b)[.!?]*")`
- `_FAREWELL_RE = re.compile(r"(?i)(?:\bgood\s*bye\b|\bgoodbye\b|\bbye[\s\-]?bye\b|\bbye\b|\bsee\s+you(?:\s+later)?\b|\btalk\s+later\b|\btalk\s+soon\b|\bthat'?s\s+all\b|\bthanks?\s+again\s+for\s+your\s+time\b|\bthank\s+you\s+for\s+your\s+time\b|\bi'?ll\s+(?:be\s+)?back\s+in\s+touch\b|tạm\s*biệt|kết\s*thúc|cúp\s*máy)[.!?]*")`
  (Vietnamese forms included).
- `contains_end_call_signal(text)`: falsy → False; `END_CALL_TOKEN in text` → True; else `_SPOKEN_END_RE.search`.
- `contains_farewell_signal(text)`: falsy → False; end-call → True; else `_FAREWELL_RE.search` ("True for
  bye/goodbye-style closings (with or without harness token)").
- `strip_end_call_signal(text)`: replace token with " ", `_SPOKEN_END_RE.sub(" ", out)`,
  `re.sub(r"\s+([,.!?])", r"\1", out)`, `re.sub(r"[,\s]+$", "", out)`, `" ".join(out.split()).strip()`.
- `strip_farewell_signal(text)`: strip_end_call_signal then `_FAREWELL_RE.sub(" ", out)`, same two regex
  passes, same final collapse.
- `should_end_call_on_turn(*, pending_script, ended, farewell, scripted_farewell)`: False if
  scripted_farewell; False if pending_script; else `bool(ended or farewell)` ("Soft bye alone is enough when
  no Script owns hang-up — Gemini often says 'Bye' without emitting [END_CALL]. Script-pending turns defer
  instead.").

### 3.5 Script event specs (exact)

- **sim.script.cue / sim.script.wait / sim.script.hang_up / sim.script.dtmf** (hang_up emits twice):
  `{"step_id", "label": label or id, "say": step.say (hang_up: resolved say_text), "trigger", "action",
  "barge_in", "class": step.interrupt_class, "overlay": effective_overlay(step), "delivery": (if action not
  wait/dtmf), "asset": (if action not wait/dtmf), "digits": (dtmf only), "gain": (speak only), "loop":
  bool(step.loop) (speak only), "waited_ms": elapsed_ms, "hold_silence_ms": (wait only), "agent_active",
  "agent_active_ms", "during_agent_speech": bool, "error": inject_error}`; source="sim.script",
  include_dialogue=False.
- **sim.hang_up**: script variant `{"step_id","label","say": say_text, error?}` source=sim.script; bridge
  variant `{"source":"script","by":"sim"}` source="sim" (from `bridge.sim_hang_up()`).
- **sim.script.hang_up_deferred**: as §3.3.
- **sim.script.error**: `{"step_id","label","delivery","asset","error": f"{TypeName}: {msg}"}`
  source=sim.script include_dialogue=False. (Bridges also emit it: gemini_text failure → sapi_fallback;
  role/say mismatch with heard/expected [:240].)
- **interruption** (script barge): §3.2 speak action — emitted only when barge_in ∧ during_agent_speech ∧ no
  inject error.
- **sim.script_inject** (bridges, source="script"): room_pcm path `{"text","label","delivery","asset":
  str(wav_path), "mix": "speech"|"parallel"|"parallel_loop", "duration_ms": int(duration_s*1000), "gain",
  "voice_gain", "loop"}`; gemini_text path `{"text","label","delivery","gain","voice_gain","effective_gain":
  clamp(gain*voice_gain), "attempt": 1}`; sapi fallback adds `"duration_ms"` and `"delivery":"sapi"`.
- **sim.script_deferred_end_call**: `{"text": clean, "reason": "script_steps_pending"}` source="sim.gemini"/"sim.openai".
- **sim.silent_mode_skip_inject**: `{"label","delivery","text": (text or "")[:120]}` source="sim".
- **sim.end_call_token**: `{"text": clean, "reason": "end_call_token"|"farewell"}` source="sim.gemini"/"sim.openai".

### 3.6 verify.py computation

Events: cues = kinds (`sim.script.cue`, `sim.script.wait`, `sim.script.hang_up`) — NOT dtmf; agent_finals =
`transcript.agent.final`; user_finals = `transcript.user.final`; interruptions = kind `interruption`.
Per step: first cue matching `spec.step_id == step.id`; none → fail "script step not fired"; `spec.error` →
fail "cue fired with error: ..."; **hardcoded (spec field ignored): `step.trigger == "agent_speaking" and
step.action == "speak" and not during_agent_speech` → fail "cue fired but agent was not active speaker"**;
else pass with `{during_agent_speech, trigger, action}`.
Timestamps: `cue_ms` = first cue's ts; silence_ms from cues with trigger=="silence" or action=="wait";
barge_ms from cues with kind sim.script.cue and (counts_for_recovery_barge OR legacy heuristic: no class AND
during_agent_speech AND trigger==agent_speaking AND `int(spec.waited_ms or 9999) < 800`).
Counts: `agent_after_cue` / `user_after_cue` with ts >= cue_ms; `agent_after_silence` >= silence_ms;
**`agent_after_barge` STRICTLY > barge_ms**. Checks appended only when spec value > 0 (or not None):
min_agent_finals_after_first_cue, min_user_finals_after_first_cue, min_agent_finals_after_silence,
min_agent_finals_after_barge_in, min/max_interruptions. Plugins: unregistered → fail `plugin:<name>`;
missing scenario/project_root → fail; exception → fail `f"{TypeName}: {e}"`; result `{"pass", "checks"
(merged with "plugin" key), "detail"}`.
Return: `script_steps`, `cues_fired`, `waits_fired`, `hang_ups_fired`, the counts above, `interruptions`,
`checks`, `plugin_results`, **`pass: all(check.pass) if checks else False`** — empty checks (empty script) FAILS.

### 3.7 Summary computation (summary.py — zeros-safe)

cues = kind `sim.script.cue`; waits = `sim.script.wait`; barges = cues where counts_for_recovery_barge;
`barges_during` = barges with during_agent_speech; `cues_during` = all cues with during_agent_speech;
silence_events = `silence.detected`; interruptions = `interruption`; agent_finals = `transcript.agent.final`.
`barge_ms` = first barge ts; `silence_ms` = first wait ts; `recovery_ms` = first agent final with ts > barge_ms
minus barge_ms; `agent_after_silence` = finals ts >= silence_ms; `cue_assets` = unique non-empty spec.asset
from `sim.script.cue` + `sim.script_inject`. Output keys: `script_cues_fired, waits_fired, barges_fired,
barges_during_agent, cues_during_agent, silences_held (=len(waits)), silence_events, interruptions,
agent_finals_after_barge, agent_finals_after_silence, recovery_ms, cue_assets, by_class` (from spec.class or
spec.interrupt_class, including non-barge classes).

### 3.8 Delivery semantics recap (bridge inject_cue, both providers)

silent_mode + not hangup farewell → `sim.silent_mode_skip_inject`, return. room_pcm: `load_wav_pcm`; mono
only (ValueError "Only mono room_pcm assets are supported"); rate must equal OUT_RATE 24000 (ValueError
"room_pcm asset rate {rate} != sim mic 24000 (resample cue WAV): {path}"); `duration_s = max(0.05,
len(pcm)/2/rate)`. voice.* → suppress_persona_output(int(duration_s*1000)+400); speech_gain = clamp(gain *
voice_gain); push_speech; end_speech_turn (complete WAV — drain without jitter waterline hold); sleep
duration_s. Noise → push_noise(gain=gain, loop=loop); one-shot sleeps duration_s; loop sleeps
`min(0.05, duration_s)` ("Continuous bed: arm quickly so Script/freestyle can continue under noise").
Emit sim.script_inject, return. gemini_text (non-farewell): SAPI-first when mixer exists, drain 8 s + 0.2 s;
else `_inject_gemini_text` (Live); on exception → sim.script.error "gemini_text failed (...); trying
sapi_fallback" → sapi; final failure raises.

---

## 4. Appendix: Observer + lk.agent.session protocol

### 4.1 Track subscription + recording (observer.py)

- AudioStream: `rtc.AudioStream(track, sample_rate=16_000, num_channels=1)` — 16 kHz mono PCM16 from the
  agent-identity audio track ONLY (`p.identity == self.agent_identity and track.kind == KIND_AUDIO`). The
  observer never subscribes to sim/caller audio.
- Two triggers: (a) `room.on("track_subscribed")` handler → start record; (b) pre-attach catch-up: on
  `attach()`, iterate `room.remote_participants` for the agent and already-subscribed audio tracks
  ("Agent track may already be subscribed before attach (common on SIP legs)"). Dedupe by `track.sid or
  id(track)` — second record of same sid is a no-op. Recording only if `recorder is not None`.
- Pump: emits `sim.agent_audio_recorded {track_sid, source="observer.agent_room", sample_rate:16_000}`;
  pushes PCM to `recorder.push_agent(pcm, 16_000, track_id=key)` AND feeds the same bytes to the onset
  detector; exception → `sim.error {where:"observer.agent_record", error, track_sid}`; finally closes the
  stream. No `track_unsubscribed` handler — the pump just ends.

**Rust SDK mapping (verified, livekit 0.8.3 / livekit-data-stream 0.1.2):** the SDK is event-driven — there
is NO `register_text_stream_handler`-style registration API. Pattern: `room.subscribe()` →
`mpsc::UnboundedReceiver<RoomEvent>`; match `RoomEvent::TextStreamOpened { reader: TakeCell<TextStreamReader>,
topic, participant_identity }` / `ByteStreamOpened` (room/mod.rs:228-231 / :223-226); `reader.take()` exactly
once per event (TakeCell semantics — a second take returns None); spawn per-stream read loops with
`StreamExt::try_next()` (Stream<Item=StreamResult<Bytes>>) or `read_all()` for whole-stream semantics
(observer uses read_all for lk.transcription). `TextStreamInfo` (info.rs:66-104): `id` (uuid — NO `sid`
field; stream SID is only on TrackPublication), `topic`, `timestamp`, `total_length`, `mime_type`,
`operation_type`, `version`, `reply_to_stream_id`, `attached_stream_ids`, `generated`, `encryption_type`,
`attributes() -> HashMap<String,String>` (header attributes manager.rs:282 merged with trailer attributes
manager.rs:466-486). Internal topics `lk.rpc_request`/`lk.rpc_response` (rpc/mod.rs:37-38) are consumed by
the SDK's RpcServerManager/RpcClient and never surface as RoomEvents — same as Python. Fallback if the
high-level API is ever dropped: deprecated `StreamHeaderReceived`/`StreamChunkReceived`/`StreamTrailerReceived`
raw proto events remain.

### 4.2 RmsOnsetDetector (vad.py) + sim.agent.audio_onset

Construction args (exact, from observer.py:107-115): `sample_rate=16_000` (hardcoded), `win_ms=ao.win_ms`
(default 20), `threshold=ao.threshold` (default 0.012 — "0.012 of full-scale PCM16 is the initial default
for tuning, NOT an immutable ground truth"), `energy_frames=ao.energy_frames` (default 3),
`exit_frames=ao.exit_frames` (default 5), `refractory_ms=ao.refractory_ms` (default 60),
`on_onset=self._on_agent_onset`. Derived: `win_samples = (16000*20)//1000 = 320`;
`refractory_samples = (16000*60)//1000 = 960`. VAD method string = `ao.vad`, default "rms", validated
`in ("rms",)` else ConfigError "must be `rms`... other VAD backends are future work"; threshold validated
`0.0 < threshold < 1.0`. Detector built only `if recorder is not None and observe.audio_onset.enabled`.

State machine: SILENCE→SPEECH requires `energy_frames` CONSECUTIVE windows with RMS >= threshold; onset
fires at the FIRST frame of the run: `onset_start = start - (energy_frames-1)*win_samples`, clamped >= 0.
First onset records `frames_before_first_onset = onset_start`. Refractory checked BEFORE speech entry: if
`(onset_start - _last_onset_consumed) >= refractory_samples` → fire; else still enters SPEECH without firing
(one long burst = one onset). SPEECH→SILENCE: `exit_frames` consecutive windows below threshold.
Chunk-boundary invariant: rolling `array("h")` buffer; windows consumed only when fully available; odd byte
dropped; flush() drops a partial window ("no onset can begin mid-window"). Window energy:
`RMS = sqrt(sum(s^2)/n)/32768.0`; `pcm16_mono_rms()` same formula, 0.0 for empty.

`onset_to_audio_ms(frame_idx, rate) = int(round(frame_idx * 1000 / rate))`.

`sim.agent.audio_onset` payload (EXACT): `{"channel":"agent", "sample_rate":16_000, "onset_frame_idx",
"frames_before_first_onset": int(frames_before_first_onset or 0), "vad": {"method": ao.vad, "threshold",
"win_ms", "energy_frames", "refractory_ms"}}`; emit args source="sim", include_dialogue=False, turn=None,
ts_mono_ms=corrected_ts. **Backdating formula**: `audio_t0_mono = int((recorder.started_mono - writer.t0_mono)
* 1000)`; `onset_ms = onset_to_audio_ms(onset_frame_idx, 16_000)`; `corrected_ts = max(0, audio_t0_mono +
onset_ms)` — "the *perceived* agent speech onset — never detection time". Returns silently if recorder None
or `started_mono is None`. EventWriter additionally clamps `mono = max(0, int(ts_mono_ms))`.
Caveat: `started_mono` is pinned by `mark_start()` (mic publish) OR lazily at first push; the recorder's
`_push` gap-pads only when `gap_s > 0.02` (`if gap_s > 0.02: pad = int(gap_s*sample_rate)`) — the detector's
frame index is buffer-relative, not wall-clock-relative (can lag < 20 ms per inter-chunk gap).

### 4.3 Transcription intake

(a) **lk.transcription text stream**: registered only if `observe.lk_transcription` (default True). Handler
(reader, participant_identity) wrapped in a task. Read `text = await reader.read_all()` — FULL stream, not
incremental; exception → `observer.error {where:"lk.transcription", error:"TypeName: msg"}`. The payload is
PLAIN TEXT (not JSON). Attributes from `reader.info.attributes`: `ATTR_FINAL = "lk.transcription_final"`
(value `attrs.get(...).lower() == "true"`); `ATTR_SEGMENT_ID = "lk.segment_id"`. Empty text dropped silently.
Role: `"agent" if participant_identity == agent_identity else "user"` (ANY other participant → "user").
Calls `on_transcript(role, text, final, segment_id, source="lk.transcription")`.
Rust: attribute keys `lk.transcription_final`/`lk.segment_id` are NOT named constants in the SDK — define
them in the port; the attribute transport itself (header+trailer merge) is verified.

(b) **TranscriptionReceived**: Python observer.py has NO TranscriptionReceived handler — transcription-like
intake comes from data-topic `transcript_turn` payloads (§4.5) and direct `on_transcript` calls from the
caller bridges (`sim.gemini`/`sim.openai`). Note for the Rust port: the Rust SDK DOES expose
`RoomEvent::TranscriptionReceived { participant: Option<Participant>, track_publication:
Option<TrackPublication>, segments: Vec<TranscriptionSegment> }` (room/mod.rs:209-212) — available if the
port ever wants it, but Python parity does NOT consume it. `TranscriptionSegment { id, text, start_time,
end_time, r#final, language }`.

(c) **sim.heard_agent**: informational mirror from the bridges, NOT fed to on_transcript.

### 4.4 `_accept_final` dedupe + turn model (observer.py:426-547)

Priority tables (lower index = higher priority) — port verbatim, do not unify:
```
_SIM_TRANSCRIPT_SOURCES = ("sim.gemini", "sim.openai")
_USER_FINAL_PRIORITY    = ("sim.gemini", "sim.openai", "data", "lk.transcription")
_AGENT_FINAL_PRIORITY   = ("data", "lk.transcription", "sim.gemini", "sim.openai")
```
Rationale: "Provider sim-transcript sources (sim.gemini / sim.openai) are the most trustworthy caller
transcripts; data-topic and lk.transcription are mirrors" — user trusts SIM sources, agent trusts
DATA/lk.transcription (the agent's own transcript is most trustworthy from the agent SDK).

`_canonical_source(source)`: unchanged if in (*_SIM_TRANSCRIPT_SOURCES, "lk.transcription"); EVERYTHING else
(any other string, incl. arbitrary data topics like "my.topic") → "data". Two different data topics collide
on the same dedupe key.

`_accept_final(role, text, source)`:
1. `norm = re.sub(r"\s+", "", text.strip())` — ALL whitespace stripped, case-sensitive. Empty → False.
2. `key = (role, norm)`; `window = observe.transcript_dedupe_window_ms / 1000` (default 15_000 ms,
   checked `now - prev_mono <= window_s` inclusive).
3. Prev entry exists and within window → DROP iff `rank(new) >= rank(prev)` — the new source must be
   STRICTLY higher priority (lower rank) to replace; equal-or-lower within window → False.
4. Accepted → store (source, now), return True. On accept the stored entry is OVERWRITTEN even by a
   lower-priority source arriving > 15 s later (no tombstone; a later duplicate with lower priority CAN win
   outside the window). Entries are never pruned/expired.

`_similar_text(a, b)`: a==b → True; either empty → False; shorter-in-longer substring →
`len(shorter)/len(longer) >= 0.85`; else False.

`on_transcript(role, text, final, segment_id=None, source="lk.transcription")` — ordering (side effects
BEFORE dedupe; dropped finals still count as activity):
1. `writer.update_dialogue(role, text, final, at_ms=wall_now_ms)`.
2. spec = {text, final} + segment_id if present.
3. Any transcript: role==agent → `last_agent_activity_mono=now`, `_agent_has_spoken=True` (interim counts —
   "realtime providers deliver final late"); ALWAYS `last_activity_mono=now`, `_any_activity=True` ("Caller
   activity also counts: the dead-call net must not fire while the agent is still working on its first reply").
4. Not final: if `_role_has_final(role)` → return (DROP late interim after final — "OpenAI bridge can
   deliver trailing .delta after .done"); else emit `transcript.{role}.interim`, return.
5. Ordering guard: if `source == "sim.openai"` and `_last_interim_key != (role, text)` → set key, emit
   `transcript.{role}.interim` with spec {**spec, "final": False} (final-flavored interim — "Emit a
   final-flavored interim first so consumers never see interim-after-final within a turn"). Only for
   source=="sim.openai".
6. `if not _accept_final(...): return`.
7. USER branch: `if _last_user_final_mono is not None and not _agent_replied_this_turn:` → emit
   `transcript.user.final` spec {**spec, "same_turn": True}, return (does NOT advance turn). If
   `_agent_replied_this_turn and (norm == _current_turn_user_norm or _similar_text(norm,
   _current_turn_user_norm or ""))` → return (duplicate utterance within the turn). Else new turn:
   `_user_has_spoken=True; turn += 1; _current_turn_user_norm = norm; writer.begin_turn(turn);
   _finalized_roles.clear(); _last_user_final_mono = now; _agent_replied_this_turn=False;
   _finalized_roles.add("user")`; emit `transcript.user.final` spec=spec.
8. AGENT branch: if `turn == 0 and first_speaker == "user" and not _user_has_spoken` → emit
   `transcript.agent.preamble` spec {**spec, "note": "agent spoke before user; not counted as a turn"}
   (EXACT note string), return — no turn increment, no `_agent_replied_this_turn`. If `turn == 0` → turn=1,
   `writer.begin_turn(1)` (first_speaker=="agent" case). If `not _agent_replied_this_turn and
   _last_user_final_mono is not None`: `spec["turn_taking_ms"] = int((mono_now - _last_user_final_mono)*1000)`
   — set ONLY on transcript.agent.final, ONLY when this is the first agent reply of the turn AND a user
   final exists. Then `_agent_replied_this_turn=True; _last_agent_final_mono=now; _last_agent_final_text=text;
   _finalized_roles.add("agent")`; emit `transcript.agent.final`.

Two clocks — monotonic for dedupe/turn_taking/ts_mono_ms; wall clock for `ts`/`at_ms`. Do not merge.

### 4.5 Data topics

`room.on("data_received")` → topic = packet.topic or ""; FILTER: `if observe.data_topics and topic not in
observe.data_topics: return` — empty list (default) = observe ALL topics ("empty=all"). Payload parse:
`json.loads(packet.data.decode("utf-8"))`; parse failure → emit `data.raw {topic, bytes: len(packet.data),
sender}` (include_dialogue=False, source=topic or "data"), return. On success:
1. `_match_tool_patterns(topic, payload)` — first matching pattern emits and returns True. Patterns from
   `observe.tool_event_patterns` (ToolEventPattern: match: dict key→expected, emit: str). Matching: for each
   (key, expected): key=="topic" → exact topic comparison; else `_lookup_path(payload, key) != expected` →
   False; ALL must match (AND). `_lookup_path`: dotted-key traversal, non-dict → None.
2. `_parse_transcript_payload(payload)`: `payload.get("type") in observe.transcript_payload_types` (default
   ["transcript_turn"]); `payload.get("interim")` truthy → None; `turn = payload.get("turn")` must be dict;
   `role = turn.get("role")` in ("user","agent"); `text = turn.get("text")` str non-empty-after-strip;
   returns (role, text.strip()).
3. If parsed: `on_transcript(role, text, final=True, source=topic or "data")` — always final=True; source =
   the actual topic string (canonicalized to "data" for ranking).
4. If neither: emit `data.message {topic, sender, payload}` (source=topic or "data", include_dialogue True).

Tool event emission: `name = payload.get("tool") or payload.get("name") or _lookup_path(payload,"spec.name")`;
`call_id = payload.get("call_id") or payload.get("toolCallId") or _lookup_path(payload,"spec.call_id")`;
spec = {name, call_id, payload}. emit_kind=="tool.start" → emit("tool.start", source=topic), store event in
`_open_tools[call_id]` if call_id. Else (tool.end/tool.error): pop `_open_tools[call_id]` →
`parent_id = start["event_id"]`, `spec["duration_ms"] = int((mono_now - writer.run_start_mono)*1000) -
start["ts_mono_ms"]` (data-topic path does NOT clamp — can go negative); tool.error adds
`spec["error"] = payload.get("error") or payload.get("message") or _lookup_path(payload,"spec.error")`;
emit with parent_event_id. No start → parent None, no duration_ms.

Rust SDK: `RoomEvent::DataReceived { payload: Arc<Vec<u8>>, topic: Option<String>, kind: DataPacketKind,
participant: Option<RemoteParticipant> }` — participant is Option; treat None gracefully. Sending:
`LocalParticipant::publish_data(DataPacket { payload, topic, reliable, destination_identities })`.

### 4.6 L3 — lk.agent.session (agent_session_observer.py)

- Topic: `TOPIC_SESSION_MESSAGES = "lk.agent.session"`. Registered via
  `room.register_byte_stream_handler`; handler filters `participant_identity != agent_identity` → return.
- **Framing**: accumulate ALL chunks (`async for chunk in reader`) into a bytes list; then parse ONE
  concatenated `AgentSessionMessage` protobuf per byte stream — NOT length-prefixed, NOT repeated. Parse or
  read error → `observer.error {where:"lk.agent.session", error}`.
- Dispatch: HasField("response") → pop `_pending_requests[response.request_id]`; set result. HasField("event")
  → handle.
- Event oneof (order): `function_tools_started` → per call `_emit_tool_start`; `function_tools_executed` →
  `_handle_tools_executed`; `tool_execution_updated` → `_handle_tool_execution_updated`; `conversation_item_added`
  → `_handle_conversation_item_added`; `agent_state_changed` → emit "session.agent_state" {old_state,
  new_state} (enum value→name via `_enum_name`, numeric fallback); `user_state_changed` → "session.user_state";
  `session_usage_updated` → "session.usage" (dedupe: identical consecutive usage dict → only first);
  `error` → "session.error" {message}; `overlapping_speech` → "session.overlapping_speech"
  (MessageToDict); `debug_message` → "session.debug" ONLY if any value not in ("", None, [], {}) —
  non-message debug → {"message": str(...)}. All via `_emit_session`: source="lk.agent.session",
  include_dialogue=False. MessageToDict flags: `preserving_proto_field_name=True`,
  `use_integers_for_enums=False`.
- tool_execution_updated fallback: always emit "session.tool_execution" (MessageToDict). "started" with
  HasField → function_call with name/call_id/id → _emit_tool_start. "ended" → key = call_id or id; skip if
  in `_completed_call_ids`; `status_name = _enum_name(ended, "status")`;
  `is_error = status_name in ("TC_ERROR", "TC_CANCELLED")` (EXACT tuple); output from
  `ended.message or status_name or ""`; emit paired. ("Some agent SDKs emit tool_execution_updated before
  function_tools_executed... teardown races still record tools.")
- conversation_item_added fallback: item oneof "function_call" → _emit_tool_start; "agent_handoff" →
  _emit_handoff.
- `_emit_handoff`: `old_id = handoff.old_agent_id or ""; new_id = handoff.new_agent_id or ""`; if
  `not old_id or old_id == new_id` → return (no-op — "initial agent assignment, NOT a transfer"
  false-positive guard; OpenAI Realtime emits AgentHandoff at session start with old_agent_id empty and
  created_at epoch 0). Emit "session.handoff" {id: handoff.id or None, old_agent_id, new_agent_id,
  created_at: created.ToJsonString() if created is not None else None} — Timestamp → ISO string via
  ToJsonString() (raw Timestamp crashed the writer — regression note).
- `_tool_key(call_id, item_id) = call_id or item_id or None`.
- `_emit_tool_start(call)`: spec = {id: call.id or None, call_id: call.call_id or None, name: call.name or
  None, arguments: call.arguments}; dedupe via `_started_call_ids` (returns stored event); emit "tool.start"
  source=_SOURCE; store in `_open_tools[key]`.
- `_emit_tool_output(output, paired_start, paired_key)`: dedupe via `_completed_call_ids`; spec = {id,
  call_id, name, output, is_error: bool}; fill name from paired start; `duration_ms = max(0,
  int((mono_now - writer.run_start_mono)*1000) - int(start["ts_mono_ms"]))` (L3 path CLAMPS);
  is_error → spec["error"] = output.output; emit "tool.error" if is_error else "tool.end" with
  parent_event_id = start["event_id"] if start.
- `_handle_tools_executed`: pair by array index; call_id mismatch between call and output →
  emit "observer.warning" {where:"function_tools_executed", message:"call/output call_id mismatch; paired by
  array index", call_id, output_call_id, index} (EXACT message); leftover outputs emitted unpaired.
- Requests: `future = loop.create_future(); _pending_requests[request_id] = future`;
  `stream_bytes(name=f"AS_{uuid.uuid4().hex[:12]}", topic=TOPIC_SESSION_MESSAGES,
  destination_identities=[agent_identity])`; ONE write of the serialized message; `aclose`;
  `await asyncio.wait_for(future, timeout=60.0)` (request_timeout_s default 60.0). Any exception → pop
  pending, re-raise. `response.error` truthy → `RuntimeError(f"session request {request_type} failed:
  {response.error}")`.
- Snapshots: `fetch_session_snapshot()` = get_chat_history then get_session_usage, each swallowing errors via
  `observer.error {where:"lk.agent.session.<op>", error}` ("without making snapshot failure fatal").
  Request ids `req_<12 hex>`; bodies SessionRequest(request_id, get_chat_history=...) /
  get_session_usage=.... Chat history → emit "session.chat_history" {items} where item dict = oneof:
  function_call → {type, id, call_id, name, arguments}; function_call_output → {type, id, call_id, name,
  output, is_error}; message → {type, ...MessageToDict(message)}; agent_handoff / agent_config_update →
  {type, ...MessageToDict}; else {type: "unknown"}. Then `_reconcile_history(items)`: per item —
  function_call → _emit_tool_start; function_call_output → ensure paired start from calls_by_key then
  _emit_tool_output; agent_handoff → _emit_handoff.
- detach order: `drain_ingress(timeout_s=0.75)` FIRST ("Prefer draining late tool events before tearing the
  handler down"), then unregister handler, then cancel pending futures + tasks (gather). drain_ingress
  (default 1.5 s) = `asyncio.wait_for(gather(*pending, return_exceptions=True), timeout=...)`; on
  TimeoutError leave tasks for detach ("tools that delete the room publish function_tools_*/... as the peer
  disconnects; cancelling ingress drops frames → false-negative tool.start/tool.end").
- Observer integration: constructed if `observe.lk_agent_session` (default True); attach on observer attach;
  `finalize_session_snapshot()` = drain_ingress(1.5) then fetch_session_snapshot; public post-disconnect
  hook drain_session_ingress(1.5); detach cancels record tasks then agent_session.detach().

**Rust SDK mapping (verified):** `ByteStreamReader` implements the same StreamReader trait
(Stream<Item=StreamResult<Bytes>>) with `info()`, `progress()`, `read_all()`, `write_to_file(directory,
name_override)`. `ByteStreamWriter` (outgoing/stream_writer.rs:62-65) + StreamWriter trait: `info()`,
`write(&[u8])`, `close()`, `close_with_reason(reason)`, `close_with_options(reason, attributes)` (trailer
attributes). `StreamByteOptions` HAS `pub destination_identities: Vec<ParticipantIdentity>` (line 40) with
`with_destination_identities`/`with_destination_identity` builders ("empty list delivers to all participants
in the room") AND `sender_identity: Option<ParticipantIdentity>` for agent impersonation; `StreamTextOptions`
likewise. Entry points: `local_participant.stream_bytes(options) -> StreamByteWriter`,
`stream_text(options)`, `send_bytes(data, options)`, `send_text`, `send_file`. Receiving the RESPONSE on the
same topic: RoomEvent carries no request correlation — correlate via attributes/stream id. RPC (if used):
`RpcServerManager` (room/rpc/server.rs:47): `register_method(method: String, handler: Fn(RpcInvocationData)
-> Pin<Box<dyn Future<Output=Result<String,RpcError>>+Send>>+Send+Sync+'static)`; handles v1 packets and v2
data-stream requests (wired from TextStreamOpened on lk.rpc_request at room/mod.rs:2415-2423).

### 4.7 Active-speaker / activity state (observer.py)

`room.on("active_speakers_changed")` (LiveKit event — energy-based, not transcripts): agent in speakers →
`_agent_active_since_mono = now`, `agent_is_active_speaker = True`, `last_agent_activity_mono = now`,
`_agent_has_spoken = True`; else `_agent_active_since_mono = None`, flag False; emits "room.active_speakers"
{identities}. `agent_active_duration_ms()` = `int((mono_now - _agent_active_since_mono)*1000)` or None.
`agent_replied_this_turn`: user final sets False + turn += 1; agent final sets True. `agent_has_spoken`,
`user_has_spoken`, `last_user_final_mono`, `last_agent_final_mono`, `last_agent_final_text`. Turn = one user
utterance + the agent reply to it. `agent_has_spoken` flips via EITHER active-speaker energy OR any agent
transcript (interim or final) — gates never hang on missing transcripts.

Rust SDK: `room.active_speakers_changed`-equivalent — the crate exposes active speakers via RoomEvent
`ActiveSpeakersChanged` (identity list); use the same energy semantics. Rust SDK also returns transcription
and DTMF as RoomEvents with `Option<Participant>` — treat None gracefully.

### 4.8 Event envelope (event_writer.py — I1 byte-compat)

Per emit: `event_id = "evt_" + uuid4().hex[:12]`; `seq` 1-based incremented BEFORE emit (summary
event_count = seq + 1); `run_id`; `turn` (writer.turn unless overridden; onset events pass None →
writer's CURRENT turn); `kind`; `ts = int(now.timestamp()*1000)` (wall); `ts_mono_ms = int((monotonic -
t0_mono)*1000)` or max(0, override); `datetime_utc` "YYYY-MM-DDTHH:MM:SS.mmmZ"; `datetime_local` same with
ZoneInfo tz (default UTC); `source`; `parent_event_id`; `dialogue` (if include_dialogue, default True);
`spec` (or {}). Append + flush to events.jsonl per emit. Dialogue snapshot {user: {text, final, at_ms} or
note "user has not spoken yet this turn", agent: same}; `begin_turn(turn)` sets writer.turn and clears ONLY
the agent dialogue (keeps user utterance, resets agent to {text:None, final:False, at_ms:None}).
turn_metrics rows only for turn > 0: {turn, user_text (from transcript.user.final spec.text), agent_text
(from transcript.agent.final spec.text), turn_taking_ms (guarded: only overwrite when
`spec.get("turn_taking_ms") is not None`), tool_count (tool.start), tool_errors (tool.error), interrupted
(kind == "interruption")}. `run.ended` is emitted by the orchestrator (not in these files).

### 4.9 Full observer event catalog

room.participant_connected {identity,name,kind}, room.participant_disconnected {identity}, room.track_subscribed
{identity,kind,sid}, room.active_speakers {identities}, room.disconnected {}, data.message {topic,sender,payload},
data.raw {topic,bytes,sender}, sim.agent_audio_recorded {track_sid,source,sample_rate}, sim.agent.audio_onset
(§4.2), sim.error {where,error,track_sid}, observer.error {where,error}, observer.warning, transcript.{user|agent}.interim
{text,final[,segment_id]}, transcript.user.final {text,final[,segment_id][,same_turn]}, transcript.agent.final
{text,final[,segment_id][,turn_taking_ms]}, transcript.agent.preamble {text,final[,segment_id],note},
tool.start {name,call_id,payload}, tool.end {name,call_id,payload[,duration_ms]}, tool.error {name,call_id,payload[,duration_ms,error]},
session.agent_state, session.user_state, session.usage, session.error {message}, session.overlapping_speech,
session.debug, session.tool_execution, session.chat_history {items}, session.handoff {id,old_agent_id,new_agent_id,created_at},
run.ended.

---

## 5. Wire-verification deltas and crate decisions (sealed)

From the two independent verifications, the sealed Rust choices (do not revisit):

1. **Gemini**: vendor `gemini-live 0.1.8` (MIT, ~1400 LOC, tokio + tokio-tungstenite 0.29 + rustls 0.23 +
   serde + base64 0.22). Two patch deltas, both confirmed against the 0.1.8 source:
   - `SpeechConfig.language_code` (src/types/config.rs:56-60) — add
     `#[serde(skip_serializing_if = "Option::is_none")] pub language_code: Option<String>`; the struct is
     already `#[serde(rename_all = "camelCase")]` → serializes `languageCode`. Keep optional so existing
     callers compile; mirrors python-genai SpeechConfig (types.py:5317).
   - Reconnect policy (src/session.rs:71-87): add `pub reconnect_on_drop: bool` (default false); in the
     runner (session.rs:436-462) branch on the internal DisconnectReason (session.rs:408-414): GoAway →
     always reconnect (inject resume handle, which the crate already does at session.rs:634-644); ConnectionLost
     → reconnect only if `reconnect_on_drop`, else emit `ServerEvent::Closed{reason}` + set Closed, mirroring
     the Python caller (transport_dropped=True, no mid-call reconnect). The crate's default policy
     (auto-reconnect on both) is MORE aggressive than Python — do not ship it unpatched.
   - Surface close codes: patch src/transport.rs:311-324 so `RawFrame::Close` carries the code, enabling the
     §1.6 classification table. Do NOT rely on the crate for VAD/SpeechStarted — ServerEvent has no
     speech-started/ended variants; manual activity markers + TurnComplete/Interrupted is the contract
     (same as Python).
   - Setup JSON emitted by the vendored crate is camelCase (`realtimeInput`, `activityStart`); Python sends
     snake_case (`realtime_input`, `activityStart`); the server accepts both — field presence is what
     matters. Do not chase byte parity on key casing.
2. **OpenAI**: hand-roll on tokio-tungstenite (matches the Python side, which uses raw `websockets`).
   Protocol surface is ~10 event types. `oai-rt-rs 0.4.0` (tokio-tungstenite, GA-shaped ClientEvent/
   ServerEvent models incl. truncate + speech_started/stopped, inspected on disk) is the documented fallback
   if the event layer grows — protocol layer only, never LiveKit media.
3. **Audio**: mixer + AudioSource at 24 kHz for BOTH providers; Gemini output PCM (24 kHz) goes to the mixer
   un-resampled (Python does the same — the only resample is SDK-side on agent-audio input: 48k→16k for
   Gemini, 48k→24k for OpenAI).
4. **Timing constants** (already enumerated inline above — the ones that bite):
   `suppress` monotonic max-extension; script-hold grace default 20.0 s (extends); inject settle 0.15 s;
   verify window 2.8 s; drain 8.0 s; STT tail 1.6 s; trailing pad 120 ms; silence 650 ms; receive timeout
   15 s (Gemini) / unbounded (OpenAI); handshake backoff `min(2*attempt, 6)` (2 s, 4 s); Gemini ws
   ping_interval 30 / ping_timeout 60 (without this the 20 s default kills calls ~4 min in with 1011);
   reconnect cap 2; truncate grace 200 ms; out-done fallback 6000 ms; sapi suppress pad +400 ms;
   audio_source_start once-per-utterance latch; room_pcm `duration_s = max(0.05, len(pcm)/2/rate)`; loop bed
   sleep `min(0.05, duration_s)`; dtmf digit gap 150 ms / `w` = 120 ms; script tick 50 ms; agent-reply
   budget 45.0 s; settle 900 ms; post-cue gap tolerance 1200 ms; hang-up drain
   `min(10.0, max(5.0, 1.2 + words*0.45))` + 0.55 s; `_wait_agent_idle` 6.0 s (speak) / 5.0 s (hang_up).
5. **Event/source names are report consumers' contract** (metrics.py counts `sim.script.cue`,
   `sim.script.hang_up`, `sim.script_inject`; web/markers.py groups by `sim.script_inject`; verify matches
   `spec.step_id` on cue kinds). Port names byte-for-byte as listed in §1.11, §2.7, §3.5, §4.9.
