# Rust full-port research — rewrite `livekit-agent-simulator` (lks)

> Research companion to `docs/plans/PLAN-20260813-rust-full-port.md`.
> Status: **research complete, decisions sealed** (2026-08-13). Revision 2: cross-checked against
> the Python ground truth (21 MCP tools / 23 CLI commands — 22 data + `mcp` / 25.1 kLOC / 105 files / 16 kHz
> conversation.wav / `publish_dtmf` API) and the crate sources (gemini-live `SpeechConfig`
> lacks `language_code`; reconnect policy differs from Python by design). Corrected items are
> marked **REVISED**.
> Every claim below is backed by a URL, an on-disk crate checkout, or a file path in this repo.
> Where a fact is uncertain it is marked **VERIFY** with a fallback — the plan never invents APIs.

---

## 0. Scope and inputs

Target of the rewrite: `src/livekit_agent_simulator/` (**25,090 LOC** Python across **105 files**,
per `wc -l` of the package tree on 2026-08-13 — REVISED; earlier drafts said ~34.5 kLOC / 108).
New Rust workspace lives at `src/livekit_agent_simulator_rust/` (exists, empty); the single
crate lives under `crates/lks/` (workspace member, binary + library `lks`). The Python
package remains the reference implementation; the Rust port must stay **report-format
byte-compatible** and **config/scenario compatible** so `compare_runs` and the existing web
player keep working across implementations.

Evidence sources used:

