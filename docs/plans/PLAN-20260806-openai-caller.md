# Plan — OpenAI Realtime caller provider (second sim voice)

## Summary (read this first)
- **You asked:** Research implementing an OpenAI Realtime model caller like Gemini; with two providers (Gemini + OpenAI) design a sound pattern. Research is done → `docs/openai-realtime-caller.md`; this plan seals the implementation.
- **What is going on:** Today `GeminiCallerBridge` (60 kB `gemini/live_session.py`) is the only caller brain and is imported by name from `run_orchestrator.py`, `script/runtime.py`, `caller_nudge.py`, `interrupt_rate.py`. Its contract is already duck-typed (`hasattr(bridge, ...)`). The seam for a second provider is missing; config has no `provider` field.
- **We recommend:** Introduce a **`CallerBridge` Protocol + provider factory + shared base** (Strategy/factory, same as `SimLeg`/`CallerPolicy`), keep `GeminiCallerBridge` behavior untouched, add **`OpenAICallerBridge`** using **raw `websockets` (already in `.venv`, no new dep)** with **built-in server VAD**. The switch is **flat at `simulator` level**: `simulator.provider: "google" | "openai"` + `simulator.mode: "realtime"` + a **single generic `simulator.api_key`** (only the active provider's key lives there — one brain per run, one key; AGENTS.md "one clear API, no aliases"). `voice:` becomes a provider-neutral bag (`model`, `voice`, `language`). This is a **clean break (pre-1.0)**: the existing target config renames `google_api_key` → `api_key`, moves `voice.provider`/`voice.mode` up to `simulator.*`, and drops `simulator.language` (voice.language is authoritative). No `google_api_key` alias retained — core `_require` fails fast with a clear message. `gemini/` moves under a `callers/` package; `callers/base.py` owns shared audio/end_call/watch plumbing.
- **Status:** **Implemented** (2026-08-06): config `provider`/`mode`/`api_key`, `callers/` package (Protocol + factory + shared base), `OpenAICallerBridge` (raw `websockets` + semantic VAD), 17 new unit tests, target config migrated. Full suite `468 passed`.
  - **Real Gemini smoke: PASS.** Live `lks execute vi-extraction-happy-path` against `voice-ai-agent` produced a real report (`003-...-021053-7dba`): `sim.gemini_connected`, mic published, agent audio bridged, 2.5MB conversation.wav, and correct `transport_dropped` handling on a transient 1006 drop. The `callers/` refactor + `api_key` rename work live.
  - **Real OpenAI smoke: PASS.** With a valid key, live `lks execute` ran (`006-...-021909-d47e`): `sim.openai_connected` (gpt-realtime-2.1 + marin), caller bootstrap spoke first, **caller produced a real Vietnamese opening** ("Chào anh chị, tôi là Trần Văn An…") via the OpenAI model, agent audio was bridged in (`sim.agent_audio_bridged`). The run ended `dead_call_silence` because the **agent under test** went silent (22s threshold) — not a bridge failure. Two real bugs were found + fixed by the live smoke:
    1. `session.audio.output.language` is **not a valid GA param** (server: `Unknown parameter`) — removed; caller language comes via the persona system prompt.
    2. OpenAI needs an explicit **`response.create`** after a text-only bootstrap/reground item — added to `_emit_bootstrap_cues` / `inject_reground`.
  - **Full suite: 470 passed** (includes regression tests for both fixes).

---

## Non-goals (do not do in this PR)

1. **No provider code.** No `OpenAICallerBridge`, no factory, no config `provider` field, no `callers/` rename yet — this PR is design + research only (AGENTS.md: research → plan → implement).
2. **No `openai` SDK dependency.** Ship with raw `websockets` (already a transitive dep via `google-genai`, v16.0 in `.venv`). Verified: `import openai` fails today; `websockets 16.0` imports fine.
3. **No Gemini fidelity regression.** `gemini/live_session.py` behavior (manual activity markers, transport-drop retry, role-flip suppression, quiet-caller voice gain, `script_speak_directive`) stays as-is; the seam is a boundary, not a rewrite.
4. **No stubborn / fit-to-one-repo patches.** No consumer keys in `src/`; no scenario-specific hacks to paper over provider gaps.
5. **No multi-feature PR.** One gap = one PR (WIP PR process). This is the research/design bead; the provider bead is separate.
6. **Do not change Script/JSONL `delivery` semantics** in this PR. (Naming cleanup of `gemini_text` → `model_text` is a **later** migration, tracked as an open question, not done now.)

---

## Invariants (must hold after implement, in the follow-up PR)

| ID | Invariant |
|----|-----------|
| I1 | One config field selects the caller brain: `simulator.provider` ∈ `{google, openai}` (default `google`), and `simulator.mode` ∈ `{realtime}` today (reserved for a future cascade/TTS brain). One generic `simulator.api_key` holds the active provider's key. This is a documented pre-1.0 breaking change (see Migration in Feature planning); target configs are migrated in the same PR. |
| I2 | `GeminiCallerBridge` runs bit-for-bit as today when `provider: gemini` — same events, same transcripts, same reconnect/transport-drop semantics. |
| I3 | The bridge contract consumed by `ScriptRunner` / `nudge_caller_after_agent_greeting` / `InterruptRateRunner` / `_conversation_loop` stays satisfied (either via the Protocol or unchanged `hasattr` duck-typing). |
| I4 | `end_call.py` token/farewell heuristics remain provider-agnostic and shared. |
| I5 | Audio-out path is shared: OpenAI output PCM is 24 kHz mono (same as Gemini) → `ParallelMicMixer`, `AudioSource(24000,1)`, `LocalConversationRecorder`, room_pcm cues all unchanged. |
| I6 | Interruption is normalized to one shared shape so `interruption` events, Script barge asserts, and `interrupt_rate` metrics stay provider-agnostic. |
| I7 | Provider selection never lives in scenario `Caller.mode` (mode changes SimLeg only — design lock #5). `simulator.provider` is a simulator capability, not a call topology. `simulator.mode` is distinct from scenario `Caller.mode`: `simulator.mode` picks the sim-brain family (realtime now, cascade later), scenario `Caller.mode` picks the SimLeg topology (`webrtc_sim`/`inbound_sip`/…). |
| I8 | Portable defaults stay (`en-US`/`UTC`); `provider` default `gemini` keeps every existing `.agent-sim/` working. |

---

## Feature planning

### Recommended approach (sealed)

**Phase 1 — this PR (design only):**
- Add `docs/openai-realtime-caller.md` (already written) + this plan.
- Optional: add `docs/plans/` note that `callers/` rename is the agreed destination so a future PR is consistent.

**Phase 2 — follow-up PR (provider seam + OpenAI brain):**

1. **Config (flat `simulator` brain block):** add `SimulatorConfig.provider: Literal["google","openai"] = "google"`, `SimulatorConfig.mode: Literal["realtime"] = "realtime"` (both validated fail-fast on unknown values), and rename `google_api_key` → **`api_key`** (single generic key; the active provider uses it). `SimulatorVoiceConfig` stays a provider-neutral bag (`model`, `voice`, `language`) and becomes the **only** language source (drop `simulator.language`). `config_snapshot` redacts as today (no secrets) and adds `provider`/`mode` to the `simulator` snapshot.

   **Migration table (targets in the same PR):**
   | Today | New | Note |
   |---|---|---|
   | `simulator.google_api_key` | `simulator.api_key` | `_require` fails fast with a clear message |
   | `voice.provider: google` | `simulator.provider: google` | moved up |
   | `voice.mode: realtime` | `simulator.mode: realtime` | moved up |
   | `simulator.language: "en-US"` | *(removed)* | `voice.language` is authoritative |
   | `simulator.voice.*` | unchanged | `model`/`voice`/`language`
2. **`callers/` package** (renames `gemini/`):
   - `callers/base.py` — shared plumbing: `ParallelMicMixer` wiring, `publish_mic`, `watch_agent_tracks*`, `end_call.py` helpers, `_play_pcm`/`_drain_persona_speech`, `suppress_persona_output`/`begin_scripted_user_silence`/`scripted_silence_active`, `sim_hang_up`, `bind_script_pending`, `transport_dropped`, `end_call` event. Public interface = `CallerBridge` Protocol.
   - `callers/gemini.py` — the current `GeminiCallerBridge` body (kept verbatim where possible; only the shared bits move to base). **No Gemini behavior change.**
   - `callers/openai.py` — `OpenAICallerBridge` (below).
   - `callers/factory.py` — `build_caller_bridge(cfg, ...) -> CallerBridge` selecting by `cfg.simulator.provider` (`google` → Gemini bridge, `openai` → OpenAI bridge).
   - Keep `gemini/` as a thin re-export shim **only if** `run_orchestrator.py`/tests still import it; per AGENTS.md "no legacy shims", prefer updating all importers in the same PR (they are few: `run_orchestrator.py`, `script/runtime.py`, `caller_nudge.py`, `interrupt_rate.py`, `tests/test_gemini_reconnect.py`).
3. **`OpenAICallerBridge`** (raw `websockets`):
   - Connect: `wss://api.openai.com/v1/realtime?model=<voice.model>`, `Authorization: Bearer <cfg.simulator.api_key>`, then `session.update` with `output_modalities:["audio"]`, `audio.input.format {audio/pcm, 24000}`, `audio.input.transcription {model: gpt-4o-mini-transcribe}`, `audio.input.turn_detection {type: semantic_vad, eagerness: medium}`, `audio.output {audio/pcm 24000, voice}`, `instructions=<persona system prompt>`.
   - Agent→model: `rtc.AudioStream(track, sample_rate=24000)`, base64 → `session.input_audio_buffer.append` (server VAD chunks + auto-creates responses). Reuse the existing speech-gate only if manual commit is later needed.
   - Model→room: `response.output_audio.delta` (base64 PCM) → `_play_pcm`; `response.audio_transcript.delta/done` → observer transcripts (`source="sim.openai"`); `response.done` → `mixer.end_speech_turn()`.
   - Interruption: `input_audio_buffer.speech_started` → `mixer.clear_speech()` + normalized `interruption` event (same spec as Gemini's `sc.interrupted` path); optionally `conversation.item.truncate` after we track played audio.
   - Inject say-a-line: `conversation.item.create(role=user, content=[{type:input_text, text:script_speak_directive(...)}])` + `response.create`. (Drop the role-lock prose for the OpenAI path — `conversation.item.create` is an exact user-turn primitive; Gemini keeps its prose.)
   - End call: reuse `end_call.py` heuristics on `response.audio_transcript.done` output, identical gates as Gemini (defer while Script pending, mute on bye).
   - Reconnect: same handshake-retry + `transport_dropped` semantics as Gemini (no SDK reconnect either side).
4. **Tests** (`tests/test_openai_realtime.py`): unit-test the event→action mapping with a fake `websockets` (message queue): session.update shape, output audio → mixer, speech_started → interruption event + clear, transcript → observer, conversation.item.create on inject, end_call heuristics. Keep `test_gemini_reconnect.py` green (Gemini untouched). No real-API test (CI has no OpenAI key).

### Prior art / research (see `docs/openai-realtime-caller.md`)
| Source | What we reuse | What we avoid |
|--------|---------------|---------------|
| OpenAI Realtime WebSocket guide | server→server transport, `session.update`, base64 PCM 24 kHz, `response.output_audio.delta`, VAD events | WebRTC path (browser) and `openai` SDK helpers |
| OpenAI Realtime conversations | `conversation.item.create` role=user + `response.create` as exact say-line primitive | Gemini role-lock prose for the OpenAI path |
| OpenAI VAD guide | `server_vad`/`semantic_vad` params; `input_audio_buffer.speech_started` semantics | Manual activity-marker gate (Gemini-specific) |
| OpenAI WebSocket interruption support thread | WebSocket server does not know played audio → client must truncate; `speech_started` is the signal | Assuming `audio_interrupted` fires on WS (it does not) |
| Existing repo | `CallerPolicy` Strategy, SimLeg factory, `judge.endpoint_type` precedent, `end_call.py`, `ParallelMicMixer`, `LocalConversationRecorder` | Forcing both providers through one code path |

---

## Risks / mitigations

| Risk | Mitigation |
|------|-----------|
| Refactor `gemini/`→`callers/` breaks Gemini fidelity | Keep Gemini body verbatim in `callers/gemini.py`; only shared plumbing moves to base; `test_gemini_reconnect.py` + full pytest must stay green; run a real `lks execute` smoke on a target before merging |
| WebSocket server can't know what audio we played → transcript vs audio skew on barge | Normalize interruption to the same event the Gemini path emits; track played-audio for `conversation.item.truncate` as a follow-up knob; document skew (same as community-reported) |
| Server VAD may auto-respond to noise/agent re-prompts (talkativeness) | `semantic_vad` + `eagerness` tuning; fallback knob `turn_detection: null` + manual commit parity with Gemini activity markers |
| OpenAI voice list differs from Gemini's `Puck` | `voice.voice` is config; map/validate per provider; invalid voice fails fast at connect with a clear error |
| Single generic key | One `simulator.api_key` (renamed from `google_api_key`); preflight warns when key missing/short for the active `simulator.provider` (extend existing `preflight.py` google-key check) |
| Scope creep (delivery rename, SDK install) | Explicit non-goals; `gemini_text`→`model_text` tracked as a later migration |

---

## Deliverables / acceptance

| Gate | Required |
|------|----------|
| This PR | `docs/openai-realtime-caller.md` + `docs/plans/PLAN-20260806-openai-caller.md`; `uv run pytest -q` green (docs-only, but verify no test imports break) |
| Follow-up PR | `callers/` package + `OpenAICallerBridge` + factory + config `simulator.provider`/`mode`/`api_key` (+ target migration) + tests; `uv run pytest -q` green; **real** `lks execute` smoke with `simulator.provider: google` (regression) and `simulator.provider: openai` (new) |
| Portable core | No consumer keys in `src/`; provider is a simulator capability, never a scenario-mode override |

---

## Open questions (tracked, not blocking this design PR)

1. `delivery: "gemini_text"` naming → migrate to neutral `model_text` in a later PR (AGENTS.md: remove legacy shim after migration).
2. Whether `nudge_freestyle_answer(agent_hint)` should become a real user turn for OpenAI (send `conversation.item.create` nudge) or stay a no-op like Gemini's `activity_end`-only nudge. Recommend: keep no-op first; wire later if OpenAI freestyle stalls.
3. Default `eagerness` for `semantic_vad` (medium) vs `server_vad` (silence-based) — smoke both; keep whichever yields the most natural caller turn-taking in real runs.
4. `conversation.item.truncate` on barge: ship in v1 of the OpenAI bridge or defer? Recommend ship a best-effort `truncate` using mixer-played bytes, since it directly improves agent-side memory on barge.
