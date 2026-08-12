# Plan — Rust full port of `livekit-agent-simulator` (lks)

> Implements: `src/livekit_agent_simulator_rust/` (exists, empty). Research: `docs/rust-port-research.md` (companion; every crate/API claim below cites it). Revision 2 (2026-08-13): cross-checked against the Python ground truth (21 MCP tools, 22 CLI commands, 25.1 kLOC / 105 files, 16 kHz conversation.wav, `publish_dtmf` API) and the crate sources (gemini-live `SpeechConfig` has no `language_code`; reconnect policy differs from Python by design). Every corrected item is marked REVISED.

## Summary (read this first)
- **You asked:** Write the definitive research doc + full-port implementation plan for rewriting the Python package `livekit-agent-simulator` (lks) in Rust. Research is done → `docs/rust-port-research.md` (written this date, 2026-08-13); this plan seals the implementation.
- **What is going on:** The Python package (`src/livekit_agent_simulator/`, ~25.1 kLOC incl. subdirs, 105 files) is an MCP + `lks` CLI tool that dials any LiveKit voice agent using `.agent-sim/` in a target repo: config, scenarios, a realtime model caller (Gemini Live / OpenAI Realtime), a scripted-cue caller, a forensic report writer (events.jsonl + runs.sqlite + summary.json), a web report player, an MCP server (21 tools), suite/compare/optimize/evals. The Python SDK chain (`livekit`, `livekit-api`, `google-genai`, `aiosqlite`) is heavy (uv venv, libwebrtc via `livekit[rtc]`), install is `pipx`-free but env-bound, and cross-platform packaging ships a zip with a wrapper (`install.sh`/`install.ps1`). The Rust rewrite targets a single static binary with embedded web assets, native-speed audio/PCM, and identical on-disk artifacts.
- **We recommend:** A single-crate Cargo workspace rooted at `src/livekit_agent_simulator_rust/` with one library crate **`lks`** living under `crates/lks/` (binary `lks`, library `lks`; bin name stays `lks` for CLI parity; `crates/lks/src/main.rs` thin, all logic in `crates/lks/src/lib.rs` + modules). Rationale: the Python package is one importable package with internal seams — mirroring that as one crate with modules (`config`, `scenario`, `script`, `run`, `callers`, `livekit`, `audio`, `logging`, `web`, `ops`, `mcp`, `suite`, `optimize`, `evals`, `plugins`) gives the same compile-time boundary checking with zero intra-workspace versioning overhead; a `lks-core` split buys nothing at 25 kLOC and would slow the port. If the crate later needs to be embedded by third-party tools, `pub` modules already expose the library surface. Decision recorded in §Key decisions D1.
- **Key crate choices (verified, research §1–§3):** `livekit 0.8.3` + `livekit-api 0.6.3` (realtime + server API; `rustls-tls-webpki-roots` feature), Gemini Live via **`gemini-live` 0.1.8 vendored/forked** (Candidate B; thin typed wrapper; hand-rolled tokio-tungstenite fallback) — REQUIRES a small fork patch (see D4/Open question 2; the crate's `SpeechConfig` lacks `language_code`), OpenAI Realtime hand-rolled on `tokio-tungstenite`, **`rusqlite` 0.40 bundled** (not sqlx — fixed schema, sync writes fit the per-event flusher), **`rmcp` 3.1.2** for MCP (official rust-sdk), **`rust-embed` 8.12** for `web/dist` (~200 KB), `axum 0.8` for the report/API server, `serde`/`yaml_serde`, `clap 4.x`, `tokio 1.x`, `hound` (WAV), `jiff` + `chrono-tz` (datetime parity), `pyo3 0.27` for plugins (P8).
- **Status:** Plan sealed. P0–P10 phased; P0–P4 are the report-parity core (P3 is the parity gate where Rust can read/write what Python reads/writes and vice versa). Manual live gates (need real LiveKit + Gemini keys) are marked MANUAL and are not CI gates; every phase has an offline deterministic gate that CI runs.
- **What is CUT from v1** (AGENTS.md no-dead-features): `lks optimize` persona-prompt optimizer is ported last (P9) and only if the MCP tool + CLI command surface is exercised by a real user flow (the Python MCP tools list is the contract — see Non-goals 6/7 and Phase P9); `evals` (custom judge backends) ported at P7 with `http_anthropic`/`http_openai`/`gemini` backends only (research §2.2: google-genai Rust Live unverified — judge REST calls are standard HTTP, no gap); plugins (P8) run the *existing* `.py` verify plugins unchanged via **embedded CPython (pyo3)** — no plugin-source rewrite; Windows release binary deferred (research §1.6 VERIFY) with Python as the documented Windows path until proven.

---

## Non-goals (do not do in this port)

1. **No behavior redesign.** The Rust port is a *behavioral* port with byte-compatible artifacts (events.jsonl, runs.sqlite, summary.json, meta.json) and config/scenario compatibility. No new features, no scenario-schema evolution, no report-format changes. Python remains the reference implementation until P10; any divergence is a bug in the port (recorded as an issue with a pinned Python-side golden).
2. **No second language runtime in the hot path.** PCM handling, cue synthesis, VAD, event writing, sqlite — all native Rust. The only embedded interpreter is CPython via pyo3 for **verify plugins** (P8) — deliberately isolated behind a plugin loader; core never depends on Python.
3. **No sqlx.** Fixed-schema embedded DB with small per-event writes → `rusqlite` (bundled) is the sealed choice (§3.1 of research). No async DB layer.
4. **No LiveKit SDK audio feature attempt.** `livekit` has no `audio` feature (research §1.2); PCM handling is ours. Do not add one.
5. **No consumer keys in core.** Same hard rule as Python (AGENTS.md): credentials only in target `.agent-sim/config.yaml` (gitignored); `config_snapshot` redacts secrets (proven by R5 meta.json — only `url_host`, booleans, model names).
6. **No dead-feature surface.** Do not port CLI/MCP/config knobs that have no user flow. During porting, if a Python feature has zero tests and zero references in docs/templates (e.g. unused judge backends, half-implemented optimize knobs), drop it and record the drop in the phase's porting note; do not "port for completeness" (AGENTS.md no-dead-features rule).
7. **No dual-maintenance drift.** While both implementations exist, the Rust port is the *feature-complete* target; Python is frozen for bugfixes only after P3 (parity). No new features land in Python after P3 unless they are also ported in the same change.
8. **No legacy shims in Rust.** No `google_api_key` alias (config is the post-2026-08-06 schema: `simulator.provider`/`mode`/`api_key` — plan template PLAN-20260806-openai-caller.md §Migration); no `gemini_text` delivery naming (that rename is tracked as a Python-side migration; Rust implements the *current* schema only, see Open question 3).
9. **No streaming judge / transcription features.** LiveKit transcription text-stream plumbing is `VERIFY`-level in the Rust SDK (research §1.3); the port derives transcripts from audio + session-observer text streams exactly like Python's `Observer` (R4 `livekit/observer.py` consumes both `lk.transcription` text streams and `TranscriptionReceived`-style events), so no new SDK feature is required. Do not build on unverified SDK events. (REVISED: R4 `observer.py` registers `register_text_stream_handler("lk.transcription", …)` and reads attrs `lk.transcription_final`/`lk.segment_id`; Rust `TextStreamOpened` carries the same topic + attributes — the dual-path note below stands.)
10. **Windows release binary not promised in v1** (research §1.6 VERIFY). `install.ps1` adaptation is a P10 *documented* item, gated on a Windows CI build; if not proven, `install.ps1` keeps installing the Python zip until a Rust Windows binary exists. Rust code must still compile-check on Windows (CI `cargo check` job) so the future switch is cheap.

---

## Invariants (must hold after every phase; each is enforced by a golden/CI test from P1 on)

| ID | Invariant |
|----|-----------|
| I1 | **Report format byte-compatible.** `events.jsonl` envelope identical: field order `event_id, seq, run_id, turn, kind, ts, ts_mono_ms, datetime_utc, datetime_local, source, parent_event_id, spec` (dialogue snapshot injected before `spec` when `include_dialogue` — R4 `event_writer.py:108–123`); `event_id` = `evt_` + 12 hex (uuid v4 first 12 hex chars); `seq` monotonic from 1; `ts` epoch ms; `ts_mono_ms` = ms since run start (std::time::Instant origin), backdatable with `max(0, …)` clamp; `datetime_utc` ISO-8601 ms with `Z`; `datetime_local` ISO-8601 ms with tz offset (`chrono-tz`, default UTC); `spec` `{}` when none. `summary.json` key-for-key identical (R4 `metrics.py` key set — full 31-key `metrics` block incl. `talk_ratio`, `agent_chars`, `user_chars`, `user_words_*`, `slow_turns_*`, verified against R5); `meta.json` shape identical (redacted). Verified by byte-diff golden tests against Python-written fixtures. |
| I2 | **runs.sqlite DDL byte-identical** (R4 `sqlite_store.py` SCHEMA: `runs`, `run_events` PK `(run_id,seq)` + `idx_run_events_kind`, `run_turns` PK `(run_id,turn)`; `run_events.payload_json` = FULL envelope JSON, not just spec) so Python `list_runs`/`get_run_log`/`compare_runs` read Rust-written DBs and vice versa. Cross-read test in P3. |
| I3 | **Config compatible.** `config.yaml` parsed with the post-2026-08-06 schema only (`livekit`, `simulator{provider, mode, api_key, language, voice}`, `observe`, `judge`, `project`, `cues`, `telephony`); `_require` fail-fast semantics preserved; defaults portable (`en-US`/`UTC`). Rust must reject unknown `provider`/`mode` values with actionable errors. |
| I4 | **Scenario compatible.** Both legacy `.jsonl` (agent-sim/v1) and canonical `.yaml` parse to identical `Scenario` structs; `convert` re-emits YAML from JSONL; golden tests against `templates/scenario-scaffold.*`, `smoke-hello.*`, `inbound-caller-sim.*`, `outbound-callee-sim.*`, `outbound-human-pickup.*` (R6). |
| I5 | **Black-box rule.** Core never interprets consumer-specific dispatch metadata keys (passed through as JSON strings, Python `Dispatch.metadata`). No consumer env vars in config schema. Test: `dispatch_metadata_set` snapshot flag semantics preserved (R5). |
| I6 | **Portable defaults.** Core defaults `en-US`/`UTC`; timezone for `datetime_local` comes from the same default as Python (UTC) unless target config overrides. No host timezone leaks into artifacts (byte-compat requires deterministic `datetime_local` — golden tests pin it). |
| I7 | **One clear API.** CLI commands, MCP tool names/params/JSON return shapes identical to Python surface (21 MCP tools, 22 CLI commands listed in §CLI/MCP surface — REVISED counts, verified against `mcp_server.py` `@mcp.tool` markers and `cli.py` `@app.command` decorators). No aliases. Enforced by a tool-surface golden test (P4/P5). |
| I8 | **Caller brain parity.** `simulator.provider: google` → Gemini bridge, `openai` → OpenAI bridge; event kinds `sim.gemini_*` / `sim.openai_*` emitted with identical spec fields; reconnect/transport-drop semantics ported exactly — with one deliberate, documented divergence: Python *never* reconnects mid-call (sets `transport_dropped`), while `gemini-live` auto-reconnects on both GoAway and ConnectionLost (REVISED — see D4 and Open question 2). |
| I9 | **SimLeg topology parity.** `caller.mode` (`webrtc_sim`, `inbound_sip`, `outbound_sim_callee`, `outbound_human_pickup`, `agent_dials`) selects the SimLeg (REVISED — the five-mode set from R4 `scenario.py`; the plan previously dropped `agent_dials`); `simulator.mode`/`provider` are simulator capabilities, never scenario overrides (PLAN-20260806-openai-caller.md I7). |
| I10 | **Same scenario → comparable artifacts.** Running the same scenario id under Python and Rust against the same target yields `compare_runs`-compatible reports (cross-validation strategy §7). `compare_runs`/`compare --baseline` gate semantics identical. |

---

## Feature planning

### Architecture

**Workspace** (D1 — single crate): `src/livekit_agent_simulator_rust/`:

```
Cargo.toml            # workspace root: resolver = "2", members = ["crates/lks"], rust-version = "1.97"
rust-toolchain.toml   # 1.97.1 (matches livekit/rust-sdks pin, research §1.1)
crates/lks/
  Cargo.toml          # name = "lks", bin "lks" + lib
  src/main.rs         # thin: clap parse → tokio::main → lib entry
  src/lib.rs          # pub mod … (library surface; bin re-exports)
  src/config.rs       # Config structs + load + _require semantics + redaction
  src/scenario.rs     # Scenario dataclass mirror (locked fields, §5.3)
  src/scenario_yaml.rs# YAML parser (canonical)
  src/scenario_jsonl.rs # legacy JSONL parser (agent-sim/v1)
  src/scenario_convert.rs # convert: JSONL → YAML re-emit
  src/scenario_from_run.rs # promote finished run → draft YAML (P6)
  src/scenario_from_dict.rs # in-memory dict execution (P4)
  src/script/mod.rs   # script models (agent_speaking/delay_ms/delivery/say/…)
  src/script/parse.rs # script_parse.py port
  src/script/runtime.rs # ScriptRunner: cue timing, barge, hold, mute, DTMF (LocalParticipant::publish_dtmf — REVISED)
  src/script/farewell.rs  # farewell heuristics
  src/script/hang_up_gate.rs # hang_up_gate.py port
  src/script/verify.rs # script_verify counters
  src/script/summary.rs    # script summary.md
  src/callers/mod.rs       # CallerBridge trait + factory
  src/callers/base.rs      # shared: mic publish, watch agent tracks, end_call, transport_dropped, play_pcm
  src/callers/gemini.rs    # Gemini Live bridge (vendored gemini-live)
  src/callers/openai.rs    # OpenAI Realtime bridge (hand-rolled WS)
  src/callers/end_call.rs  # end_call.py heuristics
  src/caller_nudge.rs  # nudge_caller_after_agent_greeting
  src/interrupt_rate.rs
  src/livekit/mod.rs        # room, dispatch, observer facade
  src/livekit/room.rs       # Room connect, events (livekit crate)
  src/livekit/dispatch.rs   # AgentDispatchClient + RoomServiceClient (livekit-api)
  src/livekit/observer.rs   # track audio + lk.transcription text streams → transcripts
  src/livekit/agent_session_observer.rs # lk.agent.session byte streams
  src/livekit/sim_leg/mod.rs  # SimLeg trait + factory (5 modes — REVISED)
  src/livekit/sim_leg/webrtc.rs  # webrtc_sim leg (P2 killing test)
  src/livekit/sim_leg/inbound.rs  # inbound_sip (P6)
  src/livekit/sim_leg/agent_dials.rs  # agent_dials (P6) — REVISED: was mislabeled outbound_callee
  src/livekit/sim_leg/human_pickup.rs # outbound_human_pickup (P6)
  src/livekit/sim_leg/sim_callee.rs   # outbound_sim_callee helpers (P6)
  src/livekit/sim_leg/room_resolve.rs # room naming + prepare_ms (P6)
  src/audio/mod.rs
  src/audio/mic_mixer.rs   # ParallelMicMixer
  src/audio/local_recorder.rs # conversation.wav (16 kHz PCM16 stereo L=sim/R=agent — REVISED) + sim audio recorded events
  src/audio/pcm_cue.rs     # room_pcm cue WAV play + gain
  src/audio/cue_catalog.rs # builtin:voice.* / noise.* / legacy aliases + resolution
  src/audio/vad.rs         # RMS onset (agent audio) + silence detection
  src/audio/sapi_tts.rs    # sapi delivery (subprocess edge — see P3 note; non-Windows error parity)
  src/logging/mod.rs
  src/logging/event.rs     # EventKind enum (union of Python kinds) + envelope writer
  src/logging/sqlite.rs    # SCHEMA-identical DDL + RunStore
  src/logging/summary.rs   # summary.json writer
  src/logging/meta.rs      # meta.json writer (redacted config_snapshot)
  src/metrics.rs           # metrics.py key set (full 31-key metrics block — REVISED)
  src/run.rs               # run_orchestrator.rs port (phases, dispatch, end_condition)
  src/asserts.rs           # asserts.py port
  src/suite.rs             # suite.py port (evaluate_run_result, gate)
  src/ops.rs               # ops.py port: 21 MCP-mirror functions (sync + async)
  src/cli.rs               # clap app: 22 commands, --json, exit codes (0/1/130 — REVISED)
  src/cli_render.rs        # human renderers
  src/mcp/mod.rs           # rmcp server: 21 #[tool]s
  src/web/mod.rs           # axum app: static embed + /api/v1 routes
  src/web/cues.rs          # cues.json + source_priority/windows
  src/web/markers.rs       # markers
  src/web/report_time.rs   # report_time.py
  src/web/transcript_cues.rs
  src/web/speech_origin.rs
  src/web/tool_events.rs
  src/evals/mod.rs         # judge backends + runner (P7)
  src/evals/backends.rs    # gemini / http_openai / http_anthropic
  src/optimize/mod.rs      # persona optimizer (P9)
  src/plugins/mod.rs       # pyo3 loader for existing .py verify plugins (P8)
  src/plugins/api.rs       # plugin API (before_run/after_run/verify)
  src/preflight.rs         # preflight.py
  src/behavior_compile.rs  # caller policy compile (P6)
  src/persona_traits.rs    # prompt composition (P6)
  src/caller_policy.rs     # DefaultCallerPolicy + prompt_sections
  src/paths.rs             # .agent-sim/ path resolution
  src/portable_layout.rs   # portable defaults validation
  src/authoring.rs         # authoring.py port (scaffolds, GUIDE text)
  src/errors.rs            # ConfigError / ScenarioError / RunError
  tests/                   # integration + golden tests (see §Test strategy)
  assets/                  # web/dist embedded via rust-embed (build-time)
```

**Python → Rust module mapping** (every Python module accounted for; evidence: repo layout, `wc -l` ~25.1 kLOC — REVISED, research R4):

| Python module | Rust module | Notes / evidence |
|---|---|---|
| `config.py` | `config.rs` | Schema §5.2; `_require` fail-fast; redaction |
| `scenario.py` | `scenario.rs` + `scenario_jsonl.rs` | Locked fields §5.3; legacy JSONL; CALLER_MODES 5-value set |
| `scenario_yaml.py` | `scenario_yaml.rs` | Canonical YAML; templates `scenario-scaffold.yaml` etc. |
| `scenario_from_run.py` | `scenario_from_run.rs` | Draft YAML from a finished run |
| `scenario_from_dict.py` | `scenario_from_dict.rs` | In-memory dict execution |
| `script/` (models, parse, runtime, verify, summary, farewell, hang_up_gate) | `script/*` | Cue timing; barge; hold; mute; verify counts; DTMF via `publish_dtmf` |
| `script_parse.py` | `script/parse.rs` | Script step schema §5.3 |
| `script_runner.py` | (re-export; no Rust counterpart — stable import path only) | AGENTS.md layout note |
| `run_orchestrator.py` | `run.rs` | run_id (`{NNN}-{slug}-{YYYYMMDD}-{HHMMSS}-{xxxx}`), seq from report dirs, phases, dispatch, end_condition, `sim.leg_error` mapping (R4:287) |
| `livekit/adapter.py` | `livekit/mod.rs` | wraps livekit-api clients; build_token/isolate_sip_handset/update_subscriptions etc. |
| `livekit/observer.py` | `livekit/observer.rs` | `lk.transcription` text streams + AudioStream(16k) → transcript events |
| `livekit/agent_session_observer.py` | `livekit/agent_session_observer.rs` | `lk.agent.session` byte streams (register_byte_stream_handler) |
| `livekit/sim_leg/*` (webrtc, inbound, agent_dials, human_pickup, sim_callee, room_resolve, factory, protocol, errors) | `livekit/sim_leg/*` | P2 webrtc_sim; P6 the rest |
| `callers/` (base, gemini, openai, factory, end_call) | `callers/*` | P2 killing test is gemini bridge; I8 |
| `caller/` (default_policy, policy, prompt_sections) + `persona_traits.py` | `caller_policy.rs` + `persona_traits.rs` | deterministic string composition, golden-testable |
| `caller_nudge.py` / `interrupt_rate.py` | `caller_nudge.rs` / `interrupt_rate.rs` | P3 |
| `audio/` (cue_catalog, degradation, local_recorder, mic_mixer, pcm_cue, sapi_tts, vad) | `audio/*` | hound WAV; rubato/soxr resample (research §3); sapi_tts = Windows SAPI subprocess (P3 note) |
| `logging/` (event_writer, sqlite_store) | `logging/*` | I1/I2; byte-identical envelope + DDL |
| `metrics.py` | `metrics.rs` | key set frozen (R4) |
| `asserts.py` | `asserts.rs` | P4 |
| `suite.py` | `suite.rs` | evaluate_run_result + gate |
| `web/` (server, api, cues, markers, report_time, speech_origin, transcript_cues, tool_events, cue_helpers/*) | `web/*` | axum + rust-embed; routes §5.4; REST surface (research §5.4) |
| `mcp_server.py` | `mcp/mod.rs` | 21 tools; rmcp 3.1.2 |
| `cli.py` + `cli_render.py` | `cli.rs` + `cli_render.rs` | 22 commands; clap |
| `ops.py` | `ops.rs` | 21 MCP-mirror ops + compare gate |
| `evals/` | `evals/*` | P7 |
| `optimize/` | `optimize/*` | P9 |
| `plugins/` | `plugins/*` | P8 (pyo3) |
| `preflight.py` | `preflight.rs` | P2 |
| `behavior_compile.py`, `paths.py`, `portable_layout.py`, `authoring.py` | `behavior_compile.rs`, `paths.rs`, `portable_layout.rs`, `authoring.rs` | P6/early |
| `scenario_from_dict.py`, `scenario_yaml.py` | covered above | — |

**Dependencies (chosen crates + versions, evidence research §1–§3):**

| Crate | Version | For | Evidence |
|---|---|---|---|
| `livekit` | 0.8.3 (features `rustls-tls-webpki-roots`) | realtime room/tracks/data/text; `LocalParticipant::publish_dtmf(SipDTMF)` for DTMF cues (REVISED — API verified in R1 `local_participant.rs:783`) | research §1.1 |
| `livekit-api` | 0.6.3 | room/agent_dispatch/sip services + access_token | research §1.4 |
| `gemini-live` | 0.1.8 (vendored/forked) | Gemini Live bridge — REQUIRES fork patch for `language_code` (REVISED) | research §2.3 (fallback §2.4) |
| `tokio` | 1.x (full) | runtime; SDK requires tokio | research §1.1 |
| `tokio-tungstenite` | 0.30 | OpenAI bridge + fallback WS | research §2.5 |
| `rusqlite` | 0.40 bundled (REVISED — was 0.37; 0.37 predates the current rusqlite line and tokio-rusqlite 0.7 pins `^0.37` but we use `spawn_blocking`, no wrapper) | runs.sqlite | research §3.1 |
| `rmcp` | 3.1.2 (server feature) | MCP server | research §3 |
| `rust-embed` | 8.12 (`debug-embed`) | web/dist (~200 KB — REVISED, measured) | research §5.4 |
| `axum` | 0.8 | web + REST API | research §3 |
| `serde`/`serde_json`/`yaml_serde` | 1.x / 1.x / 0.10.x | config/scenario/artifacts (yaml_serde is the official YAML-org fork of archived serde_yaml — REVISED) | research §3 |
| `clap` | 4.x (derive) | CLI | research §3 |
| `thiserror`/`anyhow` | 2.x/1.x | errors | research §3 |
| `uuid` (v4) | 1.x | event_id/run_id hex | research §3 |
| `jiff` (+ `chrono-tz` if tz offsets beyond UTC are needed) | 0.2.x | datetime_utc/datetime_local parity (REVISED: jiff strtime gives exact `%Y%m%d-%H%M%S` parity; `datetime_local` needs a tz-aware formatter — `chrono-tz` only, since run_id is pure UTC) | research §3 |
| `hound` | 3.5 | WAV read/write (cues, conversation.wav) | research §3 |
| `rubato` (or `soxr-sys` 0.1.3) | latest | resample room_pcm → 16 kHz / 24 kHz (REVISED: sim audio out is 16 kHz for the room; Gemini bridge needs 16 kHz in / 24 kHz out) | research §3 |
| `rand` | 0.9 | jitter/interrupt classes | research §3 |
| `log` + `env_logger` | 0.4/0.11 | logging | research §3 |
| `pyo3` | 0.27.x | plugins (P8) | research §3 |
| `ctrlc` | 3.x | Ctrl+C in `lks web`/`serve` | research §3 |

### Key decisions (with rationale)

- **D1 — Single crate, not lks-core + bin.** The Python package is one importable unit with module seams; the crate mirrors that. A core/bin split would add workspace versioning ceremony for zero runtime benefit at this size. If a future consumer wants the library, `pub mod` surface is already there. (Research §0/§4: no prior art exists — greenfield composition, so no ecosystem constraint forces a split.)
- **D2 — tokio runtime, full features.** `livekit` requires tokio (research §1.1: "Currently, Tokio is required to use this SDK"); `gemini-live` and `rmcp` are tokio-native. `tokio::time` (cue timing), `tokio::process` (judge subprocesses, sapi TTS), `tokio::sync` (mpsc for the event writer) are all needed.
- **D3 — `livekit` + `livekit-api` for realtime + server.** No gap vs Python's `livekit.api` surface (research §1.4). TLS via `rustls-tls-webpki-roots` for a self-contained binary (research §1.2).
- **D4 — Gemini Live via vendored `gemini-live` 0.1.8 (Candidate B), fallback hand-rolled (Candidate C).** REVISED: the crate covers every wire feature (setup with realtimeInputConfig/activity handling, session resumption, GoAway, reconnect, transcript events, typed `ServerEvent`) but has **two known deltas vs Python**, resolved by vendoring + a ~2-field patch: (1) `SpeechConfig`/`VoiceConfig` has **no `language_code` field** — the Python caller sets `speech_config.language_code=voice.language`; add one serde `camelCase` field (`language_code`) to the vendored `VoiceConfig`/`PrebuiltVoiceConfig`. (2) **Reconnect policy cannot distinguish GoAway (wanted) from mid-call ConnectionLost (Python deliberately does NOT reconnect)** — `ReconnectPolicy { enabled: true, max_attempts: Some(10) }` auto-reconnects on both. Decide at P2 kickoff (Open question 2): fork-patch a `reconnect_on_drop: bool` knob (exact Python parity), or accept auto-reconnect as strictly-more-robust and emit `sim.gemini_socket_drop` + `transport_dropped` when `SessionStatus` goes `Reconnecting` without a preceding `GoAway` event. The latter avoids the fork patch; Python parity requires it. Fallback if the fork patch surface grows: raw tokio-tungstenite client (~600–900 lines, Candidate C). OpenAI Realtime stays hand-rolled on tokio-tungstenite (Python precedent: `callers/openai.py` uses raw `websockets`; research §2.5).
- **D5 — `rusqlite` bundled, not sqlx.** Fixed DDL (3 tables) + small sync writes; sqlx's async + macro build step adds nothing (research §3.1). REVISED: use rusqlite **0.40** bundled behind a thin `spawn_blocking` wrapper (not tokio-rusqlite — it pins rusqlite `^0.37`).
- **D6 — `rmcp` 3.1.2 for MCP.** Official SDK; `#[tool]` macros; MCP spec 2026-07-28. Tool surface parity is a golden test, not a code-gen concern.
- **D7 — Report format reuse (byte-compatible).** Full envelope/DDL/summary parity per I1/I2, from R5 ground truth + R4 sources (research §5.1).
- **D8 — Web embed via rust-embed 8.12** with `debug-embed` so `cargo run` and the release binary both serve `web/dist`; axum serves static + `/api/v1` (research §5.4). Web player JS unchanged.
- **D9 — JSONL scenario format reuse.** Both legacy `.jsonl` and `.yaml` parsed to identical structs; `convert` re-emits YAML (research §5.3). Golden tests from `templates/`.
- **D10 — Plugin strategy: pyo3 embedded CPython running the existing `.py` verify plugins unchanged** (research §3). Deliberate: target repos have `.agent-sim/plugins/*.py` files that call `livekit_agent_simulator.plugins.api` — rewriting them in Rust is impossible (they are consumer Python). pyo3 0.27 embeds a Python 3.11+ interpreter into the binary; the plugin API is a thin bridge. Risk-mitigated: isolated behind `plugins/` module; core never imports Python. If pyo3 is unacceptable at P8 review, fallback is documented in P8.

### CLI/MCP surface (frozen contract — I7)

- **MCP tools (21 — REVISED, verified count of `@mcp.tool` markers in `mcp_server.py`):** `guide, web, init_project, preflight, list_scenarios, list_plugins, list_cues, validate_scenario, export_scenario, init_scenario, convert_scenario, execute_scenario, optimize_persona, execute_scenarios, execute_scenario_dict, scenario_from_run, get_run_status, get_run_log, get_run_report, compare_runs, list_runs` (research §5.5). No 22nd tool exists.
- **CLI commands (22 — REVISED, verified against `@app.command` decorators in `cli.py`):** `init, guide, web, serve, preflight, scenarios, cues, plugins, validate, export, convert, scenario-init, execute, execute-all, execute-dict, optimize, status, log, report, compare, runs, scenario-from-run` + the `mcp` subcommand (a 23rd command). There is **no `version` command** — `lks --version` comes from typer's built-in flag (clap gives it via `#[command(version)]`). The CLI has no hidden `--as-json` flag; the flag is `--json` (typer Option string), with the Python parameter named `as_json` internally to avoid shadowing the stdlib — Rust needs only the `--json` flag.
- **Exit codes (REVISED — verified `cli.py` `_run` helper):** 0 success, 1 failure/gate-fail/ConfigError/ScenarioError/RuntimeError, **130 on Ctrl+C** ("Interrupted — stopping."), not 2. No code-2 usage errors.
- **REST API (`web/api.py`):** REVISED — the surface is the full 10-route set under `/api/v1`: GET `/health` (→ `{ok, version, root}`), GET `/runs`, GET `/runs/<id>`, GET `/runs/<id>/report`, GET `/scenarios`, GET `/scenarios/<id>`, POST `/validate`, POST `/execute`, POST `/preflight`. (The plan previously listed only 6 of the 10 routes.)

### Phased milestones

#### P0 — Scaffold + CI (offline gate)
- **Scope:** workspace + CI that builds and tests; rust-toolchain pin; dependency baseline.
- **Files:** `Cargo.toml` (workspace), `rust-toolchain.toml` (1.97.1), `crates/lks/Cargo.toml`, `crates/lks/src/main.rs` (prints `lks 0.1.0-rust`), `crates/lks/src/lib.rs`, `crates/lks/src/errors.rs`, `.github/workflows/rust-ci.yml`, `.gitignore` additions (`src/livekit_agent_simulator_rust/target/`, `.agent-sim/` already ignored).
- **Steps:**
  1. `cargo init` the workspace; set resolver 2, `rust-version = "1.97"`.
  2. Add base deps: `clap`, `serde`, `serde_json`, `thiserror`, `anyhow`, `log`, `env_logger`, `uuid`, `jiff`, `rand`.
  3. `crates/lks/src/main.rs` parses `--version`/`--help` (clap `#[command(version)]` — the CLI has no `version` subcommand); `lib.rs` exposes `pub mod errors`.
  4. CI workflow: `cargo fmt --check`, `cargo clippy -D warnings`, `cargo test`; matrix `ubuntu-latest`, `macos-latest` (aarch64); `windows-latest` runs `cargo check` only (research §1.6 VERIFY: no Windows release promise in v1, but compile-check keeps the door open). Use GH Actions `actions/cache` for `~/.cargo` + `target/` (research §1.6: first build is long).
- **Tests:** `cargo test` with one smoke test `cli_smoke_prints_version` (asserts binary runs). CI gate: `cargo fmt/clippy/test` green.
- **Deps added:** clap 4.x, serde/serde_json, thiserror/anyhow, log/env_logger, uuid 1, jiff 0.2.x, rand 0.9.

#### P1 — Config + scenario parsing (offline gate; golden tests)
- **Scope:** `Config` load/validate/redact; `Scenario` parse from YAML + legacy JSONL; `convert`; `validate`/`export`/`list_scenarios` ops; `init_project`/`init_scenario` scaffolds.
- **Files:** `crates/lks/src/config.rs`, `crates/lks/src/scenario.rs`, `crates/lks/src/scenario_yaml.rs`, `crates/lks/src/scenario_jsonl.rs`, `crates/lks/src/scenario_convert.rs`, `crates/lks/src/authoring.rs` (scaffolds), `crates/lks/src/paths.rs`, `crates/lks/src/ops.rs` (start), `crates/lks/tests/golden_config.rs`, `crates/lks/tests/golden_scenario.rs`.
- **Steps:**
  1. Mirror `config.py` schema §5.2: `serde` structs with defaults; `_require` fail-fast (`ConfigError` with actionable message). Use `yaml_serde` 0.10.x (official YAML-org fork of the archived `serde_yaml` — REVISED).
  2. Mirror `Scenario` dataclass locked fields §5.3 (`id, path, locale, tags, persona, context, simulator{max_turns=6, timeout_s=120, first_speaker=agent}, execute{...}, dispatch{metadata}, caller{mode=webrtc_sim}, telephony{...}, pass_criteria, pass_judges, pass_criteria_mode, script_steps, script_verify, plugin_modules, asserts, behavior_spec, caller_policy`); caller mode validated against the 5-value CALLER_MODES (`webrtc_sim, inbound_sip, outbound_human_pickup, outbound_sim_callee, agent_dials` — REVISED).
  3. YAML parser: canonical format; unknown-key handling must match Python (fail or warn identically — read `scenario_yaml.py` and copy the decision).
  4. Legacy JSONL parser: `kind: Scenario/Persona/Context/Simulator/Execute/Dispatch/Script/PassCriteria/Behavior/Plugins/Assert/Caller/Telephony` lines (AGENTS.md Scenario JSONL section; KNOWN_KINDS from R4 `scenario.py`) → same `Scenario`.
  5. `convert_scenario` re-emits YAML from JSONL (keep `.jsonl`; idempotent; `force`).
  6. Scaffolds: `init_project` (`.agent-sim/config.yaml` + dirs, from `templates/config.yaml` embedded via rust-embed or `include_str!` — pick `include_str!` for templates since they are few KB; research §5.4 covers web only) and `init_scenario` (`#` guide comments + example sections, matching `templates/scenario-scaffold.yaml`).
  7. Ops: `list_scenarios` (id, tags, validity), `validate_scenario`, `export_scenario` (parsed JSON) with Python-identical JSON shapes.
- **Tests (golden):** parse `templates/scenario-scaffold.yaml`, `smoke-hello.yaml`, `inbound-caller-sim.yaml`, `outbound-callee-sim.yaml`, `outbound-human-pickup.yaml` and their `.jsonl` twins → assert struct equality; golden snapshot of `export_scenario` JSON vs Python output for the same template; `convert` idempotency; config golden: parse `templates/config.yaml` + one with `simulator.provider: openai`; redaction test: `config_snapshot` never contains `api_key`/`api_secret` values (R5). Test names: `golden_scenario_yaml_matches_python`, `golden_scenario_jsonl_matches_yaml`, `convert_roundtrip_idempotent`, `config_require_fails_fast`, `config_snapshot_redacts_secrets`.
- **Acceptance gate:** offline (CI): all P1 tests green; Python `lks validate`/`lks export` output for the same root matches byte-for-byte where deterministic (JSON key order preserved via `serde_json` `preserve_order` feature — VERIFY: Python `json.dumps` preserves dict insertion order, so Rust must too; add `preserve_order` to serde_json features and confirm in golden test).
- **Deps added:** yaml_serde 0.10.x, serde_json (preserve_order), (rusqlite deferred to P3).

#### P2 — Room + dispatch + Gemini caller POC (killing test: one webrtc_sim run end-to-end with events.jsonl)
- **Scope:** the vertical slice that makes the port real: `lks execute <scenario> --root <target>` against a real LiveKit room with a real Gemini Live caller, writing a valid `events.jsonl`. Also `preflight`.
- **Files:** `crates/lks/src/livekit/mod.rs`, `crates/lks/src/livekit/room.rs`, `crates/lks/src/livekit/dispatch.rs`, `crates/lks/src/livekit/sim_leg/mod.rs`, `crates/lks/src/livekit/sim_leg/webrtc.rs`, `crates/lks/src/callers/mod.rs` (trait + factory), `crates/lks/src/callers/gemini.rs`, `crates/lks/src/callers/base.rs` (minimal: mic publish + play_pcm), `crates/lks/src/run.rs` (run_id, seq, phases: connect → dispatch → agent_join wait → caller start → end_condition), `crates/lks/src/preflight.rs`, `crates/lks/src/audio/mic_mixer.rs`, `crates/lks/src/audio/pcm_cue.rs` (minimal), `crates/lks/src/logging/event.rs` (writer only, no sqlite yet), `crates/lks/tests/mock_ws.rs` (fake Gemini server), `crates/lks/tests/run_orchestrator.rs`.
- **Steps:**
  1. Vendor `gemini-live` 0.1.8 at kickoff (research §2.2 VERIFY → resolved: `cargo add` is replaced by vendoring; grep confirms `BidiGenerateContent`/`live` in the crate). Apply the fork patch: add `language_code` to the vendored `VoiceConfig`/`PrebuiltVoiceConfig` (serde `camelCase`) and decide the reconnect-policy knob per D4/Open question 2. Verify `ServerEvent::ModelAudio` is 24 kHz raw PCM and `setup` accepts `outputAudioTranscription`/`inputAudioTranscription` (expected PASS — same proto as Python `google-genai`).
  2. Room: `Room::connect(url, token, RoomOptions)`; subscribe `RoomEvent`; track `TrackSubscribed` for agent audio (publish an audio source at 16 kHz mono — REVISED: Python `callers/base.py` publishes `rtc.AudioSource(16000, 1)`), `ParticipantConnected/Disconnected`, `RoomDisconnected`, `DataPacketReceived`, `TextStreamOpened` (research §1.3).
  3. Token: `livekit-api` access_token (HS256 via in-crate HMAC provider; research §1.4).
  4. Dispatch: `AgentDispatchClient` create dispatch with scenario `Dispatch.metadata` (opaque pass-through, I5); emit `dispatch.created` (spec: room, agent_name, dispatch_id, metadata_set, mode — R5 sample).
  5. Caller brain: `GeminiCallerBridge` — `setup` message (model from `voice.model`, systemInstruction from persona prompt, speech_config incl. the patched `language_code` from `voice.language`, realtimeInputConfig with activity handling per research §2.3), base64 PCM in `realtimeInput` for agent audio from `rtc::AudioStream` (16 kHz mono — REVISED: `AudioStream(track, sample_rate=16000, num_channels=1)` in R4 `observer.py:298`; the 24 kHz path is the *model output*, consumed by `_play_pcm`), `_play_pcm` for `ModelAudio` into mic mixer → `AudioSource`, `InputTranscription`/`OutputTranscription` → dialogue, `Interrupted` → interruption event, `GoAway`/reconnect per D4 (I8; incl. 1008-policy-violation retryable drop — research §2.1 + R4).
  6. Event writer (minimal): envelope per I1, kinds for the slice: `run.started, dispatch.created, room.participant_connected, room.track_subscribed, sim.gemini_connected, sim.mic_published, sim.agent_audio_bridged, sim.gemini_activity, transcript.user.final, transcript.agent.final, sim.script.*` (stub), `run.end_condition, run.ended`.
  7. Run orchestration: `new_run_id` (`{NNN}-{slug}-{YYYYMMDD}-{HHMMSS}-{xxxx}`, seq from report dirs — R4 run_orchestrator.py:52–104), report dir under `<target>/.agent-sim/reports/<run_id>/`, end_condition loop (max_turns/timeout_s/script end/dead-call), `run.ended` with summary_digest.
  8. `preflight`: config check + LiveKit connectivity (room list via livekit-api).
- **Tests (offline):** `mock_ws` fake Gemini server (accepts `setup`, serves `SetupComplete` + scripted `ModelText`/`ModelAudio` frames) → assert: setup payload shape (incl. patched `language_code`), `sim.gemini_connected` emitted, audio frames played into mixer, `transcript.user.final` on `InputTranscription`, interruption path, reconnect on transport drop (fake close + server reaccept), events.jsonl lines parse and envelope fields correct (schema-assert, not byte-golden yet — ts fields are time-dependent; golden-test the *shape* and the deterministic fields `seq`, `kind`, `spec`). Names: `gemini_setup_payload_matches_python`, `gemini_audio_plays_into_mic`, `gemini_transcript_events`, `gemini_transport_drop_reconnects`, `events_envelope_shape`, `run_id_format`, `report_dir_seq_increments`.
- **Acceptance gates:** (a) **offline/CI**: the above green. (b) **MANUAL** (needs live creds — not a CI gate): real `lks execute` against the repo's own demo target (`demo/dtmf-feature/.agent-sim` is a real consumer) or any LiveKit agent; report dir contains parseable `events.jsonl` with `sim.gemini_connected`; `lks log <run_id>` renders. This is the P2 **killing test**: "one webrtc_sim run end-to-end with events.jsonl" — do not proceed to P3 until it passes.
- **Deps added:** livekit 0.8.3 (rustls-tls-webpki-roots), livekit-api 0.6.3, gemini-live 0.1.8 (vendored), tokio full, tokio-tungstenite 0.30, hound, rubato/soxr-sys.

#### P3 — Audio pipeline + observer + event writer + sqlite + summary (report parity)
- **Scope:** full audio pipeline (mic mixer, local recorder, PCM cues, VAD/silence detection, degradation), observer (agent audio → transcripts + `lk.agent.session` byte streams), full event-kind set, SQLite RunStore, summary.json/meta.json/timeline.md/review.md writers, `get_run_log`/`get_run_status`/`list_runs` ops. **Parity gate.**
- **Files:** complete `crates/lks/src/audio/*`, `crates/lks/src/livekit/observer.rs`, `crates/lks/src/livekit/agent_session_observer.rs`, `crates/lks/src/logging/sqlite.rs`, `crates/lks/src/logging/summary.rs`, `crates/lks/src/logging/meta.rs`, `crates/lks/src/metrics.rs`, `crates/lks/src/caller_nudge.rs`, `crates/lks/src/interrupt_rate.rs`, `crates/lks/tests/parity_events.rs`, `crates/lks/tests/parity_sqlite.rs`, `crates/lks/tests/parity_summary.rs`, `crates/lks/tests/golden_kinds.rs`.
- **Steps:**
  1. EventKind enum: union of every kind enumerated in research §5.1 (run.*, room.*, dispatch.*, transcript.*, tool.start, assert.verify, script.verify, judge.verdict, sim.* incl. all sim.gemini_*/sim.openai_* wire frames, interruption, cue kinds) — REVISED: include `sim.script.dtmf` (R4 `script/runtime.py:294`), `sim.script.hang_up_deferred`, `session.*` (`agent_state`, `user_state`, `usage`, `error`, `overlapping_speech`, `debug`, `chat_history`, `tool_execution`), `handoff`, `sim.agent_greeted_nudge(_skipped)`. `EventKind` accepts the full union; `serde` to the exact string.
  2. Writer completes: `include_dialogue` snapshot logic (dialogue inserted before `spec`), `parent_event_id`, `datetime_local` via `chrono-tz` (default UTC — I6), `ts_mono_ms` from `Instant` origin, flush-on-write to `events.jsonl` (append, utf-8, one JSON object per line — Python opens `"a"` mode, R4 event_writer.py:57).
  3. SQLite: DDL byte-identical (copy the SCHEMA string verbatim — case + whitespace; I2); `create_run/append_event/update_turn/finalize_run` mirroring `sqlite_store.py`; sqlite connection per run with `busy_timeout` (Python aiosqlite default is fine — keep same semantics); `run_events.payload_json` = the FULL envelope JSON (not just spec) — REVISED per R4 `sqlite_store.py` insert.
  4. `metrics.rs`: compute the R4 key set — REVISED: the full 31-key `metrics` block verified in R5 (`schema, turn_taking_ms, ttfw_ms, ttfw_source, recovery_ms, barge_count, barges_recovered, barge_recovery_rate, interruption_count, silence_events, agent_finals, user_finals, tool_calls, tool_errors, tool_error_rate, talk_ratio, agent_chars, user_chars, user_words_count, user_words_p10/p50/mean, user_words_natural_count/p10/p50/mean, user_words_script_count/p50/mean, slow_turns_over_2500ms, slow_turns_over_5000ms`); `metrics_digest` for `compare_runs`.
  5. `summary.json` writer: exact key order from R5 ground truth (top-level `run_id, status, duration_ms, turn_count, event_count, turn_taking_ms, metrics, tool_calls, tool_errors, interruptions, silences, verdict, turns` + `caller_mode, end_reason, dial_ms` and `script_verify, assert_verify, caller` merged from summary_extra — REVISED: R5 `summary.json` has 13 top-level keys without the summary_extra ones; the extra keys appear only when the events carry them, so port the merge logic verbatim from `event_writer.finalize`); `caller.behavior_summary` included.
  6. `meta.json`: scenario id, room name, agent_name, redacted config snapshot (R5 — REVISED: `config_snapshot` also carries `judge: {model, http, endpoint_type, api_key_set} | None` and `observe_gaps: [str]`; verified in R5 meta.json).
  7. `timeline.md`/`review.md`: deterministic regeneration (research §5.1: human-readable, not byte-locked, but golden-testable).
  8. Observer: `lk.transcription` text streams (register via `TextStreamOpened`, topic == "lk.transcription", read `lk.transcription_final`/`lk.segment_id` attributes) → `on_transcript`; `lk.agent.session` byte streams → agent session state (research §1.3); AudioStream(16 kHz mono) → agent PCM recording + RMS onset (`sim.agent.audio_onset` with backdated `ts_mono_ms` — REVISED: R4 `observer.py` computes `audio_t0_mono = (recorder.started_mono − writer.t0_mono)*1000` and `ts = max(0, audio_t0_mono + onset_ms)`).
  9. Audio: `ParallelMicMixer` (room_pcm cues + caller voice mixing with gain), `LocalConversationRecorder` (**conversation.wav at 16 kHz** PCM16 stereo L=sim/R=agent — REVISED: R5 `meta.json` audio shows `sample_rate: 16000`; the plan previously said 24 kHz), VAD (silence detection for dead-call + OpenAI path), `sapi_tts` via `tokio::process` (Windows SAPI; on non-Windows it must fail identically to Python — check `sapi_tts.py` behavior and mirror; likely a clear error, not a silent no-op).
  10. `get_run_log`/`get_run_status`/`list_runs`/`compare_runs` read paths with Python-identical JSON shapes.
- **Tests (parity):** the critical suite:
  - `parity_events_bytes`: run the Python package's own test fixtures (`tests/test_event_writer.py` vectors) through the Rust writer → byte-identical `events.jsonl` (deterministic fields; ts fields normalized by seeding).
  - `parity_sqlite_cross_read`: write `runs.sqlite` from Rust, read with Python `sqlite_store.RunStore` (via a small `uv run python -c` harness in CI) → same rows; and vice versa (Rust reads a Python-written DB).
  - `parity_summary_keys`: summary.json key set + order matches R5 ground-truth file.
  - `golden_kinds`: every EventKind string serializes to exactly the Python string (incl. `sim.gemini_socket_drop`, `sim.openai_socket_drop`, `sim.gemini_resumption_handle`, `sim.script.dtmf`).
  - `metrics_values`: feed a scripted events log → identical metrics dict vs Python `metrics.py` (port the Python unit test vectors).
- **Acceptance gates:** (a) **offline/CI**: parity suite green. (b) **MANUAL**: re-run the P2 real run and diff `events.jsonl`+`summary.json` against a Python-produced report for the same scenario (same target, same day, non-deterministic fields normalized: ts, run_id) — "report parity with Python" gate. Also MANUAL: `lks web <run_id>` serves the Rust-written report and plays audio.
- **Deps added:** rusqlite 0.40 bundled, chrono-tz, (hound/rubato already at P2).

#### P4 — Asserts + suite + CLI parity
- **Scope:** `asserts.py` port (tool asserts, transcript asserts, SIP asserts — R4 `asserts.py`), `suite.py` (`evaluate_run_result`, gate, suite matrix + suite-*.json/md), full CLI (all 22 commands) with `--json` + exit-code semantics, `cli_render` human renderers, `execute-dict`, `execute-all` (parallel + wait).
- **Files:** `crates/lks/src/asserts.rs`, `crates/lks/src/suite.rs`, `crates/lks/src/cli.rs` (complete), `crates/lks/src/cli_render.rs`, `crates/lks/src/ops.rs` (execute paths complete), `crates/lks/tests/cli_parity.rs`, `crates/lks/tests/asserts.rs`, `crates/lks/tests/suite_gate.rs`.
- **Steps:**
  1. Asserts: port each assert kind with identical spec payloads and `assert.verify` events; SIP asserts need `sim.leg_error`/telephony context (P6 data is stubbed in tests).
  2. Suite: `evaluate_run_result(result, strict_judge)` returning the same `gate` dict; `execute-all` parallel workers (tokio tasks, `--parallel`, `--wait` cooldown) writing `suite-*.json/md`.
  3. CLI: clap derive for all 22 commands, flags identical (incl. `--repeat`/`-n`, `--pass-at-k`/`-k`, `--name`, `--agent-name`, `--optimized`, `--strict-judge`, `--no-report`, `--parallel`/`-p`, `--wait`, `--baseline` + the four `--max-*-regression-ms` options, `--no-open`, `--host`/`--port`, `--no-connectivity`, `--resolve`, `--root`, `--json`); exit codes: 0 success, 1 failure/gate-fail (incl. ConfigError/ScenarioError/RuntimeError), 130 on Ctrl+C (REVISED — Python `_run` raises `typer.Exit(130)`; clap/`tokio::signal` must map to exit 130).
  4. Renderers: port `cli_render.py` output text (golden test against Python `--json`-driven runs where deterministic; human renderers are golden-tested for the table shape, not byte-locked — AGENTS.md one-clear-API means same *information*, cosmetic table width may differ; note in test doc).
- **Tests:** `cli_help_lists_all_commands` (22 commands + `mcp`), `cli_as_json_shapes`, `execute_dict_stdin`, `execute_all_parallel`, `suite_gate_fail_exits_1`, `asserts_emit_verify_events`, `compare_baseline_gate` (port test_baseline_compare.py vectors).
- **Acceptance:** offline/CI green; MANUAL: `lks execute --repeat 2` on demo target with gate behavior matching Python.

#### P5 — MCP server
- **Scope:** `rmcp` server exposing the same 21 tools with same names/params/return JSON.
- **Files:** `crates/lks/src/mcp/mod.rs`, `crates/lks/tests/mcp_tool_surface.rs`, `crates/lks/tests/mcp_client_harness.rs`.
- **Steps:**
  1. `#[tool]` per Python `@mcp.tool` function; param names exact (`project_root`, `scenario_id`, `run_id`, `repeat`, `pass_at_k`, `run_name`, `agent_name`, `optimized`, `strict_judge`, `tag`, `write_report`, `parallel`, `wait_s`, `held_out`, `candidates`, `max_candidates`, `name`, `force`, `connectivity`, `limit`, `kind`, `turn`, `source`, `since_mono_ms`, `run_id_a`/`run_id_b` + regression limits, `baseline`).
  2. Return shapes: port `ops.py` dicts verbatim (they are already JSON-serializable; Rust `serde` structs with `#[serde(rename_all = "camelCase")]` where Python uses camelCase — verify per op, e.g. `run_id`, `report_dir`).
  3. `lks mcp` subcommand spawns the server (stdio transport, same as Python's console script `lks-mcp`).
- **Tests:** `mcp_tool_surface`: list tools from both servers (Rust + a Python sidecar in CI, `uv run` — optional if CI lacks uv; alternative: golden JSON of tool list + param schemas) and assert name/param parity. `mcp_client_harness`: drive `init_project`/`list_scenarios`/`validate_scenario` through a real rmcp client against the in-process server.
- **Acceptance:** tool-surface golden test green (research §6: "Tool-surface golden test: run both `lks mcp` servers against a scripted client, diff tool lists + call shapes"); MANUAL: point Claude Code / any MCP client at the Rust server and run `guide` + `execute`.

#### P6 — Remaining sim legs (inbound_sip, outbound_sim_callee, agent_dials, outbound_human_pickup)
- **Scope:** `inbound_sip`, `outbound_sim_callee`, `agent_dials`, `outbound_human_pickup` (REVISED — the 4 non-webrtc legs from CALLER_MODES; the plan previously listed `outbound_callee` which is not a real mode), `sim_callee` (callee-side call handling), `room_resolve` (room naming + prepare_ms + wait-until-answered), telephony config checks.
- **Files:** `crates/lks/src/livekit/sim_leg/inbound.rs`, `agent_dials.rs`, `human_pickup.rs`, `sim_callee.rs`, `room_resolve.rs`, `crates/lks/src/telephony.rs` (config checks), `crates/lks/tests/sim_leg.rs`, `crates/lks/tests/telephony.rs`.
- **Steps:** port each leg using `livekit-api` `services::sip` (SIP participant creation, dialing — research §1.5); DTMF via `LocalParticipant::publish_dtmf(SipDTMF)` (REVISED — verified at R1 `local_participant.rs:783`; not `livekit::webrtc::SipDTMF` — the struct lives in `livekit::room` and `publish_dtmf` is on `LocalParticipant`); the demo `dtmf-feature` suite exercises `dtmf` script cues; leg events (`sim.leg_error`, hold_timeout, human_pickup semantics) per R4 `sim_leg/*`.
- **Tests:** `sim_leg_factory_selects_leg`, `inbound_sip_creates_sip_participant` (mock livekit-api HTTP), `outbound_dials_number`, `dtmf_cue_sends_sip_dtmf` (mock — assert `publish_dtmf(SipDTMF{code, digit})` payload maps `0-9*#w` per R4 `script/runtime.py` DMAP), port of `tests/test_sip_asserts.py` + `test_room_resolve.py` vectors. MANUAL: inbound/outbound real smoke against a SIP-enabled project (needs creds; gate: same events as Python run).
- **Acceptance:** offline green; MANUAL per-leg real run.

#### P7 — Judge / evals
- **Scope:** PassCriteria LLM judge (default Gemini via REST, `judge.endpoint_type: http_openai`/`http_anthropic` custom endpoints) + `evals/` (relevancy, evidence, aggregate, presets, runner) used by `optimize` (P9).
- **Files:** `crates/lks/src/evals/mod.rs`, `crates/lks/src/evals/backends.rs`, `crates/lks/src/judge.rs`, `crates/lks/tests/evals.rs`, `crates/lks/tests/judge_http.rs`.
- **Steps:**
  1. Judge backend: REST over `reqwest` (rustls — Open question 5) calling the configured endpoint with Python-identical request bodies and parsing identical verdict JSON; `judge.verdict` event spec parity.
  2. Custom endpoints: `http_openai`/`http_anthropic` — port `evals/backends/http_*.py` request/response translation exactly (this is the consumer-facing extensibility point; tests use a local mock HTTP server).
  3. `evals/`: port the pieces `optimize` consumes (evidence extraction, relevancy scoring, aggregate); `presets.py` golden strings.
- **Tests:** `judge_verdict_event`, `judge_http_openai_mock`, `judge_http_anthropic_mock`, `evals_relevancy_vectors`, `evals_aggregate_vectors` (port Python test_evals_judge.py). MANUAL: real judge run (needs API key).
- **Acceptance:** offline green; MANUAL judge smoke on a real scenario with `pass_criteria`.

#### P8 — Plugins (pyo3 or drop)
- **Scope:** run existing `.py` verify plugins (`before_run`/`after_run`/`verify` from `templates/plugins/example_verify.py` + `docs/plugins.md`) unchanged via embedded CPython.
- **Files:** `crates/lks/src/plugins/mod.rs`, `crates/lks/src/plugins/api.rs` (bridge exposing the same plugin API surface the .py files import), `crates/lks/tests/plugins_python.rs`.
- **Steps:**
  1. Add pyo3 0.27 (abi3, `extension-module` off; interpreter = embedded CPython 3.11+).
  2. Bridge: the plugin modules `import` names from `livekit_agent_simulator.plugins.api` — provide a Rust-initialized module with the same names/functions (verify signature `(events, run context) -> verify result` per docs/plugins.md).
  3. Loader: `plugin_modules` in scenario → `PyModule::from_code` + call `before_run`/`after_run`/`verify` with the same arguments Python passes (port `plugins/loader.py` + `plugins/registry.py` semantics).
  4. Decision point (documented, not blocking P8 start): if pyo3 embedding proves unacceptable (binary size, abi3 vs python version, CI image complexity), **drop plugin support from v1** and fail `validate_scenario` with a clear "plugins require the Python build" error when `plugin_modules` is set; document in README. AGENTS.md no-dead-features: a plugin that errors loudly beats a silently-skipped one.
- **Tests:** `plugins_python_executes_example` (run `templates/plugins/example_verify.py` against a synthetic events log → same verdict dict as Python), `plugins_missing_module_fails_validate`.
- **Acceptance:** offline green; MANUAL: run a scenario with `plugin_modules` against a real target and compare `script.verify`/plugin output vs Python.

#### P9 — Optimize
- **Scope:** `lks optimize` + `optimize_persona` MCP tool (persona-prompt optimizer: baseline + LLM-proposed variants, evaluate over dataset, held-out generalization check, winner selection, write `.agent-sim/optimized/<name>/prompt.yaml`, `--optimized` apply).
- **Files:** `crates/lks/src/optimize/mod.rs`, `crates/lks/src/optimize/variant.rs`, `crates/lks/src/optimize/eval.rs`, `crates/lks/src/optimize/apply.rs`, `crates/lks/src/optimize/mutate.rs`, `crates/lks/tests/optimize.rs`.
- **Steps:** port `optimize/` semantics; the LLM proposal/mutation calls go through the evals/judge HTTP layer (P7); variant evaluation = repeated `execute_scenario` with the candidate persona prompt; winner = beats baseline on the dataset AND passes held-out (research §4: no prior art; port the Python algorithm from `optimize/optimize.py` + `variant.py` — read them and mirror exactly).
- **Tests:** `optimize_no_winner_keeps_baseline`, `optimize_writes_artifact`, `optimized_flag_applies_prompt` (port test_optimize.py vectors with a fake eval backend).
- **Acceptance:** offline green; MANUAL: one real `lks optimize` run on the demo target (needs keys).
- **CUT check (AGENTS.md no-dead-features):** if the `optimize_persona` MCP tool or `lks optimize` CLI has no user flow at P9 time (no consumer uses it), this phase shrinks to `optimize` ops returning a clear "not supported in Rust build" error and the P9 acceptance becomes "error is explicit, tests cover it" — decide at P9 kickoff against the Python tool-usage log. Default recommendation: port it (it has an MCP tool + CLI + docs recipe = a documented user flow).

#### P10 — Packaging (install.sh/install.ps1 adaptation, cross-platform CI release)
- **Scope:** release pipeline (GH Actions release workflow: matrix build macOS aarch64/x86_64 + Linux x86_64; `cargo build --release`, strip, tar.gz/zip per platform, sha256sums, GH Release assets), `install.sh` adaptation (same curl|bash UX, download the Rust binary tarball instead of the Python zip; keep `lks-mcp` alias wiring), `install.ps1` (research §1.6 VERIFY gate: only if a Windows Rust build is proven in CI; else keep Python zip for Windows and document), `web/dist` embed at build time (rust-embed `debug-embed` dev / `compression` release — decide size vs speed; R5 web/dist is ~200 KB, measured — REVISED).
- **Files:** `.github/workflows/release.yml`, `scripts/release.sh`, `install.sh` (rewrite), `install.ps1` (rewrite or leave), `crates/lks/assets/` (web/dist copy step in build.rs or CI), `docs/portability.md` note.
- **Steps:**
  1. Release workflow: build + test on matrix (macos-14 aarch64, ubuntu-latest, windows-latest optional), upload artifacts, create GitHub release with checksums (repo has GH Releases precedent: install.sh references `v0.1.0` releases).
  2. Rewrite `install.sh` to fetch `<release>/lks-<os>-<arch>.tar.gz`, verify sha256, install to `$HOME/.local/bin` — REVISED: preserve the actual current flags: `DEST`, `INSTALL_ROOT`, `LK_SIM_REF`, `CURRENT_DIR` symlink semantics, `--easy-mode` (appends DEST to PATH in shell rc), and the `--with-mcp`/`--with-rust-mcp` flag set if present — read the current script and preserve every flag.
  3. Verify `lks mcp` stdio server works from the installed binary (MCP clients exec `lks-mcp`).
- **Tests:** CI smoke: installed-binary smoke (`install.sh` from the draft release on ubuntu/macos runner → `lks --version` + `lks guide`), `cargo test` release-profile pass. MANUAL: fresh-machine install + one real `lks execute`.
- **Acceptance:** release assets downloadable and installable on macOS + Linux; Windows documented (research §1.6).

### Cross-validation strategy (Python ↔ Rust)
1. **Golden artifacts from Python.** Before each parity phase, capture Python outputs (events.jsonl, runs.sqlite, summary.json, meta.json, export/validate JSON, MCP tool list) for the template scenarios and the R5 real run (`114-people-pleaser-refuse-card-20260809-201652-8b32`) as `tests/golden/` fixtures. Rust golden tests diff against these byte-for-byte where deterministic (I1/I2) and key-for-key where time-dependent (ts fields normalized via seed/freeze).
2. **Cross-read DB test (P3):** Rust-written `runs.sqlite` read by Python `RunStore`; Python-written read by Rust (CI job with `uv run` sidecar, or a committed fixture DB).
3. **compare_runs across implementations (P4+):** same scenario id + same target run under Python and Rust → both report dirs under one `.agent-sim/reports/`; `lks compare <py-run> <rust-run>` (from either implementation) shows only non-deterministic diffs (ts, run_id, wav bytes) and zero structural diffs; the `--baseline` gate (max_ttfw_regression_ms etc., default 1500/2000/30000/0) is exercised as a regression check, not a pass gate (caller model timing differs run-to-run).
4. **Suite-level parity (P4/P6):** `execute-all` over the template scenarios under both implementations → identical pass/fail verdicts for hard gates (script_verify/assert_verify), judge verdicts excluded from strict equality (LLM non-determinism) and compared only for presence/shape.
5. **MCP surface parity (P5):** tool-name/param-schema golden diff (see P5 tests).

### Test strategy
- **Unit:** `tests/` per module with no network: mock WS server (`mock_ws.rs` — a tokio-tungstenite server speaking a scripted protocol), fake LiveKit via `livekit`'s `RoomOptions`/event mocks where the SDK allows (else abstract `RoomHandle` trait in `livekit/room.rs` so tests inject fake `RoomEvent` streams — the trait is ours, SDK stays behind it; VERIFY at P2 what the SDK exposes for tests, fallback is the trait), mock HTTP for livekit-api and judge backends (axum test server), time frozen via injected `Clock` (deterministic `ts`/`datetime_utc` for golden tests).
- **Integration (CI-gated optional):** real LiveKit room + real agent — only in the repo's own demo project with keys in CI secrets (marked `#[ignore]` + `LK_SIM_CI_LIVE=1` env gate; the P2/P3 MANUAL gates double as this). Never in default CI (no keys).
- **Golden:** `tests/golden/` fixtures from Python (above); regenerate script `scripts/refresh_golden.sh` that runs the Python package to re-capture fixtures and diffs them (`git diff` reviewable).
- **Porting vectors:** for every Python unit test that is deterministic and offline, port the *vectors* into Rust tests (test_config.py, test_scenario*.py, test_metrics.py, test_event_writer.py, test_asserts.py, test_mic_mixer.py, test_observer.py, test_suite.py, test_plugins.py, test_evals_judge.py, test_baseline_compare.py, test_gemini_reconnect.py (via mock_ws), test_openai_realtime.py (via mock_ws), test_room_resolve.py, test_sip_asserts.py, test_cues.py, test_interrupt_rate.py, test_hold_timeout.py, test_end_call.py, test_hang_up_gate.py, test_silent_mode.py, test_script*.py, test_web_server.py, test_rest_api.py, test_cli_render.py…). Non-deterministic tests (real latency) stay MANUAL.

### Risks / mitigations

| Risk | Evidence | Mitigation |
|------|----------|-----------|
| libwebrtc build burden (C++ from source, long first build, rustflags) | research §1.6 (webrtc-sys, download_ffi.py, workspace README) | Use prebuilt webrtc-sys FFI downloads; CI `actions/cache` + sccache for `target/`; rustflags from `rust-sdks/.cargo/config.toml` copied into workspace; macOS aarch64 + Linux x86_64 matrix only in v1 |
| `gemini-live` community maintenance (0.1.x, single maintainer, stalled ~4 months) | research §2.3 | **Vendor/fork the crate** (MIT, ~1400 LOC core) with a ~2-field patch (REVISED — `language_code` on SpeechConfig; optional reconnect-on-drop knob per D4/Open question 2); wire protocol documented (R2 protocol.md); fallback hand-rolled client (~600–900 lines, Candidate C) |
| `gemini-live` reconnect semantics differ from Python by design (auto-reconnects mid-call) | research §2.3 gaps | Decision point at P2 kickoff (Open question 2): fork-patch `reconnect_on_drop` for exact parity, or accept auto-reconnect as more robust and emit `transport_dropped` on `Reconnecting`-without-GoAway; never silently diverge — document the choice in the phase note |
| LiveKit Rust SDK gaps (transcription plumbing, event surface) | research §1.3 VERIFY items | Derive transcripts from audio + `lk.transcription` text streams + agent-session byte streams exactly like Python `Observer`; `RoomHandle` trait keeps the SDK swappable; feature-probe at P2, fallback documented |
| MCP parity drift | research §5.5, §6 (21 tools) | Tool-surface golden test (P5): scripted client against both servers, diff tool lists + call shapes; same for REST API (`test_rest_api.py` vectors) |
| Dual-maintenance cost (Python + Rust) | — | Rust is the feature target; Python frozen for bugfixes after P3; new features land in both or neither (Non-goal 7); Python stays until P10 then removal is a separate tracked decision |
| Report byte-compat drift | R5 ground truth + R4 sources | I1/I2 golden byte-diff tests in every phase touching logging; `scripts/refresh_golden.sh` re-captures from Python when Python legitimately changes (bugfix) |
| Windows support | research §1.6 VERIFY (builds.yml lists platforms but unproven for lks) | `cargo check` on Windows in CI; `install.ps1` keeps Python zip until a Windows Rust binary is proven; documented in README |
| Windows SAPI (`sapi` delivery) | R4 `audio/sapi_tts.py` | `tokio::process` calling SAPI exactly as Python does; non-Windows behavior mirrored (clear error); covered by `test_sapi_tts.py` vectors |
| pyo3 embedding for plugins (binary size, ABI) | research §3 | abi3; plugins behind `plugins/` isolation; documented fallback = loud "requires Python build" error and validate-fail when `plugin_modules` set (P8) |
| `yaml_serde`/`chrono` formatting drift (YAML emit, datetime strings) | research §3 VERIFY notes | Golden tests pin emitted YAML (convert) and datetime formatting; use `jiff` strtime for `%Y%m%d-%H%M%S` and `chrono-tz` `%Y-%m-%dT%H:%M:%S%.3f%z` with `Z` substitution per R5 sample (REVISED — serde_yaml is archived; yaml_serde is the official fork) |
| Optimizer is live-benchmark only (expensive to validate) | R4 `optimize/` | P9 port mirrors Python algorithm; tests use fake eval backend; MANUAL real run gated |
| First-release install script regression | install.sh current flags (DEST, INSTALL_ROOT, LK_SIM_REF, --easy-mode) | P10 CI install smoke from draft release; preserve every existing flag (REVISED — flag list corrected) |

### Deliverables / acceptance (table)

| Phase | Deliverable | Acceptance gate | Gate type |
|-------|-------------|-----------------|-----------|
| P0 | Workspace + CI | `cargo fmt`/`clippy`/`test` green on matrix; `lks --version` works | CI (offline) |
| P1 | Config + scenario parse + convert + scaffolds | Golden tests vs templates + Python export JSON; redaction test | CI (offline) |
| P2 | Room + dispatch + Gemini caller POC | mock-WS tests green; **one real webrtc_sim run end-to-end with events.jsonl** | CI + MANUAL (live creds) |
| P3 | Audio + observer + full event writer + sqlite + summary | Parity suite: events bytes, sqlite cross-read, summary keys; **report parity with Python** (same scenario, normalized diff) | CI + MANUAL |
| P4 | Asserts + suite + full CLI | CLI surface tests; gate exit codes; assert events; **`lks execute --repeat` gate behavior matches Python** | CI + MANUAL |
| P5 | MCP server | Tool-surface golden test; scripted-client harness; MANUAL MCP client smoke | CI + MANUAL |
| P6 | SIP/outbound legs + DTMF | SimLeg unit tests; MANUAL per-leg real run | CI + MANUAL |
| P7 | Judge + evals | Mock-HTTP judge tests; evals vectors; MANUAL judge smoke | CI + MANUAL |
| P8 | Plugins (pyo3) | `example_verify.py` verdict parity; missing-module validate-fail | CI (+ MANUAL) |
| P9 | Optimize | Fake-backend tests; MANUAL real optimize run | CI + MANUAL |
| P10 | Packaging + release | Install smoke from draft release on macOS/Linux; Windows documented | CI + MANUAL |
| All | Invariants I1–I10 | Each phase's CI gate includes the relevant golden tests; final PR runs the full cargo suite + the parity suite | CI |

### Open questions (tracked with recommended answers)

1. **Single crate vs `lks-core` split if the crate grows past ~40 kLOC?** Recommended: keep single crate; the `lib` target already exposes `pub mod` for embedders. Revisit only if a third-party consumer appears.
2. **Gemini reconnect parity (REVISED — new):** Python *never* reconnects mid-call (`transport_dropped=True`, deliberate; reconnects only on GoAway with a saved resume handle), while `gemini-live` auto-reconnects on both GoAway and ConnectionLost with no policy knob distinguishing them. Recommended: first try accepting the crate default as strictly-more-robust (emit `sim.gemini_socket_drop` + set `transport_dropped` when `SessionStatus` transitions to `Reconnecting` without a preceding `GoAway` event, and when reconnect exhausts); if the parity suite or real runs show divergence, fork-patch a `reconnect_on_drop: bool` (default true) into the vendored crate and set it false for exact Python semantics. Decide at P2 kickoff, record in the phase note.
3. **`gemini_text` → `model_text` delivery rename (Python-side migration, PLAN-20260806 open question 1):** Recommended: Rust implements the *current* schema (`gemini_text`); when Python migrates, port the rename in the same change (one gap one PR). Not a blocker.
4. **`semantic_vad` eagerness for the OpenAI bridge:** Recommended: ship `eagerness: medium` (same as Python default) and expose no config knob in v1 (portable defaults); tuning is a future knob if real runs show talkativeness.
5. **`conversation.item.truncate` on barge (OpenAI):** Recommended: port Python's current behavior first (whatever `callers/openai.py` does today — read and mirror), add truncate only if Python adds it.
6. **reqwest vs ureq for judge/evals HTTP:** Recommended: `reqwest` (rustls) — already in the dependency graph via tokio ecosystem; one HTTP stack for web + judge.
7. **`web/dist` embed compression (rust-embed `compression` feature):** Recommended: enable in release builds if binary size is a concern (>200 MB is the pain line given libwebrtc); measure at P10 (web/dist itself is ~200 KB — REVISED, measured).
8. **Windows release binary in v1?** Recommended: no — research §1.6 VERIFY; `cargo check` on Windows in CI keeps it cheap to add later; `install.ps1` keeps the Python zip until proven.
9. **Drop `lks optimize` if unused at P9?** Recommended: port it (documented user flow: MCP tool + CLI + docs recipe); revisit with the Python usage log at P9 kickoff (AGENTS.md no-dead-features).
10. **Pyo3 abi3 version vs system Python:** Recommended: abi3-compatible embedding with CPython 3.11+; the target repos run any modern Python for their agents — plugins only need the *interpreter*, not site-packages (verify `docs/plugins.md` for what plugin `.py` files import — if they import target packages, note that embedded CPython must be the *system* Python via pyo3's `Python::with_gil` + `sys.path` extension; VERIFY at P8, fallback documented).
11. **`timeline.md`/`review.md` byte-lock?** Recommended: no (research §5.1: human-readable, not byte-locked); regenerate deterministically for golden *tests* (same input → same output) but don't promise cross-implementation byte equality.
12. **`language_code` on Gemini speech_config (REVISED — new):** the vendored `gemini-live` `SpeechConfig` has no `language_code` field; Python sets `speech_config.language_code=voice.language` (default `en-US`). Recommended: fork-patch one serde `camelCase` field (`language_code`) on `VoiceConfig`/`PrebuiltVoiceConfig`, mirror `voice.language` fallback chain (voice.language > simulator.language > `en-US`), and pin it with the `gemini_setup_payload_matches_python` test. Do NOT ship without it — the caller's language would silently fall back to server auto-detect.

---

*Companion research: `docs/rust-port-research.md` (all crate versions, API claims, VERIFY items, and evidence paths). Porting checklist: this plan + research doc + Python source as reference (R4) + real-run fixtures (R5).*
