# Appendix A — Web layer phase spec (P3.5: web report player + REST API parity)

> Ground truth: `src/livekit_agent_simulator/web/` (12 files, 2406 LOC — api.py 230, server.py 380, cues.py 159, markers.py 305, report_time.py 96, speech_origin.py 348, tool_events.py 286, transcript_cues.py 198, cue_helpers/source_priority.py 82, cue_helpers/windows.py 168, `__init__.py` 7, paths.py 72 [only `package_web_dir` used]) — all read fully. This appendix is the implementer-ready spec; it references no Python source. Companion: `docs/rust-port-research.md` crate evidence (axum 0.8.9, tower-http 0.6.11, rust-embed 8.12, jiff 0.2.35).

---

## P3.5 — Web report player + REST API (parity)

- **Scope:** port the two HTTP servers and the full cues-report pipeline: (1) report UI server (port **8765**) serving the embedded SPA (`web/dist`) + `/api/runs*` JSON + `/runs/<id>/*` file routes; (2) REST API server (port **8787**, prefix `/api/v1`) bridging to the ops layer; (3) `cues.json` pipeline — report_time (timing base), source_priority, windows, transcript_cues (dedupe + ghost-STT), markers, tool_events, speech_origin, cues payload assembly. Byte-parity target: `cues.json` output identical to Python `web/cues.py` for the same report dir (golden-tested), header-level parity for HTTP (exact Content-Type / Cache-Control / no-store / 302 semantics). This phase runs after P3 (report files exist on disk) and before P4 (ops passthrough routes land against the Rust ops layer; until then `POST /api/v1/execute|validate|preflight` return op-stub shapes that the P4 op layer fills in — the web layer only guarantees status codes + error mapping, payload shapes are ops-owned, port note 20).
- **Files:**
  - `crates/lks/src/web/mod.rs` — server spawn/join, shared port/route constants
  - `crates/lks/src/web/report_server.rs` — report UI server (port 8765)
  - `crates/lks/src/web/api_server.rs` — REST API server (port 8787)
  - `crates/lks/src/web/cues.rs` — `build_cues_payload` + `write_cues_json` (payload assembly order)
  - `crates/lks/src/web/report_time.rs` — wav duration, `_mono_to_audio_ms`, `_clamp_end`, t0 resolution
  - `crates/lks/src/web/source_priority.rs` — `source_rank`, `texts_similar`, `_ASR_TOKEN_ALIASES`
  - `crates/lks/src/web/windows.rs` — `estimate_utterance_ms`, `collect_interim_starts`, `collect_agent_active_windows`, `best_interim_start`, `best_active_window`
  - `crates/lks/src/web/transcript_cues.rs` — raw/dedupe/ghost-filter/timing/final-sort
  - `crates/lks/src/web/markers.rs` — all marker kinds + `_collect_script_injects` + `_inject_duration_near`
  - `crates/lks/src/web/tool_events.rs` — `_build_tool_spans`, `_build_session_summary`, `_extract_chat_history`, `_build_tool_summary`
  - `crates/lks/src/web/speech_origin.rs` — `_norm_speech`, `_CONTENT_STOP`, `_text_overlap`, `_mostly_script_say`, `_tag_cues_with_markers`, `_pin_script_window`, `_synthetic_script_barge_cues`
  - `crates/lks/src/web/static_assets.rs` — rust-embed asset map + `package_web_dir` resolution
  - `crates/lks/tests/parity_cues.rs` (golden vs Python `cues.json`), `crates/lks/tests/web_routes.rs`, `crates/lks/tests/cues_constants.rs`, `crates/lks/tests/markers.rs`, `crates/lks/tests/speech_origin.rs`, `crates/lks/tests/transcript_dedupe.rs`, `crates/lks/tests/static_serving.rs`, `crates/lks/tests/range_requests.rs`
