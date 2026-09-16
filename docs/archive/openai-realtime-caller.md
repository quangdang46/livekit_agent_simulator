# Research — OpenAI Realtime as the sim caller (second provider)

**Goal:** add an OpenAI Realtime voice model as a caller provider alongside Gemini Live, with a design that does not hardcode either provider. This doc records the API facts needed to implement; a separate plan PR will seal the design.

**Date:** 2026-08-06. **Primary sources:** OpenAI developers docs (developers.openai.com/api/docs), openai-python GA types, openai-agents SDK source. **Verified in `.venv`:** `websockets 16.0` present (google-genai dep); **`openai` NOT installed**; `google-genai 2.11.0`.

---

## 1. Transport choice for this package

OpenAI Realtime connects via **WebRTC** (browser/mobile) or **WebSocket** (server media pipelines: Twilio, SIP, broadcast, media worker).

We are a server-side media pipeline — LiveKit gives us raw PCM tracks, we resample/mix in-process, and we already ship audio as PCM through `ParallelMicMixer`. **WebSocket is the correct transport.**

Two concrete options:

- **(A) Raw `websockets` (already in `.venv`, v16.0).** We send/receive JSON events, base64-encode PCM ourselves. Zero new deps. Matches the project's "generic core, verify against installed packages" rule. Same pattern the existing Gemini bridge uses (it talks to `google-genai` and drives `session.send_realtime_input`/`receive()`).
- **(B) `openai` SDK (not installed).** `client.realtime.connect()` exists in GA `openai>=1.78`; would add a heavy dependency and a JSON-typed event layer we do not need for a black-box caller.

**Recommendation: (A).** It is exactly the "server-side WebSocket" case the docs describe, adds no dependency, and keeps the provider code symmetric with Gemini (client class + typed PCM pump). The `openai` SDK's Realtime helpers (transcriptions, tool calling, truncation bookkeeping) are agent-side conveniences we do not use — the sim caller needs only audio-in, audio-out, transcripts, VAD events.

### Wire facts (WebSocket, server-to-server)

- Endpoint: `wss://api.openai.com/v1/realtime?model=<model>`.
- Auth: `Authorization: Bearer <OPENAI_API_KEY>` header. No `OpenAI-Beta` header needed for GA interface (was `realtime=v1` in beta).
- All messages are JSON text frames; events are `{ "type": "...", ... }`.
- Connect → server sends `session.created` → client sends `session.update` (instructions, audio formats, voice, turn_detection, output_modalities) → server echoes `session.updated`.
- Input audio: `session.input_audio_buffer.append` with base64 PCM16, 24 kHz mono, chunks ≤ 15 MB.
- Output audio: `response.output_audio.delta` with base64 PCM16 24 kHz mono (must be played by us — server does not know what we played). `response.audio_transcript.delta`/`.done` carry the spoken transcript.
- Session cap 60 min.

---

## 2. Session configuration (GA shape)

```json
{
  "type": "session.update",
  "session": {
    "type": "realtime",
    "model": "gpt-realtime-2.1",
    "instructions": "<persona system prompt>",
    "output_modalities": ["audio"],
    "audio": {
      "input": {
        "format": { "type": "audio/pcm", "rate": 24000 },
        "transcription": { "model": "gpt-4o-mini-transcribe" },
        "turn_detection": { "type": "semantic_vad", "eagerness": "medium" }
      },
      "output": {
        "format": { "type": "audio/pcm", "rate": 24000 },
        "voice": "marin"
      }
    }
  }
}
```

