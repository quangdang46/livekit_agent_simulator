# AGENTS.md — livekit-agent-simulator

Standalone Python package: MCP + `lks` CLI. Dials **any** LiveKit voice agent using
`.agent-sim/` in a **target repo** (config, scenarios, reports). The agent under test is a
black box — we never import or edit target application code unless the user asks.

---

## Boundary

| In scope | Out of scope |
|---|---|
| `src/livekit_agent_simulator/` | Target agent source, consumer app code, DB, env |
| Scenario JSONL, Script timing, observer, reports | Parsing project-specific dispatch keys in core |
| LiveKit room + dispatch + sim caller | Agent model stack, tools, business rules |

**Opaque dispatch:** `config.yaml` → `livekit.dispatch_metadata` and scenario `Dispatch.metadata`
are passed through as JSON strings. Core Python must not interpret consumer-specific keys.

**Target repo** = path passed as `project_root` / `--root`. Consumer wiring examples live in
`docs/portability.md` — load that file only when the task is target `.agent-sim/` setup, not
for package bugs or features.

---

## Product rule: generic core, not fit-to-one-repo

This package ships **tools + core capabilities** that every LiveKit agent repo can use.
It is **not** a glue layer for one consumer (worker, dashboard, language, brand).

| Do | Do not |
|---|---|
| Build features every target can enable via config / scenario / plugins | Hardcode language, timezone, agent IDs, data topics, or business strings in `src/` |
| Give **extension points** (opaque dispatch, `observe.*`, Script, verify plugins) so users customize | Parse or special-case consumer keys in core Python |
| Put project-specific wiring only in **that target’s** `.agent-sim/` | Change package defaults to match the last repo we smoked |
| Prefer one clear API (`record_audio`, not aliases) | Keep “legacy” flags, dual names, or compatibility shims “just in case” |

**Customization belongs to the user.** We ship knobs and contracts; the target fills
`config.yaml`, scenarios, plugins. If something only works for one monorepo, it is
wrong for core — fix the design or keep it out of `src/`.

**Dev-stage cleanliness (repo is still evolving):**

- No legacy paths. Delete dead config, unused fields, and half-features in the same change.
- Defaults must be **portable** (`en-US` / `UTC` in core; demos may override in templates or target config).
- Docs/examples use neutral placeholders (`yourProjectKey`, `/path/to/target-repo`) — not a real product name as the default.
- Prefer fail-fast or remove over silent multi-provider stubs that only implement one backend.

---

## Product rule: no stubborn patches (defaults first)

A **normal human caller** is the default product, not a special scenario case.

| Do | Do not |
|---|---|
| Prefer existing defaults / one clear gate (e.g. hang_up waits for agent reply) | Stack scenario delays, extra waits, persona constraints, or authoring warns to paper over a bad override |
| If a run feels unnatural, ask: “was a default turned off?” | Treat “don’t hang up right after saying your name” as a one-off backchannel/noise carve-out |
| Fix the **wrong override** or the **broken knob** once | Add compensating sleeps, style flags, or “human reminder” prose in every JSONL |
| Revert stubborn patches when the user says the behavior is just normal | Leave half-fixes in `src/` after the real fix was “leave the default on” |

**Smell test:** if the patch only makes sense for one scenario id or one failure screenshot, it is probably wrong. Delete it and use the default path.

---

## Product rule: no dead features (keep the surface lean)

Do **not** add CLI commands, MCP tools, config knobs, or scenario sections that
nobody (human or agent) actually uses. A feature that exists but is never run is
worse than no feature: it ships bugs, bloats docs/help, and doubles review cost.

| Do | Do not |
|---|---|
| Only implement what has a concrete user: a real run, a test, or a documented recipe | Add a feature "for completeness" or "someone might need it later" |
| Before building, ask: *who runs this? which flow calls it?* If the answer is "nobody", skip it | Keep legacy/duplicate paths (CLI vs MCP alias) that no test exercises |
| When a feature goes unused, remove it or fold it into the existing surface | Ship half-finished knobs (e.g. `compare --baseline` without the P1.D regression gate) |
| Gate new CLI/MCP surface behind at least one test | Grow `lks --help` with commands that have 0 tests and no docs usage |