- **Steps:**
  1. **Timing base (`report_time.rs`)** — implement in this order; everything else depends on it:
     - `wav_duration_ms(path) -> Option<i64>`: `int(frames * 1000 / rate)` — hound `WavReader` frames; `rate = getframerate() or 1` (rate 0 → 1); any error → `None` (corrupt header/unsupported format tolerated).
     - `load_events(path) -> Vec<Event>`: skip empty lines; skip lines that fail JSON parse (corrupted jsonl tolerated per-line); missing file → `[]`.
     - `load_json(path) -> serde_json::Value`: missing/corrupted → `{}`; non-object → `{}`.
     - `resolve_audio_t0_ms(meta, events) -> i64`: `meta.audio.t0_mono_ms` (int, `max(0, …)`); else first event with kind starting `transcript.` or kind in `{sim.mic_published, sim.gemini_connected}` → `max(0, ts_mono_ms)`; else 0.
     - `mono_to_audio_ms(mono, t0, duration_ms) -> Option<i64>`: `start = max(0, mono - t0)`; **if duration_ms is Some and start > duration_ms + 2000 → None** (events beyond 2 s past audio end dropped).
     - `clamp_end(start, end, duration_ms) -> i64`: `end = max(start + 120, end)`; if duration: `end = min(end, max(start + 120, duration_ms))`.
     - **No datetime formatting exists in this module.** The only datetime parse in the whole web layer is the run-list sort key (step 3).
  2. **Source priority + text similarity (`source_priority.rs`)** — pure functions, table-driven:
     ```rust
     const SIM_CALLER_SOURCES: [&str; 2] = ["sim.gemini", "sim.openai"];
     // USER table:  sim.gemini=0, sim.openai=0, data=2, lk.transcription=3
     // AGENT table: data=0, lk.transcription=1, sim.gemini=2, sim.openai=2
     fn source_rank(source: &str, role: &str) -> i64
     // exact table match on role == "user" (any other role → agent table);
     // else if source non-empty AND not in {sim.gemini, sim.openai, lk.transcription}
     //   → opaque data-topic source: 1 (user) / 0 (agent);
     // else (empty or builtin-not-in-table) → 9
     fn texts_similar(a: &str, b: &str) -> bool
     ```
     - `texts_similar` normalization: lowercase + strip; remove apostrophes `'` `'` `'`; `re.sub(r"[^\w\s]", " ", t)` → replace every non-word/non-space char with a space; collapse whitespace. Either normalized text empty → false.
     - Match if `a_n == b_n || a_n in b_n || b_n in a_n` (substring either direction).
     - Token-canonical match: per-token aliases `_ASR_TOKEN_ALIASES = {"okey":"okay", "ok":"okay", "k":"okay", "thank":"thanks", "thx":"thanks", "byebye":"bye"}` applied to each whitespace token (note `bye`→`bye` is identity); token sets `ta`, `tb`; `inter = ta ∩ tb`; true iff `inter.len() >= max(1, int(min(len(ta),len(tb)) * 0.5 + 0.5))` — **round-half-up of the smaller set size, min 1** (`int(x + 0.5)` floor).
  3. **Windows (`windows.rs`)** — all constants verbatim:
     - `estimate_utterance_ms(text, role) -> i64`: empty text → **800 (agent) / 600 (user)**. `words` = split on `" "` after replacing `\n` with `" "`, keeping non-empty entries; `units = max(len(words), max(1, len(text)/4))` (**integer division**); `ms = units * (95 if agent else 85)`; clamp agent to `[700, 22_000]`, user to `[500, 14_000]`.
     - `collect_interim_starts(events, duration_ms) -> Vec<(role, ms, text, source)>`: kinds `transcript.*.interim`; role from `"agent"`/`"user"` inside kind; text = `spec.text` stripped; skip empty; ms via `mono_to_audio_ms` (skip `None`).
     - `collect_agent_active_windows(events, duration_ms) -> Vec<(i64, i64)>`: from `room.active_speakers` events; `agent_on` iff any identity `starts_with("agent-")` **or** `"agent" in identity.to_lowercase()`; missing/empty `identities` → off; non-list `spec.identities` → `[]`. Points sorted by ms; **`gap_close_ms = 2800`**: while on, if gap since last-on > 2800 → close window `(start, last_on + 600)` and start new; on off → close `(start, last_on + 600)`. Trailing window end = `last_on + 600`, capped at duration_ms. Merge pass: if `w0 <= prev_w1 + 1500` → merge.
     - `best_interim_start(interims, role, final_ms, text, est_ms, prefer_source) -> Option<i64>`: `window_lo = max(0, final_ms - est_ms - 3000)`; `window_hi = final_ms - 500`; if `window_hi <= window_lo` → None. Candidate match: `il == final_l || final_l.starts_with(il[..min(12, il.len())]) || il.starts_with(final_l[..min(12, final_l.len())])` — **12-char truncated prefix match**. Rank = `source_rank(src, role)`; if `prefer_source == Some(s)` and `src == s` → rank = **-1**. Sort candidates by `(rank, ms)`; return the min `ms` among the best rank.
     - `best_active_window(windows, final_ms, est_ms) -> Option<(i64, i64)>`: keep windows where `final_ms >= w0 - 300 && final_ms <= w1 + 2000`; `span = max(1, w1 - w0)`; `score = abs(w1 - final_ms) + abs(span - est_ms) / 3` (**integer division**); lowest score wins → `(w0, w1)`.
  4. **Transcript cues (`transcript_cues.rs`)** — pipeline: raw → dedupe → ghost filter → timing → final sort.
     - Raw cues from `transcript.user.final` / `transcript.agent.final` events: fields `role, final_ms, text, turn, source, kind, ts_mono_ms` (`final_ms` from `mono_to_audio_ms`, skip None; non-int `ts_mono_ms` → skip event).
     - **Dedupe** (window `max_delta`): sort raw by `(final_ms, source_rank(source, role), 0 if role=="agent" else 1)` — agent first on ties. For each cue walk backward over same-role cues: `delta = abs(prev.final_ms - c.final_ms)`; `similar = texts_similar(prev.text, c.text)`; `cross_source = prev_src && cur_src && prev_src != cur_src`. `max_delta = 15000` if user+similar+cross_source; **6000** if agent+similar+cross_source; **4000** if similar; **2500** if not similar. **If `delta > max_delta` → BREAK (do not continue scanning — farther cues can never match; sorted order is relied on).** If similar: replace `cues[i]` with `c` iff `cur_rank < prev_rank` OR (equal rank AND `len(c.text) > len(prev.text)`); set replaced; break. Not replaced → append.
     - **Ghost-STT filter**: only runs when at least one user cue has source in `{sim.gemini, sim.openai}` (a "provider user"). Drop user cue `c` iff ALL of: (a) `src` not in `{sim.gemini, sim.openai}`; (b) `src == "lk.transcription"` OR (`src` non-empty AND not in `{sim.gemini, sim.openai, sim.script}` AND `"transcript" in src`) — i.e. agent-side STT sources only; (c) exists provider user cue `g` with `abs(c.final_ms - g.final_ms) <= 2500`; (d) `!any(texts_similar(c.text, g.text) for g in near)`. Non-user cues always kept; `sim.script` sources never dropped.
     - **Timing per cue** (after dedupe/ghost): `est = estimate_utterance_ms(text, role)`; agent → `best_active_window(agent_windows, final_ms, est)` → `(start, end_hint)`; `prefer_source = Some("sim.gemini")` for user else None; `interim_start = best_interim_start(interims, role, final_ms, text, est, prefer_source)`; if `interim_start < start` (or start None) → `start = interim_start`. Fallback `start = max(0, final_ms - est)`. If `start >= final_ms` → `start = max(0, final_ms - 400)`. **Clamp `start = max(0, min(start, final_ms - 200))`.** `tail = 350`; `end = final_ms + 350`; if `end_hint > final_ms` → `end = max(end, min(end_hint, final_ms + 800))`; if duration known → `end = min(end, max(start + 200, duration_ms))`; finally `end = max(start + 200, end)`. Final sort `(start_ms, 0 if agent else 1)`.
  5. **Markers (`markers.rs`)** — kind strings (verbatim): `barge_in, script_cue, user_audio_source, audio_onset, silence_wait, silence, interruption, recovery, backchannel, false_interrupt, dtmf, tool, tool_error`.
     - `_collect_script_injects`: kind `sim.script_inject`; `start = mono_to_audio_ms(ts_mono_ms)`; `dur = int(spec.duration_ms or 0)`; **if `dur <= 0`: `dur = 2200` if `delivery != "room_pcm"` else `800`**; `end_ms = start + max(200, dur)`; fields `start_ms, duration_ms, end_ms, label, text, delivery, asset`.
     - `_inject_duration_near(at_ms)`: candidate injects with `abs(inj.start_ms - at_ms) <= 900`; `same` if label substring (either direction) or `_text_overlap(label, say)` via `say`; `score = d - (200 if same else 0)`; best (lowest score) → its `duration_ms`.
     - `sim.script.cue`: `mtype = barge_in` if `spec.barge_in` else `script_cue`; overridden by icls: `backchannel`→backchannel, `noise`→false_interrupt, `dtmf`→dtmf (`icls = spec.class or spec.interrupt_class`). `detail = "trigger={trigger or '?'} · during_agent={bool}"` + (`" · class={icls}"`) + (`' · say="{say}"'`) + (`" · waited={waited}ms"`). **Span: if barge and icls not in (noise, backchannel): `span = max(2200 if during else 1400, (inj_dur or 0) + 400)`; else `span = max(400, min(waited, 2000) or 400, (inj_dur or 0) + 200)`.** `end = clamp_end(start, start + span, duration_ms)`. Label prefix: `"⚡ "` (barge+during, not noise/backchannel), `"💬 "` (backchannel), `"🔇 "` (noise). Fields: `type, start_ms, end_ms, label, detail, step_id` (None if empty), `say` (None if empty), `during_agent_speech, barge_in, class` (icls, may be None), `audio_ms = inj_dur or span`. `barge_points.append(start)` only if `barge && icls not in (noise, backchannel, dtmf, silence)`.
     - `sim.script.wait`: `span = waited` if `waited > 0` else **1500**; `win_start = max(0, start - span)`; `end = clamp_end(win_start, start + 200)`; type `silence_wait`; `label = label or step_id or "user pause"`; detail `"script wait · trigger={trigger or 'silence'} · held≈{span}ms"`; fields `step_id, trigger`.
     - `silence.detected`: `span = duration_ms` if `> 0` else **4000**; `win_start = max(0, start - span)`; `end = clamp_end(win_start, start)`; type `silence`; label `"silence detected"`; detail `"observer silence ≥ threshold ({span}ms)"`; field `duration_ms`.
     - `interruption`: `end = clamp_end(start, start + 500)`; type `interruption`; label `"interruption ({by})"` (`by = spec.by or "unknown"`); `detail = note.strip() or "by={by}"`; fields `class` (strip or None), `by`.
     - `sim.agent.audio_onset`: `end = start + 300`; type `audio_onset`; label `"agent audio onset"`; detail `"onset_frame={spec.onset_frame_idx} · vad={spec.vad.get('method','?')}"`; field `onset_frame_idx`.
     - `sim.caller.audio_source_start`: `end = start + 300`; type `user_audio_source`; label `"caller audio source"`; detail `"provider={spec.provider or '?'} · via={spec.via or '?'}"`.
     - `Recovery`: agent_finals = all `transcript.agent.final` audio-ms; for each barge point in order, first agent final `> barge_ms` not already consumed; `end = clamp_end(recovery_ms, recovery_ms + 800)`; type `recovery`; label `"agent recovery"`; detail `"agent final after barge-in @ {barge_ms}ms"`; field `after_barge_ms`.
     - **Final sort: `(start_ms, type_string)`** — within same start_ms, type sorts alphabetically (`audio_onset < backchannel < barge_in < … < user_audio_source`).
  6. **Tool events (`tool_events.rs`)**: `_UNCLOSED_TOOL_TAIL_MS = 500`, `_TOOL_BAND_MIN_MS = 400`, `_TOOL_BAND_CAP_MS = 400`. Events `tool.start` / `tool.end` / `tool.error`. Pending key: `"call:{call_id}"` (call_id = first of `spec.call_id`/`spec.id`) else `"evt:{event_id}"`. `name = first of spec.name/tool_name or "tool"`; `arguments = first of spec.arguments/args/payload`; `call_id = first of spec.call_id/id`. `tool.start` stores `{call_id, name, start_ms, turn, source, arguments, parent_event_id, start_event_id}`. End matching order: (1) `call:{call_id}` key in pending; (2) scan pending for row with `start_event_id == parent_event_id`; (3) `len(pending) == 1` → take the only one. `dur = int(spec.duration_ms or 0)`; if `dur <= 0` and start_row → `dur = max(0, start - base_start)`. `is_error = kind == "tool.error" || bool(spec.is_error)`. `output = first of spec.output/result/error`; `error = first of spec.error/message` if is_error. **`end_ms = base_start + max(400, min(dur, 400))` if dur > 0 else `base_start + 500`**, then `clamp_end`. Span fields: `call_id, name, start_ms, end_ms, duration_ms` (None if dur <= 0), `turn, source, arguments, output, is_error, error, parent_event_id`. Unclosed pending: `end = base + 500` clamped; `duration_ms = None, is_error = false, output = None, error = None`. Sort `(start_ms, name)`.
     - `_build_session_summary`: `session.usage` → `usage = dict(spec)`; `session.agent_state` → transitions `{at_ms, from (str|None), to}` appended only if `new_state` is not None; `session.error` → errors `{at_ms, message = first of spec.message/error or "session error"}`. Returns None if all empty; else only non-empty keys present: `{"usage"} / {"state_transitions"} / {"errors"}`.
     - `_extract_chat_history`: last `session.chat_history` event (reversed scan), `spec.items` if list else None.
     - `_build_tool_summary`: `tool_count` from `summary.tool_calls` (int) else `summary.metrics.tool_calls`; `tool_errors` from `summary.tool_errors` else `metrics.tool_errors`; if `<= 0` → from spans (len / count is_error). Returns `{"tool_count", "tool_errors"}` ints (can be 0).
  7. **Speech origin (`speech_origin.rs`)** — the cue-tagging pass:
     - `_norm_speech`: keep only alnum + whitespace (everything else → `" "`), lowercase, collapse whitespace. `_CONTENT_STOP` (verbatim): `{"được","không","mình","bạn","với","cho","của","này","đó","là","và","các","một","như","để","có","thì","rồi","nữa","when","what","that","this","with","have","from","your","will","been","were","they","them","than","then","also","just","into","over","more","hook"}`.
     - `_text_overlap(a, b)`: empty → false; `len(shorter) >= 5 && shorter in longer` → true; content-word sets (len >= 4, not in `_CONTENT_STOP`); empty intersection → false; `any(len(w) >= 5 for w in inter)` → true; else `len(inter) >= 2`.
     - `_mostly_script_say(text, say)` (all on normalized text `nt`, `ns`): `nt == ns` → true. `ns in nt`: extras = `nt` with `ns` removed once (single occurrence replaced with `" "`, then strip); extras empty → true; `len(extras) <= 12 && len(extras.split()) <= 2` → true; content words in extras: `len(content) >= 2 || len(extras) > max(24, int(len(ns) * 0.45))` → false; else true. `nt in ns` (STT split of script): `len(content) >= 2 || len(nt) >= 12` → true; `len(ns) - len(nt) <= 20` → true. Fallthrough: `_text_overlap(text, say) && len(nt) <= len(ns) + 24`.
     - `_tag_cues_with_markers(cues, markers)`: marker `m` is "near" cue `c` iff `abs(m.ms - final_ms) <= 8000 || abs(m.ms - start) <= 1200 || (m.ms <= end && m.end_ms >= start)` where `final_ms = c.final_ms` if not None else `c.end`. Non-user role → `speech_origin = "natural"` (no tagging).
     - **User scoring vs script markers** (barge_in + script_cue types): `delta = final_ms - m.ms`; skip if `delta < -800 || delta > 15000`. `text_hit = _mostly_script_say(text, say)` only if `say` non-empty and not starting with `[`. `tiny = len(text.split()) <= 3 && len(text.strip()) <= 28`. **barge markers**: `text_hit && -500 <= delta <= 15000` → accept, `score = 100 - min(40, max(0, delta) / 400)` (integer division); `tiny && 0 <= delta <= 3500` → accept, `score = 70 - min(30, delta / 200)`; else score 0. **non-barge**: `accept = text_hit && 0 <= delta <= 8000`, `score = 50`. `origin = "script_barge"` if barge else `"script_cue"`. Best score wins.
     - **Time-only fallback**: origin still natural, `len(text.split()) <= 2 && len(text.strip()) <= 24` → first barge marker with `0 <= delta <= 3500` → `script_barge`.
     - **Late-STT fallback**: `len(text.split()) <= 4`; barge markers with `say` non-empty, not starting `[`, `_text_overlap(text, say)`, `delta >= -500`; key = `(phrase, -abs(delta))` where `phrase = 2` if `nt in ns || ns in nt` else 1; max key wins → `script_barge`.
     - `_pin_script_window`: sets `script_step_id, script_say, script_label` from the matched marker; `inject_ms = matched.start_ms or final_ms`; `audio_ms = matched.audio_ms or 0`; **if `audio_ms <= 0`: `2200` if origin == "script_barge" else `900`**. `start_ms = max(0, inject_ms - 80)`; `end_ms = max(final_ms + 500, inject_ms + audio_ms + 350, existing end_ms)`; sets `inject_ms`.
     - **Full-line preference**: if `say` non-empty, not starting `[`, `len(text.strip()) < len(say)`, `len(text.split()) <= 6` → `stt_text = original text`; `text = say`.
     - `_synthetic_script_barge_cues(markers, cues)`: covered_steps (existing cues with speech_origin in {script_barge, script_cue} and a script_step_id) + covered_injects (their inject_ms). For each marker with `barge_in` or type `barge_in`: skip if `step_id in covered_steps`; skip if `any(abs(inject_ms - t) < 600 for t in covered_injects)`. `say = m.say` strip; `label = m.label or step_id or "script barge"`; `display = say` unless say starts `[` AND ends `]` → label; strip leading `"⚡"`. `audio_ms = m.audio_ms or 0`; if `<= 0` → `max(1200, m.end_ms - inject_ms)`; `end_ms = max(inject_ms + audio_ms + 350, inject_ms + 1200)`. Cue fields: `role "user", start_ms = max(0, inject_ms - 80), end_ms, final_ms = end_ms, text = display, speech_origin "script_barge", script_step_id, script_say = say or display, script_label = label, inject_ms, synthetic: true, source: "sim.script", marker_tags: ["barge_in"]`. After appending, mark step + inject covered.
  8. **Payload assembly (`cues.rs`)** — exact build order: load events/meta/summary → duration (wav first, else `int(audio_meta["duration_ms"])`, TypeError/ValueError → None) → t0 → transcript cues (step 4) → tool spans (step 6) → markers (step 5) → `_tag_cues_with_markers` → `cues.extend(_synthetic_script_barge_cues)` → script_verify/assert_verify from summary, fallback to LAST `script.verify` / `assert.verify` event spec (reversed scan) → behavior_summary: `summary.caller.behavior_summary` (dict) → `summary.behavior_summary` → recompute via `script::build_caller_behavior_summary(events)` (shared ops/script module — port the contract there; port note 19) → observe_gaps: `meta.config_snapshot.observe_gaps` (list) → `config_snapshot.observe.observe_gaps` (list) → `[]`. `marker_counts` = count per marker type string. Payload shape (§3). `write_cues_json`: `serde_json::to_writer` with 2-space indent, `ensure_ascii = false` (Python `json.dumps(..., ensure_ascii=False, indent=2)`), utf-8 → `report_dir/cues.json`.
  9. **Report UI server (`report_server.rs`, port 8765)** — axum app with the route table §2, static assets from §4. Threading model: Python `ThreadingHTTPServer` = one OS thread per connection; axum with `tokio::spawn`-per-connection semantics is the equivalent — keep the server off the main runtime of the CLI's blocking ops (spawn in its own runtime or use `Handle::spawn_blocking` for the `cues.json` regeneration; the pipeline is sync and cheap, keep it sync per request like Python).
  10. **REST API server (`api_server.rs`, port 8787, prefix `/api/v1`)** — route table §2. Ops bridge: `_run_op` semantics — if the op returns a future, run it on a fresh runtime per request (`tokio::runtime::Builder::new_current_thread()` per call, matching `asyncio.run` fresh-loop-per-request); sync ops call directly. Error mapping: `ValueError` → 400, `ops::ConfigError` → 404, `FileNotFoundError` → 404, any other error → 500 with body `"{TypeName}: {msg}"`. Body reader: Content-Length or 0; `len <= 0` → `{}`; empty body → `{}`; JSON parse error or unicode error → 400 `"invalid JSON body: {exc}"`; non-object JSON → 400 `"JSON body must be an object"`. No max body size.
  11. **Logging contract**: report server logs only requests containing `404`; REST API logs only status codes starting with `4`/`5` — tests grep logs; keep the same filtering (tracing: debug for 2xx, warn for 4xx/5xx).
