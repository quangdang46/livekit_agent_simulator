# Review of the Rust full-port plan — cross-checked against repo ground truth

> Companion to `docs/plans/PLAN-20260813-rust-full-port.md` and the three appendices
> (A web layer, C wire protocols, D phases/edge-cases) plus `rust-port-research.md`.
> This file records an independent verification of the plan's claims against the **current**
> repo state (Python source, real run artifacts, templates, web/dist) on 2026-08-13.
> **Verdict: the plan is high-quality and implementable.** It is accurate on almost every
> head, and every discrepancy found below is small, localized, and fixable with one edit each.
> None of them invalidates the phased approach. They must be corrected before P3/P3.5/P5
> kickoff, because all three rely on the affected contracts.

---

## Summary of verified claims (all correct)

- **21 MCP tools** — confirmed: 21 `@mcp.tool` markers in `mcp_server.py`.
- **~25.1 kLOC / 105 files** — confirmed: 25,115 LOC / 105 `.py` files.
- **web/dist ~200 KB** — confirmed: 203,267 bytes measured. (But see §1 on the file count.)
- **conversation.wav 16 kHz** — confirmed; real run `meta.json` + `metrics.py` ground truth.
- **`list_runs` limit default 20** — confirmed (`sqlite_store.py:172`, `ops.py`).
- **Default voice `Puck`** — confirmed (`config.py`).
- **Metrics key set** — the **31-key** block in `summary.json` is byte-confirmed against a real
  report. **This is the correct target for byte-parity (I1).**
- **`publish_dtmf` / `SipDTMF` API** and **Gemini `SpeechConfig.language_code` patch point**
  — consistent with the crate evidence in the research doc.
- The three appendices (A/C/D) are consistent with the main plan and with each other on
  routes, markers, timing constants, event specs, phase assignments, and edge cases.

---

## Findings (verify-then-fix; each is one targeted edit)

### 1. metrics block: the plan's own "31-key" vs "36-key" contradiction (P3 step 4)

The **report ground truth is 31 keys** — confirmed byte-for-byte in a real
`summary.json` (e.g. `demo/base-agent/.agent-sim/reports/001-frontdesk-hours-*`).

But the **Python source** (`metrics.py`) now unconditionally returns **5 extra keys** —
`ttfa_run_ms`, `ttfa_source`, `turn_taking_audio_ms`, `user_audio_source_count`,
`agent_audio_onset_count` — added by commit `504577a` (2026-08-12, *after* the Aug-11
reports). They are NOT gated on `observe.audio_onset.enabled` and NOT omitted when empty.

- The plan's P3 step 4 says the block is "31 keys (REVISED)" **and in the same sentence**
  lists the 36-key superset as if it were also true. That is internally contradictory.
- The **byte-compat target must be decided once**:
  - I1 (byte-compatible `summary.json`) → the plan should pin **31 keys** (what real runs
    produce today) **or** pin **36 keys** (what `metrics.py` produces now). It can't be both.
  - Recommendation: **pin 36** and regenerate the parity fixture from a *fresh* Python run
    (see §2 — the R5 fixture is stale/missing). The 5 extra keys are present in every
    Python run today; byte-parity with a Rust run against the same Python version requires
    them. If 31 is chosen, parity with current Python fails immediately.
- Single edit: P3 step 4 — state the final count (recommend 36) and the source of truth
  (`metrics.py` dict literal), and drop the "31" phrasing.

### 2. R5 ground-truth fixture does not exist in this repo

`demo/dtmf-feature/.agent-sim/reports/114-people-pleaser-refuse-card-20260809-201652-8b32/`
is referenced as the golden fixture by the plan (P3.5), Appendix A §acceptance, and the
research doc (R5). **It is not present** (searched the whole repo; also no `114-*` dir).

- The plan's own cross-validation strategy says "capture into `tests/golden/` before the
  phase" — that capture was never done.
- Action before P3/P3.5 parity: re-run the same scenario (or `demo/base-agent`'s real runs,
  which exist and are newer) under Python and commit `events.jsonl`/`summary.json`/
  `meta.json`/`cues.json` + `conversation.wav` as the golden fixture, per Cross-validation §1.
- Single edit: swap the fixture path in P3.5 + Appendix A to the actually-captured fixture.

### 3. MCP `compare_runs` tool does NOT take the 7 gate params the plan lists (P5 step 1)

The plan's P5 step 1 lists MCP params `max_ttfa_regression_ms` (2000) /
`max_turn_audio_p95_regression_ms` (2500) / `require_status_done` (default true) — but these
exist **only** on the internal `ops.evaluate_baseline_gate` and are **never surfaced on any
Python public path**:

- `mcp_server.py` `compare_runs` → **4** gate params + `baseline` (1500/2000/30000/0).
- `cli.py` `compare` → same **4** `--max-*-regression-ms` flags (no `--max-ttfa-*`, no
  `--max-turn-audio-*`, no `--require-status-done`).
- `ops.compare_runs_with_baseline` → same **4**.
- The 7-param `evaluate_baseline_gate` is called from exactly one site
  (`ops.py:1011`) with only the 4 — the extra 3 silently use defaults 2000/2500/true.

Since the contract (I7) is "identical to the Python surface", the Rust MCP tool must match
**4 gate params + `baseline`**, and the CLI must match **4** flags. (The 7-param gate can
still be the internal Rust implementation — matching Python's internal `evaluate_baseline_gate`
— but the 3 extra must not appear on the tool/CLI surface.) Single edit in P5 step 1.