**Smell test:** if you can't name the flow that calls it, it is dead on arrival.
WIP.md P2.x items (OTel export, multi-party handoff, text-fast mode, …) are
parked for a reason — don't implement them just because they are listed.

---

## Research before implement or fix (mandatory)

Do **not** guess SDK wire formats, Gemini Live quirks, or LiveKit dispatch behavior.
Complete this loop before non-trivial code changes; repeat if verification fails.

```
Hypothesis → Exa / docs → .venv proof → src/ or report → fix → pytest
```

| Order | When | Where |
|---|---|---|
| 1 | Errors, prior art, API changes, regressions | **Exa** (`web_search_exa`, `web_fetch_exa`); note if using web fallback |
| 2 | LiveKit dispatch, rooms, transcription, agents | **LiveKit MCP** (`docs_search`, `get_pages`, `code_search`) |
| 3 | Gemini Live input/output, modalities, close codes | Exa + **`google-genai` in `.venv`** (`site-packages/google/genai/`) |
| 4 | Types / methods actually imported | **Installed packages** in `.venv`: `livekit`, `livekit-api`, `google-genai` |
| 5 | Our behavior vs expectation | `src/` + failing `reports/<run-id>/events.jsonl` |

**Rules**

- If docs and `.venv` disagree, trust **`.venv`** (what we run).
- Cite real paths (file + symbol) in commits and chat — no “the SDK supports X” without proof.
- Re-research when the first hypothesis fails; do not patch gaps with guesses.
- One-line typos / test-only edits: still read the target file; Exa optional.

---

## Default workflow

1. Read this file.
2. Classify: **package code** (`src/`, `tests/`) vs **target `.agent-sim/` only** (scenarios/config).
3. Run the research loop above for anything beyond typos.
4. Minimal diff → verify:

```bash
uv sync --extra dev
uv run pytest -q
```

On Windows, if `uv sync` fails (MCP exe locked):

```bash
.venv\Scripts\python.exe -m pytest -q
```

| Task | Approach |
|---|---|
| Bug / SDK / protocol | Exa + LiveKit MCP + `.venv` → fix → pytest |
| New scenario kind / MCP tool | Research first; plan if large; tests required |
| Target scenario/config only | Edit `<target>/.agent-sim/` — no package release |
| Smoke against running agent | `lks preflight` + `lks execute <id> --root <path>` (same ops as MCP) |

---

## Layout

| Path | Role |
|---|---|
| `config.py` | Load `.agent-sim/config.yaml` |
| `scenario.py` / `script_parse.py` / `script/` | JSONL + timed Script cues (runtime / verify / summary) |
| `script_runner.py` | Re-exports `script` (stable import path) |
| `run_orchestrator.py` | End-to-end run (phased) |
| `livekit/` | Room, dispatch, observer |
| `gemini/` | Sim caller bridge + optional judge |
| `logging/` | Event envelope, SQLite, reports |
| `web/` (repo root) | Web UI — Vite/TS; `pnpm build` → `web/dist/`; CI force-includes into wheel as `web_static` |
| `src/.../web/` | Report player API (`cues`, markers, HTTP server) |
| `mcp_server.py` / `cli.py` | MCP tools + `lks` |
| `templates/` | Init scaffolds |
| `tests/` | pytest |
| `docs/portability.md` | Optional consumer wiring (not default agent context) |
| `docs/smoke-test.md` | First end-to-end run |

---

## Scenario JSONL (`agent-sim/v1`)

```
Scenario → Persona → [Context] → [Simulator] → [Execute] → [Dispatch] → [Script] → [PassCriteria]
```