- **Tests:** §6 (names + intent).
- **Acceptance gates:**
  - **(a) offline/CI**: all §6 tests green, including `parity_cues_golden` — byte-diff of `cues.json` against Python `web/cues.py` output for a fresh real run fixture (REVISED: the earlier R5 fixture `114-people-pleaser-refuse-card-20260809-201652-8b32` is not in this repo — capture a current Python run into `tests/golden/` before the phase, per cross-validation strategy 1) and for one in-progress report dir (live-fallback shape).
  - **(b) MANUAL** (not a CI gate): `lks web <run_id>` serves the Rust-written report and plays audio — same acceptance as the P3 MANUAL gate but against the Rust pipeline end-to-end: run `lks execute` under Rust, then `lks web` with `--no-open`, open `http://127.0.0.1:8765/?run=<run_id>`, verify timeline markers/cues/tool bands render and conversation audio plays; then `curl -s localhost:8787/api/v1/runs` returns the same list shape as Python `lks` REST.
- **Deps added:** axum 0.8.9, tower-http 0.6.11 (features `fs`, `trace`, `catch-panic`; **not** 0.7.0 — its compression/trailing-slash/fs-Backend breaking changes are not needed), rust-embed 8.12 (features `debug-embed`, `compression`, `mime-guess`), tokio (full), serde_json (`preserve_order`), jiff 0.2.35, hound (already at P2), tower (via axum).

