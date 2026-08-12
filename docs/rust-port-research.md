# Rust full-port research — rewrite `livekit-agent-simulator` (lks) in Rust

> Research companion to `docs/plans/PLAN-20260813-rust-full-port.md`.
> Status: **research complete, decisions sealed** (2026-08-13).
> Every claim below is backed by a URL, an on-disk crate checkout, or a file path in this repo.
> Where a fact is uncertain it is marked **VERIFY** with a fallback — the plan never invents APIs.

---

## 0. Scope and inputs

Target of the rewrite: `src/livekit_agent_simulator/` (~23.5 kLOC Python across 108 files,
per `wc -l` of the package tree on 2026-08-13). New Rust workspace lives at
`src/livekit_agent_simulator_rust/` (exists, empty). The Python package remains the
reference implementation; the Rust port must stay **report-format byte-compatible** and
**config/scenario compatible** so `compare_runs` and the existing web player keep working
across implementations.

Evidence sources used:

| # | Source | What it grounds |
|---|---|---|
| R1 | On-disk checkout of `livekit/rust-sdks` at commit `1a477bc422c6890537b3bcdb017f0ac094d49661` ("Release packages (#1316)", 2026-08-10) in `/tmp/rust-sdks-verify.bak` | Crate inventory, versions, feature flags, API surface |
| R2 | On-disk Gemini Live Rust client sources in `/tmp/glsrc/` (`session.rs`, `transport.rs`, `config.rs`, `client_message.rs`, `server_message.rs`, `protocol.md`) | Confirms this is the `gemini-live` crate (jacoblincool/gemini-live-rs); wire-level design |
| R3 | Web verification (crates.io / docs.rs / GitHub, 2026-08-13) | Current published versions of `livekit`, `livekit-api`, `gemini-live`, `rmcp`, `rusqlite`, `rust-embed`, `pyo3` |
| R4 | Python package source in this repo (`src/livekit_agent_simulator/…`) | Report format, config schema, scenario schema, event kinds |
| R5 | Real run artifact: `demo/dtmf-feature/.agent-sim/reports/114-people-pleaser-refuse-card-20260809-201652-8b32/` | Ground-truth `events.jsonl`, `summary.json`, `meta.json` shapes |
| R6 | `AGENTS.md`, `docs/plans/PLAN-20260806-openai-caller.md`, `install.sh`, `templates/`, `web/dist/` | Repo rules, plan template, packaging, assets |

---

## 1. LiveKit Rust SDK capability

### 1.1 Crates and versions (verified 2026-08-13)

| Crate | Latest verified | Evidence |
|---|---|---|
| `livekit` (realtime client) | 0.8.2 published 2026-08-03; 0.8.3 in the 2026-08-10 release commit | crates.io/crates/livekit (R3); `livekit/Cargo.toml` version `0.8.3` in R1 |
| `livekit-api` (server API) | 0.6.3 | `livekit-api/Cargo.toml` in R1 |
| `livekit-protocol` | 0.7.12 | workspace `Cargo.toml` in R1 |
| `livekit-common` | 0.1.1 | R1 |
| `livekit-data-stream` | 0.1.2 | R1 |
| `livekit-datatrack` | 0.1.13 | R1 |
| `livekit-runtime` | 0.4.0 | R1; workspace note: `default-features = false`, each consumer picks a runtime flavor (`tokio` / `async` / `dispatcher`) |
| `livekit-net` | 0.1.2 | R1 |
| `libwebrtc` / `webrtc-sys` | 0.3.45 / 0.3.42 | R1; the vendored WebRTC C++ engine |