- **Execute** — run params; overrides Simulator.
- **Dispatch** — opaque metadata for `RoomAgentDispatch`.
- **Script** — timed caller cues (`agent_speaking` + `delay_ms`); `delivery: room_pcm` plays WAV into sim mic; log verify via `script_verify` and optional **plugins** (verify + `before_run` / `after_run` — `docs/plugins.md`).
- **PassCriteria** — optional LLM judge rubric.
- **Context.notes** — author-only (reports/docs); **not** injected into the caller SI.
- **Context.caller_knows** / **world** — optional facts the persona already knows (injected).

### The two caller paths

| Key | Status |
|---|---|
| **`caller_steps`** (`say` / `do` / `wait` / `dtmf` / `interrupt` / `end`) | The contract path, and the **default**. Every scenario carries it. |
| **`responses:`** | Opt-in response catalog. Routes via the Decision Router. |

They coexist, and **`caller_steps` does NOT win** — the router runs whenever
one is attached. `caller_steps` supplies the opening action and stays
mandatory; the driver's gate (`router.py:712`) checks only
`router is not None and response_catalog is not None and agent_text`, with no
reference to `caller_steps` at all. Every migrated `gpt-live-*` scenario
carries both keys, which is why they route.

This was documented the other way round in four places for a while, one of
them a Rust guard comment — corrected 2026-09-30 after the peer session
counted 11 reports containing `contract.router_decision`.

⚠️ **Two things to know before touching the routed path:**

1. The Contract Validator is **bypassed** on a routed turn (synthetic
   `ValidationResult(VALID, reason="ROUTED")`). This is a deliberate carve-out
   from the "no unvalidated utterance reaches the agent" invariant: the
   routed line is authored ground truth, so validating it means the harness
   grading its own fixture — which fails closed and turns an agent bug into
   `CALLER_BEHAVIOR_VIOLATION`. **Do not "restore" the validator here.**
   Rationale: `NEW_ARCHITECTURE_FOR_LKS_AND_LKSR.md` §27.7.
2. `responses:` requires a `router:` block in `.agent-sim/config.yaml`, and
   its absence is a **`ConfigError` naming the scenario** — never a silent
   fall back to `caller_steps`.

Docs: [docs/migration-caller-steps-to-responses.md](docs/migration-caller-steps-to-responses.md),
[docs/router-prompts.md](docs/router-prompts.md), working example
`templates/examples/router-smoke.yaml`.

### ⚠️ The routed path is PYTHON-ONLY

`lksr` does **not** execute router scenarios. `scenario.rs` `KNOWN_KINDS` has no
`"Responses"` and `scenario_jsonl.rs:185` hard-rejects unknown kinds, so a
`responses:` scenario is **rejected** under `lksr` rather than silently run
legacy. No `lksr` user is stranded: `caller_steps` is mandatory and wins when
both are present, so a dual-key scenario executes the `caller_steps` path under
`lksr` and the router is simply not engaged.

Do not port the router to Rust in v1 — `caller_contract.rs` carries ~1000 lines
with zero references outside that file, so porting means wiring dead code, which
this file forbids. Full reasoning, including the two Rust-only behaviours that
survive on the surface `lksr` keeps, is in
`docs/plans/response-router.md` Appendix A §7.

**Testing a change to the router:** the router is attached in
`live_wiring._attach_response_router`, called from
`run_contract_driver_path`. Unit tests that inject `driver.router` by hand
prove the branch works but NOT that anything attaches it — that gap is how
the router shipped un-attached with every test green. Drive
`run_contract_driver_path` (see
`tests/test_contract_live_wiring.py` and
`tests/test_router_smoke_template.py`) and mutation-verify.

---

## Hard rules

- No target-repo application code changes unless explicitly requested.
- No consumer env vars in `pyproject.toml` or core config schema.
- Credentials only in target `.agent-sim/config.yaml` (gitignored).
- Core stays **repo-agnostic**; consumer fit only under target `.agent-sim/` (or docs examples).
- No legacy shims / dual config names — clean breaks are fine while pre-1.0.
- **No stubborn patches** — defaults first; do not compensate with scenario/authoring hacks (see above).
- **No dead features** — only build what has a real user flow; don't add CLI/MCP/config surface nobody runs (see above).
- **pytest must pass** before reporting done.