| # | Source | What it grounds |
|---|---|---|
| R1 | On-disk checkout of `livekit/rust-sdks` at commit `1a477bc422c6890537b3bcdb017f0ac094d49661` ("Release packages (#1316)", 2026-08-10) in `/tmp/rust-sdks-verify.bak` | Crate inventory, versions, feature flags, API surface (incl. `LocalParticipant::publish_dtmf` — REVISED) |
| R2 | On-disk Gemini Live Rust client sources in `/tmp/glsrc/` (`session.rs`, `transport.rs`, `config.rs`, `client_message.rs`, `server_message.rs`, `protocol.md`) | Confirms this is the `gemini-live` crate (jacoblincool/gemini-live-rs); wire-level design; confirms `SpeechConfig` has NO `language_code` field (REVISED) |
| R3 | Web verification (crates.io / docs.rs / GitHub, 2026-08-13) | Current published versions of `livekit`, `livekit-api`, `gemini-live`, `rmcp`, `rusqlite`, `rust-embed`, `pyo3` |
| R4 | Python package source in this repo (`src/livekit_agent_simulator/…`) | Report format, config schema, scenario schema, event kinds, MCP/CLI tool counts (REVISED) |
| R5 | **REVISED (2026-08-13 audit): the `114-people-pleaser-refuse-card-20260809-201652-8b32` report dir is NOT in this repo** (it lived on the author's machine; the Aug-11 reports in `demo/base-agent/.agent-sim/reports/` e.g. `001-frontdesk-hours-20260811-071837-8197` are the on-disk stand-ins and are a **31-key** metrics fixture predating the audio-onset commit `504577a`). Ground truth for `events.jsonl`, `summary.json`, `meta.json` shapes: conversation.wav is 16 kHz; the CURRENT metrics block is **36 keys** (audio-onset keys unconditional in `metrics.py`). Capture a fresh Python run for parity fixtures before P3/P3.5. |
| R6 | `AGENTS.md`, `docs/plans/PLAN-20260806-openai-caller.md`, `install.sh`, `templates/`, `web/dist/` | Repo rules, plan template, packaging, assets (REVISED: web/dist is ~200 KB, **6 files** — `index.html`, `favicon.svg`, `icons.svg`, `assets/index-DNs624kh.js`, `assets/index-DNs624kh.js.map`, `assets/index-EYhUFLj5.css`) |

---

## 1. LiveKit Rust SDK capability

### 1.1 Crates and versions (verified 2026-08-13)

| Crate | Latest verified | Evidence |
|---|---|---|
| `livekit` (realtime client) | 0.8.3 published 2026-08-10 | crates.io/crates/livekit (R3); `livekit/Cargo.toml` version `0.8.3` in R1 |
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

**REVISED — verified API surface from R1 source** (`livekit/src/room/…`, `livekit-api/src/services/…`):

- `Room::connect(url: &str, token: &str, options: RoomOptions)` returns a `(Room, mpsc::UnboundedReceiver<RoomEvent>)`; `RoomOptions` defaults `auto_subscribe = true`, `single_peer_connection = true`.
- `RoomEvent` variants relevant to the port: `TrackSubscribed`, `ParticipantConnected/Disconnected/Active`, `Disconnected{reason}`, `DataReceived{payload, topic, kind, participant}`, `TranscriptionReceived{segments: Vec<TranscriptionSegment{id, text, start_time, end_time, final, language}>}`, `TextStreamOpened{reader: TakeCell<TextStreamReader>, topic, participant_identity}`, `ByteStreamOpened`, `ActiveSpeakersChanged`, `ConnectionStateChanged`.
- `lk.transcription` streams arrive as `TextStreamOpened`; the reader exposes `.info()` attributes (`lk.transcription_final`, `lk.segment_id`, `lk.transcribed_track_id`) and `read_all()`/`try_next()` — the Rust mirror of Python `register_text_stream_handler("lk.transcription", …)` (R4 `observer.py:266`). Legacy `TranscriptionReceived` remains supported for `publish_transcription()`-style senders — the port must handle BOTH paths to match current Python observe behavior (REVISED: this resolves the §1.3 VERIFY).
- **DTMF: `LocalParticipant::publish_dtmf(SipDTMF)` exists** — `livekit/src/room/participant/local_participant.rs:783` (sends `proto::SipDtmf{code, digit}` as a data packet). `SipDTMF` is defined in `livekit/src/room/mod.rs:336` (REVISED: it is NOT in `livekit::webrtc`). This maps 1:1 to Python `script/runtime.py:311` `local.publish_dtmf(code=DMAP[ch], digit=ch)`.
- `livekit-api` services: `room::RoomClient` (create_room with `CreateRoomOptions{empty_timeout, max_participants, …}`, delete_room, list_rooms, list_participants, remove_participant, mute_published_track, update_subscriptions, update_participant), `agent_dispatch::AgentDispatchClient::create_dispatch(proto::CreateAgentDispatchRequest{agent_name, room, metadata, …})` — metadata is an opaque string passthrough (tag 3), `sip::SIPClient` (create_sip_participant, trunks), `access_token::AccessToken::with_api_key(…).with_grants(VideoGrants{room_join, room, …}).to_jwt()`.

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
  with URL + token; room events via the `RoomEvent` receiver — `TrackSubscribed`,
  `ParticipantConnected/Disconnected`, `Disconnected{reason}`, `DataReceived`,
  `TextStreamOpened`, `ByteStreamOpened` (REVISED — exact variant names verified in R1
  `room/mod.rs`; `RoomDisconnected`/`DataPacketReceived` were approximate names in the
  earlier draft).
- `LocalParticipant`: `publish_track` (audio source), `publish_data_track`,
  `stream_text`/`stream_bytes` (text/byte streams, R3), `publish_dtmf(SipDTMF)` (REVISED —
  see §1.1).
- `RemoteTrack`: `AudioStream` (`rtc::AudioStream`), `AudioSource` (publish PCM into a
  track), `RemoteAudioTrack`, `TrackEvent::AudioFrame` (audio frames with `data`,
  `sample_rate`, `num_channels`, `samples_per_channel`).
- Data packets (`DataPacket::Reliable`/`Lossy`) for the `lk.agent.session` topic used by
  `livekit/agent_session_observer.py` (REVISED: Python uses `register_byte_stream_handler`
  + `stream_bytes` — R4 `agent_session_observer.py:96,422`; the Rust mirror is
  `ByteStreamOpened` + `local_participant().stream_bytes(...)`).
- RPC (`RpcRequest`/`RpcResponse`) — not used by lks (agent under test may use it; we only
  observe), **no port needed**.
- **Transcription (REVISED — VERIFY resolved):** `lk.transcription` text streams arrive as
  `TextStreamOpened` with `lk.transcription_final`/`lk.segment_id` attributes; legacy
  `TranscriptionReceived` events also exist. Python `Observer` consumes both
  (`observer.py` registers `register_text_stream_handler("lk.transcription", …)` and also
  handles `TranscriptionReceived`-style sources); the port must do the same (dual path).
  Fallback (sealed in plan §Key decisions): derive agent/user transcript events from the
  audio stream + agent-session text streams, same as the Python `Observer` does today.

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
- **DTMF (REVISED):** `LocalParticipant::publish_dtmf(SipDTMF)` on the realtime SDK
  (`livekit/src/room/participant/local_participant.rs:783`; `SipDTMF` in
  `livekit/src/room/mod.rs:336`) — for the `dtmf` script cue path (demo `dtmf-feature`
  suite). Earlier draft's `livekit::webrtc::SipDTMF` location was wrong.

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
- REVISED — R1 toolchain: `rust-toolchain.toml` pins **1.97.1** (`profile = "default"`).

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
  side assumes 24 kHz mono output in `callers/gemini.py` `GEMINI_OUT_RATE = 24_000`, and
  **16 kHz mono input** — `GEMINI_IN_RATE = 16_000`, R4 `callers/gemini.py:49-50`; REVISED:
  the agent-audio-to-model path is 16 kHz (`rtc.AudioStream(track, sample_rate=16000)`, R4
  `observer.py:298`), the sim-audio-out path is 16 kHz (`rtc.AudioSource(16000, 1)`, R4
  `callers/base.py`), and only the model *output* is 24 kHz).
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