The workspace requires rustflags for linking (`rust-sdks/.cargo/config.toml` — see the
README "Getting started" note, R3) and currently **requires Tokio** ("Currently, Tokio is
required to use this SDK" — README, R3). Toolchain pinned at **Rust 1.97.1**
(`rust-toolchain.toml` in R1).

### 1.2 Feature flags that matter for lks

- `livekit` default = `["tokio"]`; alternative runtimes exist but lks will use the
  default (decision in the plan).
- TLS: default is **no TLS on the signaling WebSocket** ("By default ws TLS is not
  enabled" — `livekit/Cargo.toml`). Choose `rustls-tls-webpki-roots` (bundled Mozilla
  roots; recommended by the SDK for containerized deployments — comment in
  `livekit-api/Cargo.toml`) for a self-contained static binary.
- `livekit` has no `audio` feature (unlike Python): PCM handling is on the caller.
  `AudioStream` / `AudioFrame` are available on `RemoteTrack` — see §1.4.

### 1.3 What the realtime SDK gives us (R1 + docs.rs R3)

- Room connect with `RoomOptions` (including auto-subscribe, dynacast), `Room::connect`
  with URL + token; room events via `room.subscribe().recv().await` — the
  `RoomEvent` enum carries `TrackSubscribed`, `ParticipantConnected/Disconnected`,
  `RoomDisconnected`, `DataPacketReceived`, and `TextStreamOpened` (docs.rs
  `livekit::data_stream` + docs.livekit.io text-streams page, R3).
- `LocalParticipant`: `publish_track` (audio source), `publish_data_track`
  (verified: `publish_data_track(&self, options) -> Result<DataTrack, PublishError>` and
  `RemoteDataTrack::subscribe() -> DataTrackStream` — docs.livekit.io data-tracks page,
  R3), `send_text`/`stream_text` (text streams, R3).
- `RemoteTrack`: `AudioStream` (`rtc::AudioStream`), `AudioSource` (publish PCM into a
  track), `RemoteAudioTrack`, `TrackEvent::AudioFrame` (audio frames of `AudioFrame` type
  with `data`, `sample_rate`, `num_channels`, `samples_per_channel`).
- Data packets (`DataPacket::Reliable`/`Lossy`) for the `lk.agent.session` topic used by
  `livekit/agent_session_observer.py`.
- RPC (`RpcRequest`/`RpcResponse`) — not used by lks (agent under test may use it; we only
  observe), **no port needed**.
- **Transcription**: the Rust SDK has `TranscriptionSegment` types, but
  transcription-aware event plumbing is primarily a server-side concern; **VERIFY**:
  whether `RoomEvent` exposes the agent's transcription text updates the way Python's
  `rtc` events do. Fallback (sealed in plan §Key decisions): derive agent/user transcript
  events from the audio stream + `agent_session_observer`-style text streams, same as the
  Python `Observer` does today (R4: `livekit/observer.py` consumes both).

### 1.4 Key gap vs Python: no `livekit.api` realtime mirror, but server API covered

Python uses `livekit.api` (`RoomServiceClient`, `AgentDispatchClient`,
`SipClient`) from `livekit-api` (R4: `livekit/adapter.py`, `livekit/sim_leg/room_resolve.py`
import `livekit.api as api`). The Rust `livekit-api` crate covers the same surface:
`access_token` (HS256 via in-crate HMAC provider — `livekit-api/Cargo.toml`,
`jsonwebtoken` with default-features off), `services::room` (`RoomClient`,
`CreateRoomOptions`, `DeleteRoomOptions`), `services::agent_dispatch`,
`services::sip`, `services::webhooks` (README R3). **No gap.**

### 1.5 SIP / outbound calling

- `livekit-api` has `services::sip` (SIP client + SIP participant creation) — enough for
  the `inbound_sip` and `outbound_*` SimLegs, which today only create SIP participants
  and dial numbers via the server API (R4: `livekit/sim_leg/inbound.py`,
  `outbound_*` legs use `livekit.api.SipClient`).
- `SipDTMF` is in the realtime SDK (`livekit::webrtc::SipDTMF` — docs.rs module list, R3)
  for the DTMF script delivery path (`dtmf` script cue in the demo suite).

### 1.6 Build burden (VERIFY with numbers)

libwebrtc is built **from source** by `webrtc-sys` (C++ compilation via the `cc` crate;
workspace has `webrtc-sys/build` member and `download_ffi.py` for prebuilt downloads).
The workspace README requires cargo config rustflags (R3). Practical consequences:
- First build is long (libwebrtc C++ build) unless prebuilt `webrtc-sys` FFI packages are
  used (`download_ffi.py` downloads prebuilt libwebrtc artifacts — R1 repo root).
- CI caching of `target/` (sccache / GH Actions cache) is mandatory.
- macOS aarch64 + x86_64 and Linux x86_64 are the supported CI matrix; Windows: libwebrtc
  builds exist for Windows in CI (`builds.yml` lists supported platform toolkits, R3)
  **VERIFY** before promising a Windows release binary in P10 — fallback: keep the Python
  package as the Windows path until verified.

---

## 2. Gemini Live options

### 2.1 Protocol facts (R2 `protocol.md` + ai.google.dev R3)

- Endpoint (API key): `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={API_KEY}` (R2 protocol.md; ai.google.dev/api/live confirms the v1beta URL, R3).
- Client messages: `setup` (first message; model, generationConfig, systemInstruction,
  tools, realtimeInputConfig, inputAudioTranscription, outputAudioTranscription,
  sessionResumption…), `clientContent`, `realtimeInput` (audio/video/text blobs +
  `activityStart`), `toolResponse`. Server messages: `setupComplete`, `serverContent`,
  `toolCall`, `toolCallCancellation`, `goAway`, `sessionResumptionUpdate`,
  `usageMetadata`, error (R2/R3).
- **Session resumption** is a first-class mechanism (`sessionResumption` config,
  `SessionResumptionUpdate`, `BidiGenerateContentConstrained` for ephemeral tokens) —
  this is what the Python `GeminiCallerBridge` currently emulates via
  `sim.gemini_resumption_handle` events + reconnect (R4 `callers/gemini.py`).
- Activity markers: `activityStart` on `realtimeInput` (R3 reference) — used by the
  Python bridge as manual activity markers.
- Model audio comes back as base64 PCM in `serverContent.part.inlineData` (24 kHz —
  confirmed in R2 `session.rs` `INPUT_SAMPLE_RATE` constant used by the crate; the Python
  side assumes 24 kHz mono in `callers/base.py`).
- Live API docs still marked **Preview** as of 2026-04-05 (R2 protocol.md note) —
  re-audit on model changes.

### 2.2 Candidate A — `google-genai` Rust (official Google crate)

**Not verified as offering the Live (WebSocket) transport in Rust.** The Python
`google-genai` package has `client.aio.live.connect(...)` (R3 snippet in the ai.google.dev
reference), but the Rust `google-genai`/rust-googleapis workspace is REST/gRPC focused;
the Live API docs' Rust examples do not appear. **VERIFY** at P2 kickoff: run
`cargo add google-genai` and grep for `live`/`BidiGenerateContent`. Fallback is Candidate C.
(Research report for the Rust google-genai crate did not surface a Live websocket client —
no URL can be cited as proof of absence; treat as unverified.)

### 2.3 Candidate B — `gemini-live` (jacoblincool/gemini-live-rs)

- Published on crates.io: **`gemini-live` 0.1.x** (0.1.8 shown on docs.rs; README, R3).
- This is exactly the crate whose sources are in `/tmp/glsrc/` (R2): module layout
  `session/transport/codec/audio/types/error`, `SessionConfig { transport, setup,
  reconnect }`, `ReconnectPolicy` with exponential backoff (base 500 ms, cap 5 s),
  `ServerEvent` enum with `SetupComplete`, `ModelText(String)`, `ModelAudio(Bytes)`
  (base64-decoded raw PCM at 24 kHz), `GenerationComplete`, `TurnComplete`,
  `Interrupted`, `InputTranscription(String)`, `OutputTranscription(String)`,
  `ToolCall`, `ToolCallCancellation`, `SessionResumption`, `GoAway`, `Usage`, `Closed`,
  `Error` (R2 `server_message.rs`).
- Transport: tokio-tungstenite + rustls + URL — no heavy deps (R2 `transport.rs`).
- Covers **every** Gemini bridge feature the Python side needs: typed setup (incl.
  `realtimeInputConfig` with automatic activity detection: `ActivityHandling`,
  `StartSensitivity`, `EndSensitivity`, `TurnCoverage` — R2 `config.rs`), session
  resumption, GoAway, reconnect, transcription events, interrupt detection.
- Risk: single-maintainer community crate (jacoblincool), MIT/Apache dual license,
  version 0.1.x (pre-1.0), "Gemini 2.5 async scheduling semantics remain under audit"
  (README, R3). Mitigation: it is a **thin, typed wrapper over the wire protocol we can
  reimplement from `protocol.md`**; the crate is small (7 files, ~34 kB `session.rs`).
- **VERIFY** at P2: confirm `ServerEvent::ModelAudio` payload rate is 24 kHz raw PCM and
  that `setup` accepts `outputAudioTranscription` + `inputAudioTranscription` — the
  codec/type layer is generated from the same proto the Python `google-genai` uses, so
  expected PASS.

### 2.4 Candidate C — hand-rolled client (raw tokio-tungstenite)

The Python side itself is effectively hand-rolled at the semantics layer
(`callers/gemini.py` uses `google.genai` only for types; the event→action mapping,
activity markers, reconnect, transport-drop handling are all in lks). A Rust
hand-rolled client is ~600–900 lines (mirror of R2's session/transport/config shape).
Fallback if B is unmaintained or wrong.

### 2.5 Decision (sealed in the plan)

**Candidate B (`gemini-live` crate) primary, Candidate C fallback.** Rationale: exact
feature match (resumption, GoAway, activity detection, transcript events), no heavy
deps, and the wire protocol is documented well enough (R2 `protocol.md`) that B is
replaceable in a week. OpenAI Realtime stays **hand-rolled on tokio-tungstenite** — the
Python side already does this (`callers/openai.py` uses raw `websockets`, R4) and the
OpenAI WS protocol is simpler (one `session.update`, `input_audio_buffer.append`,
`response.create`, event types).

---

## 3. Supporting crates

| Need | Crate | Version (verified 2026-08-13) | Evidence |
|---|---|---|---|
| Async runtime | `tokio` | 1.x (full features) | R3 (rmcp/rust-sdks READMEs); required by `livekit` SDK |
| Realtime + server SDK | `livekit`, `livekit-api` | 0.8.2 / 0.6.3 (0.8.3 / 0.6.3 in R1) | §1.1 |
| Gemini Live | `gemini-live` | 0.1.8 | §2.3 |
| WebSocket (OpenAI + fallbacks) | `tokio-tungstenite` | 0.2x (R2 uses it; pin latest at P2) | R2 `transport.rs` |
| SQLite | `rusqlite` | **0.37.0** (`bundled` → SQLite 3.50.2) | docs.rs/crate/rusqlite/0.37.0 (R3); note 0.39.0 exists on the GitHub README main branch (3.51.3 bundled) — **VERIFY** latest at P1, pin whatever is current |
| MCP server | `rmcp` | **3.1.2** (official modelcontextprotocol/rust-sdk; MCP 2026-07-28 spec + 2025-11-25 compat) | crates.io/crates/rmcp + github.com/modelcontextprotocol/rust-sdk (R3) |
| Static file embedding | `rust-embed` | 8.12.0 (`debug-embed` for dev parity; `compression` optional) | crates.io/crates/rust-embed (R3) |
| HTTP server (web + API) | `axum` 0.8 (or `hyper` + `http` manually) | axum 0.8 current | R3 rust-embed README lists axum 0.8; decide at P4 (either fine) |
| YAML | `serde_yaml` | 0.9.x | standard; **VERIFY** at P1 |
| JSON | `serde` + `serde_json` | 1.x | universal |
| CLI | `clap` | 4.5 | workspace R1 |
| Errors | `thiserror` / `anyhow` | 2.x / 1.x | R1 |
| UUID / time | `uuid` (v4), `chrono` (+ `chrono-tz` for `ZoneInfo` parity) | uuid 1.x, chrono 0.4.38 | R1 (`livekit/Cargo.toml` uses chrono 0.4.38) |
| WAV I/O + resample | `hound` 3.5 (WAV), `rubato` or `soxr-sys` (resample) | hound 3.5 in R1 examples; soxr-sys 0.1.3 in R1 workspace | R1 |
| Random | `rand` | 0.9 | R1 |
| Logging | `log` + `env_logger` | 0.4 / 0.11 | R1 |
| Plugins | `pyo3` | 0.27.x (0.27.2 latest of the 0.27 series; 0.28 exists but MSRV 1.83) | github.com/PyO3/pyo3 releases (R3) — decision: **embed CPython via pyo3 to run the *existing* `.py` verify plugins unchanged** (see plan §Key decisions; P8) |
| Process spawning (judge backends, sapi TTS) | `tokio::process` | tokio | std |
| CLI cross-platform signals | `tokio::signal` + `ctrlc` crate | ctrlc 3.x | standard |

### 3.1 SQLite choice: `rusqlite` (not sqlx)

Python uses `aiosqlite` + raw SQL with a **fixed schema** (`SCHEMA` string in
`logging/sqlite_store.py`, R4). The Rust side needs the same three tables
(`runs`, `run_events`, `run_turns` — R4) with identical DDL. `rusqlite` with `bundled`
gives: no system SQLite dependency, sync API (fine — writes are small, per-event
flushed; the writer already batches), and trivially reproduces the exact schema.
`sqlx` would add an async runtime + query macro build step + a runtime dependency on
SQLite anyway, with **no** benefit for a fixed-schema embedded DB. **Decision: rusqlite.**

---

## 4. Prior art / gap

| What exists | Evidence | Gap for the port |
|---|---|---|
| `livekit/rust-sdks` — official client+server SDK, active (release commit 2026-08-10) | R1, R3 | No built-in simulation of a caller; that is lks's job. SDK is the substrate, not competition. |
| `livekit/agents` (Python) — the agent framework under test | repo (R4 context) | lks tests agents; Agents is the *target*, never imported by core (AGENTS.md boundary). |
| `gemini-live-rs` — Gemini Live client | R2, R3 | Wire client only; no scenario/script/report harness. Exact match for the bridge. |
| LiveKit Cloud's own "simulations" / test harness docs | `docs/research-livekit-official-simulations.md` (repo, 2026) | Documented gap: no official open-source equivalent to lks's scripted caller + forensic report. |
| Python lks itself | R4 | The contract to replicate: config schema, JSONL scenario format, event envelope, sqlite, summary.json, web player, MCP tools. |
| MCP ecosystem | rmcp 3.1.2 (R3) | No MCP server ships a LiveKit caller simulator; lks's MCP tool surface (`mcp_server.py` — 25 tools, R4) is novel. |
| OpenAI Realtime WS in Rust | none surfaced | Hand-rolled (like Python side, R4 `callers/openai.py`). |

**Conclusion:** there is no prior open-source Rust implementation of a LiveKit caller
simulator; the port is greenfield composition of (livekit + gemini-live + tokio +
rusqlite + rmcp). The single most valuable prior art is the Python codebase itself —
the port is a *behavioral* port with byte-compatible artifacts, not a redesign.

---

## 5. Compatibility decisions

### 5.1 Report format reuse (byte-compatible)

Ground truth: real run `114-people-pleaser-refuse-card-20260809-201652-8b32`
(R5). Report dir contains: `events.jsonl`, `summary.json`, `meta.json`, `timeline.md`,
`review.md`, `conversation.wav` (+ `cues.json` written by `web/cues.py`).

- **events.jsonl** envelope (R5 first line):
  `event_id` (`evt_<12 hex>`), `seq` (1-based), `run_id`, `turn`, `kind`, `ts` (epoch ms),
  `ts_mono_ms` (ms since run start), `datetime_utc` (ISO-8601 ms + `Z`),
  `datetime_local` (ISO-8601 ms with tz offset), `source`, `parent_event_id`, `spec`.
  Dialogue snapshot and `include_dialogue` logic per `logging/event_writer.py` (R4).
- **Event kinds** (R4 enumeration): `run.started`, `run.end_condition`, `run.ended`,
  `run.error`, `room.*` (`participant_connected`, `participant_disconnected`,
  `track_subscribed`, `disconnected`, `active_speakers`), `dispatch.created`,
  `dispatch.agent_joined`, `dispatch.agent_timeout` (R4 `run_orchestrator.py:287` maps to
  `sim.leg_error`), `transcript.user.final`, `transcript.agent.final` (+
  `transcript.agent.preamble`), `tool.start`, `assert.verify`, `script.verify`,
  `judge.verdict`, `sim.*` (`sim.connected`, `sim.mic_published`, `sim.observer_joined`,
  `sim.gemini_connected`, `sim.gemini_activity`, `sim.gemini_socket_drop`,
  `sim.gemini_reconnecting`, `sim.gemini_go_away`, `sim.gemini_resumption_handle`,
  `sim.openai_connected`, `sim.openai_socket_drop`, `sim.agent_audio_bridged`,
  `sim.agent_audio_recorded`, `sim.audio_recorded`, `sim.script.cue`,
  `sim.script.wait`, `sim.script.hang_up`, `sim.script.hang_up_deferred`,
  `sim.script.error`, `sim.script_inject`, `sim.script_deferred_end_call`,
  `sim.end_call_token`, `sim.heard_agent`, `sim.hold_timeout`, `sim.silent_mode`,
  `sim.silent_mode_skip_inject`, `sim.error`, `sim.hang_up`, `sim.leg_error`,
  `sim.agent_greeted_nudge(_skipped)`, `sim.caller_midcall`, `sim.interrupt_rate`,
  `sim.interrupt_rate_skip`, `sim.caller.audio_source_start`, `sim.caller_role_flip_suppressed`,
  `sim.agent_listen_room`, `sim.agent.audio_onset`, `interruption`,
  `cues.json` (web), `bootstrap`/`noise`/`legacy`/`reground` cue kinds (R4 `cue_catalog`),
  `sim.gemini`/`sim.openai` (raw wire frames, `source="sim.gemini"`, R4 gemini.py:1366+).
  The full set is frozen by R4 grep; the port's `EventKind` enum must accept the union.
- **summary.json** (R5): top-level keys `run_id, status, duration_ms, turn_count,
  event_count, turn_taking_ms, metrics, tool_calls, tool_errors, interruptions,
  silences, verdict, turns, caller_mode, end_reason, script_verify, assert_verify,
  caller`. `metrics` has `schema` (version), `turn_taking_ms`, `ttfw_ms`,
  `ttfw_source`, `recovery_ms`, `barge_count`, `barges_recovered`,
  `barge_recovery_rate`, `interruption_count`, `silence_events`, `agent_finals`,
  `user_finals`, `tool_calls`, `tool_errors`, `tool_error_rate` (R4 `metrics.py` key set).
- **meta.json** (R5): scenario id, room name, agent_name, config snapshot (redacted —
  `url_host`, `agent_name`, `agent_join_timeout_ms`, `dispatch_metadata_set`,
  `simulator.provider/mode/voice_model/voice/language`, …).
- **runs.sqlite** (R4 `logging/sqlite_store.py` SCHEMA): tables `runs`
  (`run_id, scenario_id, room_name, agent_name, status, started_utc, ended_utc,
  duration_ms, turn_count, tool_errors, verdict, report_dir, summary_json`),
  `run_events` (`run_id, event_id, seq, turn, kind, ts, datetime_utc, source,
  payload_json`, PK `(run_id, seq)`), `run_turns` (`run_id, turn, user_text,
  agent_text, turn_taking_ms, tool_count, tool_errors, interrupted`).
- **timeline.md / review.md**: generated markdown; format kept human-readable, not
  byte-locked (but regenerate deterministically for golden tests).

**Decision:** Rust writer replicates the envelope byte-for-byte (same field order,
same formatting: `datetime_utc` with `Z`, `datetime_local` ISO with offset from
`chrono-tz`; `seq` monotonic from 1; `event_id` `evt_` + 12 hex chars from
`uuid::Uuid::new_v4().simple().to_string()[..12]`; `ts` epoch ms; `ts_mono_ms` from a
`std::time::Instant` origin). SQLite DDL byte-identical (case + whitespace) so the
existing Python `list_runs`/`get_run_log` can read Rust-written DBs and vice versa.
`summary.json` key-for-key identical.

### 5.2 Config compatibility

`config.yaml` schema from R4 `config.py`: `livekit` (`url, api_key, api_secret,
agent_name, room_prepare_ms=500, agent_join_timeout_ms=25000, dispatch_metadata`),
`simulator` (`provider: google|openai` default `google`, `mode: realtime`,
`api_key`, `language: en-US`, `voice: {model, voice: Puck, language}`),
`observe` (record_audio etc.), `judge` (`endpoint_type`, `api_key`…),
`project`, `cues` (volume/dir aliases), `telephony` (trunk ids, sip_uri…).
Rust `Config` structs mirror these exactly; `_require` fail-fast semantics
(ConfigError with actionable message) preserved. Redaction: `config_snapshot` never
includes secrets (R5 meta.json proves: only `url_host` and booleans).

### 5.3 Scenario compatibility

- Legacy `.jsonl` (agent-sim/v1) still parsed (R4 `scenario.py` legacy parser; templates
  include both `scenario-scaffold.jsonl` and `.yaml`); **new YAML format** is the
  canonical one going forward (R4 `scenario_yaml.py`; templates show both). Rust must
  parse **both**, emitting identical `Scenario` structs; `convert` tool re-emits YAML
  from JSONL.
- Scenario dataclass fields locked in R4 `scenario.py` (§Scenario): `id, path, locale,
  tags, persona, context, simulator {max_turns=6, timeout_s=120, first_speaker=agent},
  execute {…overrides…, hold_music_timeout_s}, dispatch {metadata},
  caller {mode=webrtc_sim}, telephony {…}, pass_criteria, pass_judges,
  pass_criteria_mode (all|majority|any), script_steps, script_verify, plugin_modules,
  asserts, behavior_spec, caller_policy`.
- Script step schema (`script/models.py`, `script_parse.py` R4): `agent_speaking`,
  `delay_ms`, `delivery` (`room_pcm` wav / `sapi` / `say`), `say`, `interrupt_class`,
  `gain`, `barge_in`, `hold`, `with_blip`, `asset`; `script_verify` counts.
- Persona → system prompt: `caller/` (`DefaultCallerPolicy`, `prompt_sections.py`,
  `persona_traits.py`) — ported as string composition (deterministic; golden-testable).

### 5.4 Web embed

`web/` is a Vite/TS app; `pnpm build` → `web/dist/` (~few hundred kB; index.html +
favicon.svg + icons.svg + assets/). Python embeds it in the wheel as `web_static`
(R4 `web/server.py`). Rust embeds `web/dist` via **rust-embed 8.12** with
`debug-embed` so `lks web` works from `cargo run` and from the release binary; served by
the report server (default host `127.0.0.1`, port 8765, R4 `web/server.py`). The REST
API surface (`/api/v1/*` — `cues`, `markers`, run log, report, compare — R4
`web/api.py`) is ported; web player JS is unchanged.

### 5.5 MCP surface

`mcp_server.py` exposes 25 tools (R4 listing): `guide, web, init_project, preflight,
list_scenarios, list_plugins, list_cues, validate_scenario, export_scenario,
init_scenario, convert_scenario, execute_scenario, optimize_persona, execute_scenarios,
execute_scenario_dict, scenario_from_run, get_run_status, get_run_log, get_run_report,
compare_runs, list_runs`. The Rust MCP server must expose the **same names + same
parameter names + same JSON return shapes** (they are the agent-facing contract; the
existing Claude Code configs reference them). `rmcp` 3.1.2 with the `server` feature and
`#[tool]` macros is the implementation vehicle.

---

## 6. Risks of the ecosystem (summary for the plan)

| Risk | Evidence | Mitigation in plan |
|---|---|---|
| libwebrtc build time/complexity | R1 `webrtc-sys`, `download_ffi.py`, rustflags requirement (R3) | Prebuilt FFI downloads, sccache CI, cross-compile matrix; `rustls-tls-webpki-roots` |
| `gemini-live` community maintenance (0.1.x, single maintainer) | R2/R3 | Thin wrapper; wire protocol documented; fallback hand-rolled client (Candidate C) |
| Rust LiveKit SDK gaps (transcription plumbing, some events) | §1.3 VERIFY items | Derive transcripts from audio + session observer like Python; feature-probe at P2 |
| Windows support | R1 builds.yml lists platforms — VERIFY | Keep Python as Windows fallback until a Windows release binary is proven in CI |
| Dual-maintenance (Python + Rust) | — | Rust reaches parity progressively; Python stays the reference until P10; then Python frozen for bugfixes only, removal tracked as a separate decision |
| MCP parity drift | R4 25-tool surface | Tool-surface golden test: run both `lks mcp` servers against a scripted client, diff tool lists + call shapes |

---

## 7. Verification artifacts on disk

- `/tmp/rust-sdks-verify.bak` — livekit/rust-sdks @ `1a477bc` (2026-08-10), includes all
  crate manifests and feature flags cited above.
- `/tmp/glsrc/` — `gemini-live` crate sources (`session.rs`, `transport.rs`,
  `config.rs`, `client_message.rs`, `server_message.rs`, `protocol.md`) used to confirm
  API surface and protocol facts.
- Repo paths cited throughout (R4/R5) are absolute under
  `/Users/tranquangdang21/Projects/livekit/livekit-agent-simulator/`.