---

## 1. Route table — report UI server (port 8765)

| Method | Path | Query | Response | Headers | Status / edge cases |
|---|---|---|---|---|---|
| GET | `/`, `/index.html` | – | `player_dir/index.html` body | `Content-Type: text/html; charset=utf-8`, `Cache-Control: no-store` | 200; **missing `index.html` at startup → server refuses to start** with `FileNotFoundError`-equivalent message: `Web UI assets missing: {player_dir}/index.html — maintainers: pnpm --dir web install && pnpm --dir web build (CI attaches web/dist into the wheel as web_static; or use pnpm --dir web dev with lks web in another terminal)` |
| GET | `/player.html` | `run` | 302 redirect | `Location: /?run=<run>` (non-empty run) else `Location: /` — **only header, no body, no Content-Type** | 302 |
| GET | `/assets/<name>` | – | file bytes | `Content-Type: mimetypes.guess_type(name) or "application/octet-stream"` | 200; path-escape → **403 `asset not found`** (prefix check on resolved path); not a file → 404 `asset not found` |
| GET | `/player.js`, `/player.css` | – | file bytes | `.js → text/javascript; charset=utf-8`, `.css → text/css; charset=utf-8` (suffix override in `_serve_file`); `Cache-Control: no-store` (suffix in `.html/.js/.css`) | 200; missing → 404 `missing {name}` |
| GET | `/api/runs` | (none used) | JSON array, newest first — see §1.1 | `application/json; charset=utf-8`, `Cache-Control: no-store`, `Content-Length` | 200; only dirs containing `events.jsonl` are listed |
| GET | `/api/runs/<id>` | – | cues payload JSON (regenerated per request) | as above | 404 `run not found` if id empty or report dir missing |
| GET | `/api/runs/<id>/cues` | – | same as `/api/runs/<id>` (second segment `cues` matched) | as above | any other second segment → 404 `unknown api path` |
| GET | `/runs/<run_id>/<name>` | – | `name == "" or "player"` → 302 `Location: /?run=<run_id>` (no body); `name == "cues.json"` → regenerate + serve `application/json; charset=utf-8` + no-store; else file bytes, `ctype = guess_type or "application/octet-stream"` | suffix override per `_serve_file` | report_dir escapes reports_root → **403 `forbidden`**; not a dir → 404 `run not found`; target escapes report_dir → **403 `forbidden`**; not a file → 404 `file not found` |
| anything else | – | – | – | – | 404 `not found` |