- Published on crates.io: **`gemini-live` 0.1.8** (published 2026-04-12, ~620 downloads;
  repo last commit 2026-04-12 — **stalled ~4 months** as of 2026-08-13; REVISED with the
  maintenance facts). docs.rs coverage 58%, 2/100 items with examples.
- This is exactly the crate whose sources are in `/tmp/glsrc/` (R2): module layout
  `session/transport/codec/audio/types/error`, `SessionConfig { transport, setup,
  reconnect }`, `ReconnectPolicy { enabled: true (default), base_backoff: 500ms,
  max_backoff: 5s, max_attempts: Some(10) }` with exponential backoff,
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
- **REVISED — two known deltas vs the Python caller** (R2 `config.rs` + Python
  `callers/gemini.py`):
  1. `SpeechConfig { voice_config: VoiceConfig }` → `PrebuiltVoiceConfig { voice_name }`
     has **NO `language_code` field**; the Python caller sets
     `speech_config.language_code = voice.language`. Mitigation: vendor/fork the crate
     (MIT, ~1400 LOC core) and patch one serde `camelCase` field
     (`language_code`) onto `VoiceConfig`/`PrebuiltVoiceConfig`. Without the patch the
     caller language silently falls back to server auto-detect.
  2. **Reconnect policy cannot distinguish GoAway from mid-call ConnectionLost** — the
     runner auto-reconnects on both, injecting the saved resume handle. Python
     deliberately does NOT reconnect mid-call (`transport_dropped = True`, no mid-call
     reconnect; reconnects only on GoAway). `ReconnectPolicy { enabled: false }` would
     also kill the wanted GoAway resumption. Mitigation options are sealed in plan D4 /
     Open question 2: fork-patch a `reconnect_on_drop: bool` knob, or accept
     auto-reconnect as strictly-more-robust and emit `sim.gemini_socket_drop` +
     `transport_dropped` when `SessionStatus` goes `Reconnecting` without a preceding
     `GoAway`, or when reconnect exhausts.
- Risk: single-maintainer community crate (jacoblincool), MIT/Apache dual license,
  version 0.1.x (pre-1.0), stalled maintenance. Mitigation: **vendor it** — it is a
  **thin, typed wrapper over the wire protocol we can reimplement from `protocol.md`**;
  the crate is small (7 files, ~34 kB `session.rs`).
- **VERIFY** at P2: confirm `ServerEvent::ModelAudio` payload rate is 24 kHz raw PCM and
  that `setup` accepts `outputAudioTranscription` + `inputAudioTranscription` — the
  codec/type layer is generated from the same proto the Python `google-genai` uses, so
  expected PASS.

### 2.4 Candidate C — hand-rolled client (raw tokio-tungstenite)