---

## Naming

| Item | Value |
|---|---|
| Package | `livekit-agent-simulator` |
| CLI | `lks` |
| MCP entry | `lks mcp` (console script `lks-mcp`) |
| Dot folder (target) | `.agent-sim/` |
| Sim participant | `lks-caller` |
| Room prefix | `lks-<run-id>` |

<!-- bv-agent-instructions-v4 -->

---

## Beads Workflow Integration

This project uses [beads_rust](https://github.com/Dicklesworthstone/beads_rust) (`br`) for issue tracking and [beads_viewer_rust](https://github.com/quangdang46/beads_viewer_rust) (`bvr`) for graph-aware triage. Issues are stored in `.beads/` and tracked in git. Current `br` workspaces normally export `.beads/issues.jsonl`; older `bd`/legacy workspaces may use `.beads/beads.jsonl`. `bvr` auto-discovers the supported JSONL files, so agents should use `br`/`bvr` commands instead of hard-coding a single filename.

### Using bvr as an AI sidecar

bvr is a graph-aware triage engine for Beads projects. Instead of parsing .beads/issues.jsonl / .beads/beads.jsonl directly or hallucinating graph traversal, use robot flags for deterministic, dependency-aware outputs with precomputed metrics (PageRank, betweenness, critical path, cycles, HITS, eigenvector, k-core).

**Scope boundary:** bvr handles *what to work on* (triage, priority, planning). `br` handles creating, modifying, and closing beads.

**CRITICAL: Use ONLY --robot-* flags. Bare bvr launches an interactive TUI that blocks your session.**

#### The Workflow: Start With Triage

**`bvr --robot-triage` is your single entry point.** It returns everything you need in one call:
- `quick_ref`: at-a-glance counts + top 3 picks
- `recommendations`: ranked actionable items with scores, reasons, unblock info
- `quick_wins`: low-effort high-impact items
- `blockers_to_clear`: items that unblock the most downstream work
- `project_health`: status/type/priority distributions, graph metrics
- `commands`: copy-paste shell commands for next steps

```bash
bvr --robot-triage        # THE MEGA-COMMAND: start here
bvr --robot-next          # Minimal: just the single top pick + claim command
```

Before claiming, verify current state with `br show <id> --json` or `br ready --json`. `recommendations` can include graph-important blocked or assigned work; only `quick_ref.top_picks` and non-empty `claim_command` fields represent claimable work.

#### Other bvr Commands

| Command | Purpose |
|---------|---------|
| `bvr --robot-insights` | Deep graph analysis: PageRank, betweenness, HITS, k-core, critical path |
| `bvr --robot-plan` | Dependency-respecting execution plan with parallel tracks |
| `bvr --robot-priority` | Priority misalignment detection |
| `bvr --robot-alerts` | Stale issues, blocking cascades |
| `bvr --robot-suggest` | Smart suggestions: duplicates, missing dependencies, labels |
| `bvr --robot-graph` | Dependency graph export (JSON/DOT/Mermaid) |
| `bvr --robot-search <query>` | Semantic search over issue titles/descriptions |
| `bvr --robot-history` | Bead-to-commit correlation from git history |
| `bvr --robot-label-health` | Per-label health metrics |
| `bvr --robot-schema` | JSON Schema definitions for all robot outputs |

#### br Quick Reference

```bash
br list                          # List all beads
br ready                         # Actionable beads (no open blockers)
br show <id>                     # View bead details
br update <id> --status in_progress  # Claim work
br update <id> --status closed   # Complete work
br dep add <id> <target>         # Add dependency
br sync --flush-only             # Sync changes to issues.jsonl
```
<!-- end-bv-agent-instructions -->