### 1.1 `GET /api/runs` element shape (exact)

```json
{"run_id": "<dir name>", "scenario_id": "<str>", "status": "<str>",
 "duration_ms": <int|None>, "turn_count": <int>, "tool_count": <int>,
 "has_audio": <bool>, "started_utc": "<str>", "mtime_ms": <int>}
```

- `has_audio = (report_dir/"conversation.wav").exists()`; `mtime_ms = int(dir.stat().mtime * 1000)` (0 on stat error — f64 seconds × 1000, truncate).
- `scenario_id`/`started_utc` from `summary.json`, fallback to `meta.json` only when absent.
- `tool_count = summary.tool_calls` else `summary.metrics.tool_calls` if metrics is an object.
- **Sort key**: `started_utc` non-empty str → parse ISO-8601 UTC (`Z` → `+00:00`, `datetime.fromisoformat` via jiff `Timestamp::strptime` / manual `+HH:MM` handling; parse failure → fall through); fallback `mtime_ms` if > 0 → `(mtime_ms/1000.0, run_id)`; else `(0.0, run_id)`. Sort descending (newest first), tie-break run_id string.
- **Live-run fallback** (only if `summary.get("status")` is None — i.e. no summary.json yet): read `events.jsonl` line-by-line; `run.started` → status `"running"`; `run.ended` → status = `ev.spec.status or "done"`; kinds `transcript.user.final` / `transcript.agent.final` with int `turn` → live_turns set; `transcript.agent.final` → `live_agent_finals += 1`; `tool.start` → `live_tools += 1`. Blank lines skipped; **a JSON parse error aborts the whole scan, keeping partial counts** (do not implement a strict parser). Still no status → `"running"` if `live_agent_finals > 0` or events file exists else `"queued"`. `turn_count = len(live_turns) or (1 if live_agent_finals > 0 else 0)`; `tool_count = live_tools`.