The Python side itself is effectively hand-rolled at the semantics layer
(`callers/gemini.py` uses `google.genai` only for types; the event→action mapping,
activity markers, reconnect, transport-drop handling are all in lks). A Rust
hand-rolled client is ~600–900 lines (mirror of R2's session/transport/config shape).
Fallback if B is unmaintained or the fork patch surface grows.

### 2.5 Decision (sealed in the plan)

**Candidate B (`gemini-live` crate) vendored/forked primary, Candidate C fallback.**
Rationale: exact feature match (resumption, GoAway, activity detection, transcript
events), no heavy deps, and the wire protocol is documented well enough (R2
`protocol.md`) that B is replaceable in a week. The vendor patch is ~2 fields
(`language_code`; optional `reconnect_on_drop`). Do NOT use rs_genai/gemini-genai-rs
(even younger, pre-1.0 rename churn, resume-handle auto-injection on reconnect
unverified — REVISED, see plan D4). OpenAI Realtime stays **hand-rolled on
tokio-tungstenite** — the Python side already does this (`callers/openai.py` uses raw
`websockets.asyncio.client`, R4) and the OpenAI WS protocol is simpler (one
`session.update`, `input_audio_buffer.append`, `response.create`, event types).

---

## 3. Supporting crates

| Need | Crate | Version (verified 2026-08-13) | Evidence |
|---|---|---|---|
| Async runtime | `tokio` | 1.x (full features) | R3 (rmcp/rust-sdks READMEs); required by `livekit` SDK |
| Realtime + server SDK | `livekit`, `livekit-api` | 0.8.3 / 0.6.3 | §1.1 |
| Gemini Live | `gemini-live` | 0.1.8 (vendored/forked — REVISED) | §2.3 |
| WebSocket (OpenAI + fallbacks) | `tokio-tungstenite` | 0.30 (REVISED — latest verified 2026-07-11; rustls-tls-webpki-roots feature) | R3 |
| SQLite | `rusqlite` | **0.40.1** (REVISED — was 0.37; latest verified 2026-08-13, `bundled` → SQLite 3.53.2; tokio-rusqlite 0.7 pins `^0.37` and is NOT used — we wrap `spawn_blocking` ourselves) | docs.rs/crate/rusqlite (R3) |
| MCP server | `rmcp` | **3.1.2** (official modelcontextprotocol/rust-sdk; MCP 2026-07-28 spec + 2025-11-25 compat) | crates.io/crates/rmcp + github.com/modelcontextprotocol/rust-sdk (R3) |
| Static file embedding | `rust-embed` | 8.12.0 (`debug-embed` for dev parity; `compression` optional) | crates.io/crates/rust-embed (R3) |
| HTTP server (web + API) | `axum` 0.8 (or `hyper` + `http` manually) | axum 0.8 current | R3 rust-embed README lists axum 0.8; decide at P4 (either fine) |
| YAML | `yaml_serde` 0.10.x (REVISED — `serde_yaml` was archived by dtolnay 2024-03-25; `yaml_serde` is the official YAML-org fork, drop-in via `serde_yaml = { package = "yaml_serde" }`; `serde_yaml_ng` stalled 2024; `serde_yml` publicly criticized — avoid) | 0.10.5 | crates.io/crates/yaml_serde (R3) |
| JSON | `serde` + `serde_json` | 1.x (enable `preserve_order` for Python dict-order parity) | universal |
| CLI | `clap` | 4.x (derive; pin `4` — v5 unstable branch in flight) | R1 |
| Errors | `thiserror` / `anyhow` | 2.x / 1.x | R1 |
| UUID / time | `uuid` (v4), `jiff` (+ `chrono-tz` for `datetime_local` offsets) | uuid 1.24, jiff 0.2.35 (strtime gives exact `%Y%m%d-%H%M%S` parity; run_id is pure UTC — verified R4 `run_orchestrator.py` uses `timezone.utc` only, so NO tzdata needed for run_id; `chrono-tz` only for `datetime_local` tz conversion) | R1, R3 |
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
SQLite anyway, with **no** benefit for a fixed-schema embedded DB. **Decision: rusqlite
0.40.1 bundled** behind a small `spawn_blocking` async wrapper (REVISED — not
tokio-rusqlite 0.7, which pins rusqlite `^0.37` and lags 3 majors).

---

## 4. Prior art / gap

| What exists | Evidence | Gap for the port |
|---|---|---|
| `livekit/rust-sdks` — official client+server SDK, active (release commit 2026-08-10) | R1, R3 | No built-in simulation of a caller; that is lks's job. SDK is the substrate, not competition. |
| `livekit/agents` (Python) — the agent framework under test | repo (R4 context) | lks tests agents; Agents is the *target*, never imported by core (AGENTS.md boundary). |
| `gemini-live-rs` — Gemini Live client | R2, R3 | Wire client only; no scenario/script/report harness. Exact match for the bridge (after the ~2-field vendor patch). |
| LiveKit Cloud's own "simulations" / test harness docs | `docs/research-livekit-official-simulations.md` (repo, 2026) | Documented gap: no official open-source equivalent to lks's scripted caller + forensic report. |
| Python lks itself | R4 | The contract to replicate: config schema, JSONL scenario format, event envelope, sqlite, summary.json, web player, MCP tools. |
| MCP ecosystem | rmcp 3.1.2 (R3) | No MCP server ships a LiveKit caller simulator; lks's MCP tool surface (`mcp_server.py` — **21 tools**, R4) is novel. |
| OpenAI Realtime WS in Rust | none surfaced | Hand-rolled (like Python side, R4 `callers/openai.py`). |

**Conclusion:** there is no prior open-source Rust implementation of a LiveKit caller
simulator; the port is greenfield composition of (livekit + gemini-live + tokio +
rusqlite + rmcp). The single most valuable prior art is the Python codebase itself —
the port is a *behavioral* port with byte-compatible artifacts, not a redesign.

---

## 5. Compatibility decisions

### 5.1 Report format reuse (byte-compatible)

Ground truth: real run `114-people-pleaser-refuse-card-20260809-201652-8b32`
(R5 — REVISED: this exact dir is not in the repo; use a fresh Python-captured run as the
golden fixture, see R5 note above). Report dir contains: `events.jsonl`, `summary.json`, `meta.json`, `timeline.md`,
`review.md`, `conversation.wav` (+ `cues.json` written by `web/cues.py`).

- **events.jsonl** envelope (R5 first line):
  `event_id` (`evt_<12 hex>`), `seq` (1-based), `run_id`, `turn`, `kind`, `ts` (epoch ms),
  `ts_mono_ms` (ms since run start), `datetime_utc` (ISO-8601 ms + `Z`),
  `datetime_local` (ISO-8601 ms with tz offset), `source`, `parent_event_id`, `spec`.
  Dialogue snapshot and `include_dialogue` logic per `logging/event_writer.py` (R4) —
  the `dialogue` key is inserted **before** `spec` (R4 `event_writer.py:108–123`; REVISED).
- **Event kinds** (R4 enumeration; REVISED — the full set):
  `run.started`, `run.end_condition`, `run.ended`, `run.error`, `room.*`
  (`participant_connected`, `participant_disconnected`, `track_subscribed`,
  `disconnected`, `active_speakers`), `dispatch.created`, `dispatch.agent_joined`,
  `dispatch.agent_timeout`, `sim.leg_error`, `transcript.user.final`,
  `transcript.agent.final` (+ `transcript.agent.preamble`, `transcript.*.interim`),
  `tool.start`/`tool.end`/`tool.error`, `assert.verify`, `script.verify`,
  `judge.verdict`, `assert.goals_met`, `sim.*` (`sim.connected`, `sim.mic_published`,
  `sim.observer_joined`, `sim.gemini_connected`, `sim.gemini_activity`,
  `sim.gemini_socket_drop`, `sim.gemini_reconnecting`, `sim.gemini_go_away`,
  `sim.gemini_resumption_handle`, `sim.openai_connected`, `sim.openai_socket_drop`,
  `sim.agent_audio_bridged`, `sim.agent_audio_recorded`, `sim.audio_recorded`,
  `sim.script.cue`, `sim.script.wait`, `sim.script.dtmf`, `sim.script.hang_up`,
  `sim.script.hang_up_deferred`, `sim.script.error`, `sim.script_inject`,
  `sim.script_deferred_end_call`, `sim.end_call_token`, `sim.heard_agent`,
  `sim.hold_timeout`, `sim.silent_mode`, `sim.silent_mode_skip_inject`, `sim.error`,
  `sim.hang_up`, `sim.agent_greeted_nudge(_skipped)`, `sim.caller_midcall`,
  `sim.interrupt_rate`, `sim.interrupt_rate_skip`, `sim.caller.audio_source_start`,
  `sim.caller_role_flip_suppressed`, `sim.agent_listen_room`, `sim.agent.audio_onset`,
  `interruption`, `session.*` (`agent_state`, `user_state`, `usage`, `error`,
  `overlapping_speech`, `debug`, `chat_history`, `tool_execution`), `handoff`,
  `silence.detected`, `observer.error`/`observer.warning`,
  `data.message`/`data.raw` (topical topics), `sim.gemini`/`sim.openai` raw wire frames
  (`source="sim.gemini"`, R4 gemini.py). The full set is frozen by R4 grep; the port's
  `EventKind` enum must accept the union.
- **summary.json** (R5; REVISED — full **36-key** `metrics` block per current `metrics.py`, incl. the unconditional audio-onset keys `ttfa_run_ms`/`ttfa_source`/`turn_taking_audio_ms`/`user_audio_source_count`/`agent_audio_onset_count`; the 31-key R5-era block predates commit `504577a`):
  top-level keys `run_id, status, duration_ms, turn_count, event_count, turn_taking_ms,
  metrics, tool_calls, tool_errors, interruptions, silences, verdict, turns` (13 keys in
  the R5 file; `caller_mode`, `end_reason`, `dial_ms` and the merged `script_verify`,
  `assert_verify`, `caller` blocks appear from summary_extra when present — port
  `finalize()`'s merge logic verbatim). `metrics` (36 keys, REVISED) has `schema`,
  `turn_taking_ms`, `ttfw_ms`, `ttfw_source`, `ttfa_run_ms`, `ttfa_source`,
  `turn_taking_audio_ms`, `user_audio_source_count`, `agent_audio_onset_count`,
  `recovery_ms`, `barge_count`, `barges_recovered`, `barge_recovery_rate`,
  `interruption_count`, `silence_events`, `agent_finals`, `user_finals`, `tool_calls`,
  `tool_errors`, `tool_error_rate`, `talk_ratio`, `agent_chars`, `user_chars`,
  `user_words_count`, `user_words_p10/p50/mean`, `user_words_natural_count/p10/p50/mean`,
  `user_words_script_count/p50/mean`, `slow_turns_over_2500ms`, `slow_turns_over_5000ms`
  (R4 `metrics.py` key set — the 5 audio-onset keys are unconditional since commit `504577a`).
- **meta.json** (R5; REVISED — `config_snapshot` shape verified): scenario id, room
  name, agent_name, config snapshot (redacted) with keys `project`, `livekit`
  (`url_host`, `agent_name`, `agent_join_timeout_ms`, `dispatch_metadata_set`),
  `simulator` (`provider`, `mode`, `voice_model`, `voice`, `language`), `judge_enabled`,
  `judge` (`model`, `http`, `endpoint_type`, `api_key_set`) | None, `cues` (`dirs`,
  `alias_keys`, `target_cues_dir`), `observe` (`lk_transcription`, `lk_agent_session`,
  `record_audio`, `data_topics`, `silence_threshold_ms`, `audio_onset_enabled`,
  `audio_onset_threshold`), `telephony` (trunk/dial-in booleans, `prepare_ms`,
  `wait_until_answered`, `krisp_enabled`), `observe_gaps` (`["tool_events"]` when
  not lk_agent_session and no tool_event_patterns).
- **conversation.wav (REVISED):** 16 kHz PCM16 stereo, L = sim caller, R = agent
  (R5 `meta.json` audio: `{'sample_rate': 16000, 'channels': {'left': 'sim', 'right':
  'agent'}, ...}`; earlier draft's 24 kHz was wrong — 24 kHz is only the Gemini model
  output rate).
- **runs.sqlite** (R4 `logging/sqlite_store.py` SCHEMA): tables `runs`
  (`run_id, scenario_id, room_name, agent_name, status, started_utc, ended_utc,
  duration_ms, turn_count, tool_errors, verdict, report_dir, summary_json`),
  `run_events` (`run_id, event_id, seq, turn, kind, ts, datetime_utc, source,
  payload_json`, PK `(run_id, seq)`, `idx_run_events_kind` — REVISED: `payload_json`
  stores the FULL envelope JSON, not just spec), `run_turns` (`run_id, turn, user_text,
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
`api_key`, `language: en-US`, `voice: {model: gemini-3.1-flash-live-preview,
voice: Puck, language}`),
`observe` (`timezone=UTC`, `lk_transcription=true`, `lk_agent_session=true`,
`record_audio=true`, `data_topics`, `flow_topics`, `tool_event_patterns`,
`audio_onset {enabled=false, vad=rms, threshold=0.012, win_ms=20, energy_frames=3,
exit_frames=5, refractory_ms=60}`, `transcript_payload_types=["transcript_turn"]`,
`transcript_dedupe_window_ms=15000`, `silence_threshold_ms=4000`,
`turn_taking_warn_ms=2500`),
`judge` (`model`, `temperature=0.0`, `base_url`, `api_key`, `endpoint_type:
openai|anthropic`), `cues` (`dirs`, `aliases`), `telephony` (`outbound_trunk_id` |
legacy `sip_trunk_id`, `inbound_trunk_id`, `dial_in`, `sim_inbound_number`,
`prepare_ms=3000`, `wait_until_answered=true`, `krisp_enabled=false`, `agent_room`,
`agent_room_name_template`, `handset_isolation="mute_and_unsubscribe"`),
`project`.
Rust `Config` structs mirror these exactly; `_require` fail-fast semantics
(ConfigError with actionable message) preserved. Redaction: `config_snapshot` never
includes secrets (R5 meta.json proves: only `url_host` and booleans). REVISED —
note the Python `bool()` quirk: YAML string `"false"` coerces to `True` for
`observe` flags and `wait_until_answered`/`krisp_enabled`; replicate or document as
intentional divergence (plan defers to parity).

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
  asserts, behavior_spec, caller_policy`. JSONL kinds (KNOWN_KINDS): `Persona, Context,
  Simulator, Execute, Dispatch, PassCriteria, Script, Behavior, Plugins, Assert,
  Caller, Telephony` (+ header `Scenario`) — REVISED: the earlier draft's kind list
  missed `Behavior`/`Plugins`/`Assert`/`Caller`/`Telephony`.
- Caller modes (REVISED): `CALLER_MODES = {webrtc_sim, inbound_sip,
  outbound_human_pickup, outbound_sim_callee, agent_dials}`; `SIP_MODES` = all but
  `webrtc_sim`; `HANDSET_ISOLATION_MODES = {mute_uplink, mute_and_unsubscribe, none,
  remove}` (R4 `scenario.py:47-61`).
- Script step schema (`script/models.py`, `script_parse.py` R4): `agent_speaking`,
  `delay_ms`, `delivery` (`room_pcm` wav / `gemini_text` / `sapi`), `say`, `digits`
  (`0-9*#w` charset for `dtmf` actions), `interrupt_class`, `gain`, `barge_in`,
  `hold`, `with_blip`, `asset`; `script_verify` counts.
- Persona → system prompt: `caller/` (`DefaultCallerPolicy`, `prompt_sections.py`,
  `persona_traits.py`) — ported as string composition (deterministic; golden-testable).

### 5.4 Web embed

`web/` is a Vite/TS app; `pnpm build` → `web/dist/` (**~200 KB — REVISED, measured:
index.html 395 B + favicon.svg 9.3 KB + icons.svg 4.9 KB + assets/; earlier draft's
"few hundred kB" guess is confirmed, **6 files** — REVISED: `index.html`, `favicon.svg`, `icons.svg`, `assets/index-DNs624kh.js`, `assets/index-DNs624kh.js.map`, `assets/index-EYhUFLj5.css`). Python embeds it in the wheel as
`web_static` (R4 `web/server.py`). Rust embeds `web/dist` via **rust-embed 8.12** with
`debug-embed` so `lks web` works from `cargo run` and from the release binary; served by
the report server (default host `127.0.0.1`, port 8765, R4 `web/server.py`). The REST
API surface under `/api/v1` is ported; web player JS is unchanged.

REVISED — REST API route set (R4 `web/api.py`): `GET /health` (`{ok, version, root}`),
`GET /runs`, `GET /runs/<id>`, `GET /runs/<id>/report`, `GET /scenarios`,
`GET /scenarios/<id>`, `POST /validate`, `POST /execute`, `POST /preflight` (9 routes;
the earlier draft listed only 6). Errors are `{"error": msg}` with 400/404/500 mapping;
`Cache-Control: no-store` on JSON.

### 5.5 MCP surface

`mcp_server.py` exposes **21 tools** (REVISED — verified count of `@mcp.tool` markers at
lines 25–297 of `mcp_server.py`: `guide, web, init_project, preflight, list_scenarios,
list_plugins, list_cues, validate_scenario, export_scenario, init_scenario,
convert_scenario, execute_scenario, optimize_persona, execute_scenarios,
execute_scenario_dict, scenario_from_run, get_run_status, get_run_log, get_run_report,
compare_runs, list_runs`). The Rust MCP server must expose the **same names + same
parameter names + same JSON return shapes** (they are the agent-facing contract; the
existing Claude Code configs reference them). `rmcp` 3.1.2 with the `server` feature and
`#[tool]` macros is the implementation vehicle.

REVISED — CLI surface (R4 `cli.py`, verified `@app.command` decorators): **23 commands — 22 data
commands + the `mcp` subcommand** — `init, guide, web, serve, preflight, scenarios, cues, plugins, validate,
export, convert, scenario-init, execute, execute-all, execute-dict, optimize, status,
log, report, compare, runs, scenario-from-run` — plus the `mcp` subcommand. Exit codes:
0 success; 1 on ConfigError/ScenarioError/RuntimeError/gate-fail; **130 on Ctrl+C**
(`typer.Exit(130)`, "Interrupted — stopping."). The JSON flag is `--json` (the Python
param is named `as_json` internally to avoid shadowing the stdlib `json` module — Rust
only needs `--json`).

---

## 6. Risks of the ecosystem (summary for the plan)

| Risk | Evidence | Mitigation in plan |
|---|---|---|
| libwebrtc build time/complexity | R1 `webrtc-sys`, `download_ffi.py`, rustflags requirement (R3) | Prebuilt FFI downloads, sccache CI, cross-compile matrix; `rustls-tls-webpki-roots` |
| `gemini-live` community maintenance (0.1.x, single maintainer, stalled ~4 months) | R2/R3 | **Vendor/fork** (MIT, ~1400 LOC core) with a ~2-field patch (`language_code`; optional `reconnect_on_drop`); wire protocol documented; fallback hand-rolled client (Candidate C) |
| Rust LiveKit SDK gaps (transcription plumbing, some events) | §1.3 VERIFY items (REVISED: mostly resolved — `TextStreamOpened`/`TranscriptionReceived` dual path confirmed in R1) | Derive transcripts from audio + session observer like Python; feature-probe at P2 |
| Windows support | R1 builds.yml lists platforms — VERIFY | Keep Python as Windows fallback until a Windows release binary is proven in CI |
| Dual-maintenance (Python + Rust) | — | Rust reaches parity progressively; Python stays the reference until P10; then Python frozen for bugfixes only, removal tracked as a separate decision |
| MCP parity drift | R4 21-tool surface | Tool-surface golden test: run both `lks mcp` servers against a scripted client, diff tool lists + call shapes |

---

## 7. Verification artifacts on disk

- `/tmp/rust-sdks-verify.bak` — livekit/rust-sdks @ `1a477bc` (2026-08-10), includes all
  crate manifests, feature flags, `LocalParticipant::publish_dtmf`, and example corpus
  (`agent_dispatch`, `basic_text_stream`, `play_from_disk`, `save_to_disk`) cited above.
- `/tmp/glsrc/` — `gemini-live` crate sources (`session.rs`, `transport.rs`,
  `config.rs`, `client_message.rs`, `server_message.rs`, `protocol.md`) used to confirm
  API surface, protocol facts, and the missing `language_code` field (REVISED).
- Repo paths cited throughout (R4/R5) are absolute under
  `/Users/tranquangdang21/Projects/livekit/livekit-agent-simulator/`.