### 4. CLI command count is 23, not 22 (research + P5 §CLI/MCP surface)

Confirmed: **23** `@app.command` (22 data commands + `mcp`). The plan/research say 22 and
treat `mcp` as "plus the mcp subcommand". Cosmetic, but a tool-surface golden test would
count 23 — fix the count in the plan's surface list (and the invariant I7 "22 CLI commands").

### 5. web/dist is 6 files, not 4 (cosmetic)

`web/dist` now has **6** entries: `index.html`, `favicon.svg`, `icons.svg`,
`assets/index-DNs624kh.js`, `assets/index-DNs624kh.js.map`, `assets/index-EYhUFLj5.css`
(~203 KB). Plan/research say "4 files". Update the count; size claim stays correct.

---

## Consistency notes (no action needed, but worth being aware of)

- **`observe:` duplicate key in `demo/dtmf-feature/.agent-sim/config.yaml`** — the demo
  config has two `observe:` blocks (the second overrides `silence_threshold_ms`). Not a plan
  defect, but the plan's `config_snapshot` key-order contract assumes one block; the demo
  file is the exact input P1 golden tests will parse. Worth confirming which block wins
  under `yaml_serde` (last-wins) matches PyYAML.
- **Appendix D §1 P1 item 9**: "folders: mkdir reports_dir + scenarios_dir" is consistent
  with the plan. No issue.
- **Cross-validation strategy** and **Test strategy** (§499–578) are sound; they rely on the
  (missing) fixture in §2, so that is the one dependency to land first.

---

## Architecture change (2026-08-13 — fine-grained workspace, applied to plan)

The original plan's D1 (single crate `crates/lks`) was superseded by decision during the
spec/architecture review. The workspace is now split at the Python import seams:

```
crates/lks-core/       pure logic (config, scenario*, script models/parse/verify/summary, metrics,
                       asserts, suite, evals, optimize, persona/behavior, logging/sqlite,
                       telephony checks, + CallerBridge/SimLeg traits) — NO livekit, NO pyo3
crates/lks-livekit/    media/network (room, dispatch, observer, agent_session, sim_legs, callers,
                       audio/*, script/runtime, run_orchestrator, preflight)
crates/lks-web/        axum report server (8765) + REST API (8787) + cues pipeline + rust-embed web/dist
crates/lks-mcp/        rmcp server (21 tools)
crates/lks-plugins/    pyo3 embedded CPython (existing .py verify plugins)
crates/lks/            binary (main.rs thin, cli/cli_render/ops)
crates/gemini-live/    vendored fork (real dependency, own Cargo.toml)
crates/agent-sim-proto/ prost-generated AgentSessionMessage
```

Key consequence: **P0/P1 build and test without building libwebrtc** (`cargo test -p lks-core`),
because `livekit`/libwebrtc only enters via `lks-livekit` from P2. This is the primary
motivation, not ceremony. Dependency direction mirrors Python's import graph
(`ops.py`/`web`/`mcp` → `run_orchestrator`/`preflight` → `livekit`).

Plan edits applied: D1, §Summary "We recommend", Architecture workspace block (+ block-level
module→crate map), Python→Rust mapping note, P0 Files/Steps/Deps, P1 Files, P2 Files, P3 Files,
P3.5 Files, P4 Files, P5 Files, P6 Files, P7 Files, P8 Files, P9 Files, P10 Files (web/dist →
`lks-web/assets/web/`), Dependencies table ownership note, Open question 1 (resolved).

## Recommended fix list (by phase)

| Where | Fix | Status |
|---|---|---|
| P3 step 4 | Resolve 31 vs 36 metrics keys — pin **36** (matches current `metrics.py`), regenerate parity fixture | **APPLIED** (plan P3 step 4 + I1 + App B §5 + research R5) |
| P3.5 + App A + research R5 | Point golden fixture at a real captured report (re-run + commit to `tests/golden/`) | **APPLIED** — fixture path updated to "fresh Python capture" (plan P3.5 test, App A acceptance, research R5, Cross-validation §1); outstanding HUMAN action: re-run a current scenario under Python and commit `tests/golden/` before P3/P3.5 |
| P5 step 1 | MCP `compare_runs` = 4 gate params + `baseline` (drop `max_ttfa_*`/`max_turn_audio_*`/`require_status_done` from the tool surface) | **APPLIED** (plan P5 step 1) |
| P4 CLI compare | Same: 4 `--max-*-regression-ms` flags, no extras | **APPLIED** (plan P4 step 5) |
| I7 / §CLI surface | "22 CLI commands" → "23 (22 data + mcp)" | **APPLIED** (I7, §221 count, plan header) |
| Research §5.4 / plan deps | web/dist "4 files" → "6 files (~200 KB)" | **APPLIED** (research R5/R6 + deps table) |

No other claim in the plan, research doc, or appendices A/C/D failed verification.

## Outstanding human action

- **Capture the golden fixture before P3/P3.5.** Run a current scenario under the Python
  package (`lks execute ...`) and commit the resulting `events.jsonl`, `runs.sqlite`,
  `summary.json`, `meta.json`, `cues.json`, `conversation.wav` into
  `src/livekit_agent_simulator_rust/crates/lks/tests/golden/`. The Aug-11 `demo/base-agent`
  reports exist but are a **31-key** fixture (pre audio-onset commit `504577a`) and cannot
  stand in for `summary.json`/`metrics` parity. The template scenarios (`templates/*.yaml`
  + `.jsonl`) are the correct input for P1 golden tests.