### 1.2 REST API server (port 8787, prefix `/api/v1`)

All responses: 200 with `Content-Type: application/json; charset=utf-8`, `Cache-Control: no-store`, `Content-Length`, body = JSON with `ensure_ascii = false`, 2-space indent. Errors: `{"error": msg}` — `ensure_ascii = false`, **no indent**.

| Method | Path | Body | Response | Edge cases |
|---|---|---|---|---|
| GET | `/api/v1/health` | – | `{"ok": true, "root": "<resolved project_root>"}` — **no `version` key** (docstring claims one; code does not emit it) | – |
| GET | `/api/v1/runs` | – | `ops::list_runs` passthrough | – |
| GET | `/api/v1/runs/<id>` | – | `ops::get_run_status` | empty id → 400 `missing run id` |
| GET | `/api/v1/runs/<id>/report` | – | `ops::get_run_report` (matched when rest ends with `/report` after strip) | `…/report/foo` → run_id `x/report/foo` → 404 from ops |
| GET | `/api/v1/scenarios` | – | `ops::list_scenarios` | – |
| GET | `/api/v1/scenarios/<id>` | – | `ops::export_scenario` | empty → 400 `missing scenario id` |
| POST | `/api/v1/validate` | `{"scenario_id": str}` | `ops::validate_scenario` | missing → 400 `validate needs scenario_id` |
| POST | `/api/v1/execute` | `{"scenario_id", "repeat" (int, default 1), "pass_at_k", "run_name", "agent_name"}` | `ops::execute_scenario(root, sid, repeat=int(...), pass_at_k, run_name, agent_name)` | missing sid → 400 `execute needs scenario_id`; `int(repeat)` — float truncates toward zero, `"3"` works, `"abc"` → ValueError → 400 |
| POST | `/api/v1/preflight` | `{"connectivity": bool, default true}` | `ops::preflight(root, connectivity=...)` | **`bool(value)` semantics: only JSON false/0/""/null are falsy; string `"false"` is truthy — replicate exactly** |
| unknown POST | – | – | 404 `{"error": "unknown POST route: {rest}"}` | – |
| other unknown | – | – | 404 `{"error": "unknown route: {path}"}` | non-`/api/v1` prefix → 404 `unknown path — REST API under /api/v1` |

- Query strings stripped (`urlparse(...).path`) before routing.
- `_read_json`: Content-Length or 0; `len <= 0` → `{}`; empty body → `{}`; JSON/unicode decode error → 400 `invalid JSON body: {exc}`; non-object → 400 `JSON body must be an object`. No max body size.
- Ops error mapping: `ValueError` → 400; `ops::ConfigError` → 404; `FileNotFoundError` → 404; other → 500, body `"{TypeName}: {msg}"`.
- Async ops run on a fresh event loop per request (Python `asyncio.run`); Rust: current-thread runtime per request. Blocking execute allowed.
- `start_api_server` returns `{"url": base+PREFIX, "base_url", "host", "port", "root"}` (+ server/thread handles in non-blocking mode; Rust: return the `tokio::task::JoinHandle`). Blocking mode prints `REST API: {url} (root: {root})` + `Ctrl+C to stop`; poll interval 0.5 s; Ctrl+C → shutdown + close.

---

## 2. Cues payload — exact shape (`cues.json`)

```json
{
  "run_id": "<report_dir.name>",
  "scenario_id": "<meta.scenario_id or summary.scenario_id>",
  "audio": {"file": "conversation.wav" | null, "duration_ms": <int|null>,
            "t0_mono_ms": <int>,
            "channels": <audio_meta.channels> | {"left": "sim", "right": "agent"}},
  "cues": [ ... ], "markers": [ ... ],
  "marker_counts": {"<type>": <count>, ...},
  "script_verify": <dict|null>, "assert_verify": <dict|null>,
  "caller": {"behavior_summary": <dict>} | null,
  "behavior_summary": <dict|null>,
  "tool_events": [ ... ],
  "tool_summary": {"tool_count": <int>, "tool_errors": <int>},
  "session_summary": <dict|null>, "chat_history": <list|null>,
  "observe_gaps": <list>
}
```