Key notes:
- **Input format is fixed at 24 kHz PCM16 mono** (unlike Gemini's 16 kHz). The agent→model pump must resample 48 kHz→24 kHz. LiveKit's `rtc.AudioStream(track, sample_rate=24000)` resamples for us — same pattern as the Gemini 16 kHz path.
- **Output audio is PCM16 24 kHz** → matches the LiveKit `AudioSource(24000, 1)` sample rate the mixer already uses. **No resample on output** (Gemini output is 24 kHz too, so the whole audio-out path is shared).
- `output_modalities: ["audio"]` locks to audio. (Gemini needs AUDIO-only for the same reason — TEXT mode closes the socket.)
- **No built-in "output transcription" flag** — the assistant's own transcript comes from `response.audio_transcript.delta` events automatically. `audio.input.transcription` enables caller-side ("input") transcription of what the model hears — this is our agent transcript mirror.
- `turn_detection`:
  - `server_vad` — silence-based chunking; params `threshold`, `prefix_padding_ms`, `silence_duration_ms`, `create_response`, `interrupt_response`.
  - `semantic_vad` — semantic end-of-utterance; params `eagerness: low|medium|high|auto`, `create_response`, `interrupt_response`.
  - `null` — VAD off; client owns `input_audio_buffer.commit` + `response.create` (push-to-talk style; like Gemini manual activity markers).
- Voices (GA): `alloy, ash, ballad, coral, echo, sage, shimmer, verse, marin, cedar`. **Voice cannot change after first audio response** in a session.
- 60-minute session cap; also note `max_response_output_tokens`, `temperature`, `reasoning.effort` (Realtime 2 adds reasoning — set `low` for latency).

---

## 3. Event stream (what the bridge consumes)

### Input side (caller-side transcript + VAD)

| Event | Meaning |
|---|---|
| `session.created` / `session.updated` | Session ready / config applied |
| `input_audio_buffer.speech_started` | User (agent audio we bridged) started speaking — server **cancels any in-flight response** (`response.cancelled`) |
| `input_audio_buffer.speech_stopped` | End of user speech |
| `input_audio_buffer.committed` | Audio buffer committed as a user item; input transcription kicked off |
| `conversation.item.input_audio_transcription.completed` | Final transcript of what the model heard (agent speech) |
| `input_audio_buffer.speech_stopped` + `conversation.item.created` | Server-side turn lifecycle under VAD |

**This is the critical parallel to Gemini:** Gemini's manual VAD (`activity_start`/`activity_end` + speech-gated PCM) exists because auto VAD never committed agent turns for freestyle. OpenAI Realtime has **server VAD built in**, which chunks *our* bridged agent audio as "user speech" and auto-creates responses. So the OpenAI provider can likely use **server VAD instead of a manual activity-marker gate** — a simpler `_pump_agent_audio` (feed PCM, no activity bookkeeping) unless we need manual turn control for Script inject / freestyle nudges (then `turn_detection: null` + explicit commit).

### Output side (model speech + transcript + interruption)

| Event | Meaning |
|---|---|
| `response.output_audio.delta` | base64 PCM16 24 kHz — play into mixer |
| `response.audio_transcript.delta` | Partial transcript of model speech (our caller transcript, `final=False`) |
| `response.audio_transcript.done` | Final transcript for the response item |
| `response.output_item.done` / `response.done` | Turn complete — `response.done` has usage + full output items |
| `response.cancelled` | Fired when VAD detects user speech during playback (barge-in) — **the interruption signal** |
| `error` | Server error; may include `type: "invalid_request_error"` / connection close |

### Barge / interruption semantics (WebSocket — important)

On **WebSocket** the server does **not** know what we played. Interruption works like this (confirmed by docs + OpenAI support thread):
1. We see `input_audio_buffer.speech_started` → the server has already cancelled the in-flight response (`response.cancelled`).
2. We must **stop local playback** (clear the mixer speech queue) at the point we believe the agent audio started.
3. Optionally send `conversation.item.truncate` `{item_id, content_index: 0, audio_end_ms}` to remove the unplayed tail from the model's context.

The Gemini provider does this differently: it gets `server_content.interrupted` (server knows playback) and does not need `conversation.item.truncate`. **The provider abstraction must give the shared policy one interruption event shape** — e.g. a normalized `"caller-interruption"` callback — so `end_call`, Script barge asserts, and `interrupt_rate` metrics stay provider-agnostic.

---

## 4. Making it speak a Script line / freestyle nudge (text into the conversation)

OpenAI Realtime supports text injection without the role-flip problem Gemini has:

- **`conversation.item.create`** with `item.type: "message"`, `role: "user"`, `content: [{ type: "input_text", text: ... }]`, then **`response.create`** — the model speaks the text as the user (caller). This is a real "say this line" primitive.
- Gemini's `script_speak_directive` / `send_realtime_input(text=...)` is the fragile equivalent that needs role-lock phrasing. OpenAI's is a first-class, exact primitive.

This maps cleanly onto:
- `inject_cue(delivery="gemini_text")` → OpenAI `conversation.item.create(role=user) + response.create`. The delivery kind name ("gemini_text") should be renamed to something provider-neutral (e.g. `model_text` or keep as a generic "text" delivery) since both providers support it.
- Freestyle nudge → OpenAI `response.create` after `input_audio_buffer.speech_stopped`/committed, or a `conversation.item.create` user nudge. Gemini needs `nudge_freestyle_answer` via `activity_end`. Different mechanics, same policy intent ("ask the caller to reply").
- `_emit_bootstrap_cues` (bootstrap midcall text) → `conversation.item.create` role=user.

---

## 5. Provider differences that force a seam

| Concern | Gemini Live | OpenAI Realtime |
|---|---|---|
| SDK | `google-genai` (`genai.Client`, `client.aio.live`) | raw `websockets` (or `openai` SDK) |
| Input rate | 16 kHz PCM | 24 kHz PCM |
| Output rate | 24 kHz PCM | 24 kHz PCM |
| Connect | `connect()` returns async CM | `websockets.connect()` |
| VAD / turn commit | Manual activity markers (auto VAD unreliable) | Built-in server/semantic VAD (or `null` for manual) |
| Say-a-line | `send_realtime_input(text=...)` + role-lock prose | `conversation.item.create(role=user)` + `response.create` |
| Interruption | `server_content.interrupted` | `input_audio_buffer.speech_started` → `response.cancelled` + client truncate |
| Transcripts | `input_audio_transcription` / `output_audio_transcription` in server_content | `conversation.item.input_audio_transcription.completed` / `response.audio_transcript.delta` |
| Reconnect | None built-in; retry handshake only (we handle 1006) | Same class of problem — socket drop needs the same handling |

Shared (already provider-neutral in this repo): `end_call.py` token/farewell heuristics, `ParallelMicMixer`, `LocalConversationRecorder`, observer, ScriptRunner contract, `CallerPolicy` text.

---

## 6. Design implications (for the plan PR)

1. **`provider` field** on `SimulatorConfig` (e.g. `gemini` default / `openai`), mirroring the existing `judge.endpoint_type` precedent (openai/anthropic switch). `voice.model`, `voice.voice`, and per-provider API key (`google_api_key` vs `openai_api_key`) select the provider implementation.
2. **A `CallerBridge` Protocol** owning the contract ScriptRunner / nudge / orchestrator already consume (all currently duck-typed via `hasattr` on `GeminiCallerBridge`): `run`, `stop`, `end_call`, `inject_cue`, `nudge_freestyle_answer`, `inject_reground`, `suppress_persona_output`, `begin_scripted_user_silence`, `scripted_silence_active`, `begin/end_script_hangup_farewell`, `drain_persona_speech`, `sim_hang_up`, `bind_script_pending`, `publish_mic`, `watch_agent_tracks*`, `transport_dropped`. Shared code can stay duck-typed or be tightened to the Protocol.
3. **`gemini/` becomes `callers/` (or `sim_callers/`)** with `base.py` (shared audio/mixer/end_call plumbing) + `gemini.py` + `openai.py`; factory `build_caller_bridge(cfg, ...)` selects by `cfg.simulator.provider`. Keeps the "Strategy + factory" pattern already used by SimLeg (`sim_leg/`) and CallerPolicy.
4. **Do not regress Gemini fidelity** — 60k-line-worth of `live_session.py` behavior (transport drop, activity markers, role-flip suppression, quiet-caller gain) stays as the Gemini implementation. The seam is a boundary, not a rewrite.
5. **OpenAI provider specifics** (first implementation):
   - `pump_agent_audio`: `AudioStream(track, sample_rate=24000)`, base64-append; rely on server VAD by default.
   - `pump_model_events`: `response.output_audio.delta` → `_play_pcm`; `response.audio_transcript.delta/done` → observer transcripts; `input_audio_buffer.speech_started` → normalize to interruption + clear mixer speech; `response.done` → turn_complete equivalent (end_speech_turn).
   - `inject_cue(text)` → `conversation.item.create(role=user, input_text)` + `response.create` (drop the role-flip prose of `script_speak_directive`).
   - Map `end_call` token/farewell heuristics identically to Gemini (shared `end_call.py`).
   - Reconnect: reuse the handshake-retry pattern (no SDK reconnect either side).

## 7. Open questions for the plan PR

- Should `turn_detection` for the OpenAI caller be **server VAD** (simplest, matches "natural caller") or **null + manual commit** (control parity with Gemini's activity markers, needed if Script must own precise barge timing)? Recommend server VAD first; manual commit is the fallback knob.
- `delivery: "gemini_text"` in Script/JSONL is Gemini-named. Rename to a neutral `model_text` (alias `gemini_text` kept only during migration, then removed per AGENTS.md "no legacy shims") — or leave and map in the factory.
- `openai_api_key` optional-add: should OpenAI provider reuse the existing `simulator.google_api_key` block with a `provider` switch, or get its own `simulator.openai.api_key` nested block? (Precedent: `judge.base_url`/`api_key` is its own block.)
- Whether to install the `openai` SDK at all, or ship raw `websockets` (recommended: raw).
- Should `nudge_freestyle_answer`'s `agent_hint` arg become meaningful for OpenAI (e.g. send a real user turn) or stay a no-op like Gemini's? (Stays policy-owned text.)