Empty-state payload (no events, no files): `audio {file: null, duration_ms: null, t0_mono_ms: 0, channels: {left: "sim", right: "agent"}}`, `cues []`, `markers []`, `marker_counts {}`, `script_verify/assert_verify/caller/behavior_summary/session_summary/chat_history` all null, `tool_events []`, `tool_summary {tool_count: 0, tool_errors: 0}`, `observe_gaps []`. `channels` default applies only when `audio_meta.channels` is missing/null/empty (falsy).

### Per-cue and per-marker fields

- **Transcript cue**: `role, final_ms, text, turn, source, kind, ts_mono_ms, start_ms, end_ms` + optional `script_step_id, script_say, script_label, inject_ms, marker_tags, speech_origin, stt_text, synthetic`.
- **Marker by type**: `barge_in/script_cue/backchannel/false_interrupt/dtmf`: `type, start_ms, end_ms, label, detail, step_id (null if empty), say (null if empty), during_agent_speech, barge_in, class, audio_ms`. `silence_wait`: + `trigger`. `silence`: + `duration_ms`. `interruption`: + `class, by`. `audio_onset`: + `onset_frame_idx`. `user_audio_source`: no extras. `recovery`: + `after_barge_ms`. `tool/tool_error`: + `tool_name, is_error, call_id`.
- **Tool span**: `call_id, name, start_ms, end_ms, duration_ms (null if unknown), turn, source, arguments, output, is_error, error, parent_event_id`.

---

## 3. Static serving

- **Embed**: `crates/lks/assets/web/` holds the `web/dist` copy; `#[derive(RustEmbed)] #[folder = "assets/web/"]` — feature `debug-embed` for dev (read from disk, picks up rebuilds), `compression` in release (~200 KB web/dist). Resolve `mime` via the `mime-guess` feature by extension; fall back to `application/octet-stream`.
- **Static dir resolution** (`package_web_dir` port): prefer the **newest** of (1) repo-root `web/dist` found by walking up ≤ 6 parent dirs looking for `web/dist/index.html`, (2) embedded `web_static` equivalent. If `web/dist` exists with `index.html`: it wins if the packaged copy is missing OR `dist/index.html.mtime >= packaged/index.html.mtime` (**f64 ≥ f64, sub-second precision; `>=`, not `>`**); stat error → prefer dist. In a wheel/installed context only the embedded copy exists.
- **Routing**: axum `nest_service("/", ServeDir::new(...))` (tower-http `fs` feature) with `ServeDir::append_response_header("cache-control", "no-store")` — **no-store iff the served suffix is `.html`/`.js`/`.css` or the no-store flag is set** (matches `_serve_file`: `no_store || suffix in (".html", ".js", ".css")`; `Accept-Ranges: bytes` only when content-type starts with `audio/`). Mount order: the API routes and `/player.html` redirect must match before the catch-all `ServeDir`.
- **Accept-Ranges**: tower-http `ServeDir` implements real `Range` handling (via `http-range-header`) — Python sends the header but ignores Range; port note 1 says byte-identical behavior is a full-body 200, **but** the frontend only relies on the header presence; the Rust side may keep tower-http's real range support (a strict superset that never breaks the UI) OR disable it — decision: keep tower-http's default range support and assert in tests that a `Range` request returns 206 with a correct partial body (Python returns 200 full body; the divergence is client-visible only to range-issuing clients, which the bundled UI never is; document in the porting note).
- **Path escape 403**: Python checks are `str(target).starts_with(str(root))` on fully-resolved paths. Rust: canonicalize/`resolve()` then `Path::starts_with` on the canonical root; on escape → 403 with the exact message (`forbidden` / `asset not found`). Note the run-id split: first path segment = run id (run ids with `/` are unreachable, matching Python's split behavior); a run id of `..` resolves outside → 403.
- **Redirects**: 302 + `Location` only — no body, no Content-Type, no Cache-Control (matches Python `send_response_only`).
- `_serve_file` suffix override beats caller content type: `.js` → `text/javascript; charset=utf-8`, `.css` → `text/css; charset=utf-8`, `.html` → `text/html; charset=utf-8` + no-store; `.json` has no override (cues.json keeps `application/json; charset=utf-8`). Errors: `text/plain; charset=utf-8` + Content-Length, no Cache-Control.

---

## 4. Integration notes

- **Report dir layout read by the web module** (all under `<root>/.agent-sim/reports/<run_id>/`): `events.jsonl` (append-only, one JSON object per line — the live-run fallback and all cue builders read this), `summary.json` (status/counts/verdicts/tool_calls/metrics), `meta.json` (`audio.t0_mono_ms`, `audio.channels`, `scenario_id`, `config_snapshot.observe_gaps`), `conversation.wav` (16 kHz PCM16 stereo L=sim/R=agent; `has_audio` and `duration_ms` depend on it), `cues.json` (written by the web layer itself). SQLite `runs.sqlite` is **not** read by the web layer (list runs derives from report dirs + summary.json — `list_run_ids` only lists dirs containing `events.jsonl`).
- **Live-run behavior**: while a run is in progress (no `summary.json`), `/api/runs` derives status/turn/tool counts from `events.jsonl` (partial-scan semantics, §1.1). `cues.json` is **regenerated on every request** to `/api/runs/<id>`, `/api/runs/<id>/cues`, and `/runs/<id>/cues.json` — never cached (`Cache-Control: no-store`); must match or a stale-file race appears for in-progress runs.
- **Pre-warm**: `start_web_server` with a `run_id` whose report dir exists writes `cues.json` once at startup.
- **`behavior_summary` recompute**: when summary lacks `caller.behavior_summary` and `behavior_summary`, recompute via `script::build_caller_behavior_summary(events)` — a shared ops/script-module function (ported in P3/P4), called lazily exactly like Python's lazy import.
- **Return values**: `start_web_server(host=127.0.0.1, port=8765, open_browser=true, run_id=None, blocking=true) -> {"url", "base_url", "host", "port", "run_id", "runs", "reports_dir", "player_dir"}`; `url = {base}/?run={run_id}` if run_id else `{base}/`; non-blocking adds server/thread keys (Rust: a `join_handle`). `open_browser` false skips the browser open; browser-open failure swallowed. Blocking prints `Open: {url}` + `UI assets: {player_dir}`.

---

## 5. Constants checklist (verbatim — every value)

| Domain | Constant | Value |
|---|---|---|
| timing | cue tail / end-hint cap / start floor / start hard cap / min width | 350 / final+800 / final-400 / final-200 / 200 |
| timing | `_clamp_end` min span | start+120 |
| timing | `mono_to_audio_ms` drop window | > duration+2000 → None |
| dedupe | max_delta user similar cross-source / agent / similar / not-similar | 15000 / 6000 / 4000 / 2500 |
| dedupe | tie sort | (final_ms, rank, agent-before-user); break (not continue) on delta > max_delta |
| ghost | provider sources / window / never dropped | {sim.gemini, sim.openai} / ±2500 ms / sim.script + non-user |
| windows | active-speaker gap / window tail / merge | 2800 / +600 / +1500 |
| windows | interim window / prefix truncation | [final−est−3000, final−500] / 12 chars |
| windows | utterance estimate | agent 95 ms/word, user 85 ms/word; empty 800/600; clamps (700,22000)/(500,14000) |
| markers | barge span (during/not) | 2200 / 1400; `max(span, inj_dur+400)` |
| markers | non-barge cue span | `max(400, min(waited,2000) or 400, inj_dur+200)` |
| markers | inject duration fallback (non-room_pcm / room_pcm) | 2200 / 800; end = start + max(200, dur) |
| markers | recovery / interruption / audio onset / user audio source | +800 / +500 / +300 / +300 |
| markers | silence_wait span fallback / end | 1500 / start+200 |
| markers | silence span fallback | 4000 |
| markers | inject-near window / same bonus | ±900 ms / −200 score |
| tools | tail / band min / band cap | 500 / 400 / 400; end = base + max(400, min(dur, 400)) or base + 500 |
| speech_origin | marker proximity | ±8000 or ±1200 or interval overlap |
| speech_origin | script-match delta / barge / tiny / cue / late-STT | (−800, 15000) / (−500, 15000) / (0, 3500) / (0, 8000) / ≥ −500 |
| speech_origin | scores | 100 − min(40, delta/400); 70 − min(30, delta/200); 50 |
| speech_origin | pin window | start inject−80; end max(final+500, inject+audio+350); audio fallback 2200 (barge) / 900 |
| speech_origin | synthetic cue | dedupe <600 ms; audio fallback max(1200, end−inject); end max(inject+audio+350, inject+1200); start inject−80; `synthetic: true, source: "sim.script", marker_tags: ["barge_in"]` |
| sort | markers final | (start_ms, type-string alphabetical) |
| sort | cues final | (start_ms, agent-before-user) |
| sort | tool spans | (start_ms, name) |

Python-specific semantics to replicate deliberately: `int(x * 0.45)` and `len(x) / 4` are floor ops; `max(1, int(min(ta,tb) * 0.5 + 0.5))` is round-half-up; `int(frames * 1000 / rate)` = float division then truncation; sort keys are tuple-key comparisons, not piecewise (Python/Rust stable sorts match); `bool("false")` is true; `int(3.5) == 3`.

---

## 6. Tests

| Name | Intent |
|---|---|
| `parity_cues_golden` | Rust `build_cues_payload` output byte-diffed against Python `web/cues.py` output for the R5 real-run fixture and one live/in-progress report dir (all fields incl. marker order, dedupe outcome, synthetic barge cues, `marker_counts`, tool bands, empty-state payload) |
| `test_web_routes` | Every §1 route: 200/302/403/404 statuses, exact headers (Content-Type, Cache-Control no-store, Accept-Ranges on audio), redirect Location-only, `/api/runs` element shape + sort order, live-fallback status derivation (running/queued/done, partial-scan abort semantics), run-id split behavior, `cues.json` regeneration freshness |
| `test_cues_constants` | `source_rank` table (incl. opaque data-topic 1/0 and fallback 9), `texts_similar` vectors (apostrophes, punctuation, alias pairs okey/okay, round-half-up boundary), `estimate_utterance_ms` boundaries (empty, clamps, division), `_mono_to_audio_ms` + `_clamp_end` |
| `test_markers` | Every marker kind's span/end/label/detail/prefix per §markers spec; inject duration fallback 2200/800; barge span 2200/1400 vs non-barge formula; recovery consumes first agent final per barge point; final (start_ms, type) ordering |
| `test_speech_origin` | `_mostly_script_say` all branches; `_text_overlap` with `_CONTENT_STOP`; scoring table (100/70/50); tiny + late-STT fallbacks; `_pin_script_window`; full-line preference; `_synthetic_script_barge_cues` dedupe (<600) and fields |
| `test_transcript_dedupe` | 15000/6000/4000/2500 windows; break-on-overflow; longer-text-wins ties; ghost-STT filter (fires only with provider user; drops only agent-side STT; keeps sim.script) |
| `test_static_serving` | rust-embed asset resolution, package_web_dir mtime precedence (`>=`), suffix override, no-store set, missing-index.html refusal, `.html` override no_store |
| `test_range_requests` | tower-http ServeDir range support: 206 partial body, `Accept-Ranges: bytes`, full-body 200 without Range (documented divergence from Python's header-only) |
| `test_api_errors` | REST error mapping (400/404/500 + body formats), `int(repeat)` coercion incl. `"abc"` → 400, `bool(connectivity)` truthiness, unknown-route bodies, health has no `version` key |
| `web_logging_filter` | report server logs only 404-bearing lines; API logs only 4xx/5xx (grep the tracing output) |
