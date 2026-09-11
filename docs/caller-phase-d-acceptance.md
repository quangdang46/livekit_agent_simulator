# Phase D acceptance checklist — caller contract survives real conversations

Date: 2026-09-11. Scenario: `contract-prodval-phase-d`
(`ask → negotiate/price → arrange_visit → end`, agent `agent_au_used_car_dealership`).
Target reports dir: `voice-ai-agent/.agent-sim/reports/`.

Status convention: ✅ proven on a real run · ⬜ not yet observed · ❌ blocked.
A criterion passes only on run evidence, never on unit tests alone
(unit coverage is necessary but listed separately).

## A. Caller-side validation (the hard boundary)

| # | Criterion | Status | Evidence |
|---|---|---|---|
| A1 | Every published utterance was VALID under its own behavior | ✅ | runs 016–024: `contract.published` only ever follows a VALID `attempt_verdict`; Audit B mutation-checked |
| A2 | Drifted generations are rejected, never published | ✅ | run 017 (`SEMANTIC_ACT_MISMATCH`), E2E `test_contract_e2e_hard_boundary` |
| A3 | Retry exhausts to CALLER_BEHAVIOR_VIOLATION, never "speak anyway" | ✅ | runs 016–020, 022–023 `behavior_violation` events |
| A4 | Rejected candidates never reach TTS/PCM/mixer | ✅ | Audit B (`b426991`), spy-tested + mutation-checked |
| A5 | Forbidden intents (`financing`, `vehicle_change`, `trade_in`) never published as caller intent | ✅ | no forbidden caller line in any run 015–024 transcript |

## B. LiveKit / audio path

| # | Criterion | Status | Evidence |
|---|---|---|---|
| B1 | Published PCM reaches the mixer and the agent hears it | ✅ | runs 016–024: agent replies reference caller content; `contract.published` bytes > 0 |
| B2 | `say:` authored lines publish byte-faithful | ✅ | every run: `say:1` 250974 bytes |
| B3 | `do:` publishes the exact validated string | ✅ | run 019 vs 020: published text == validated utterance |
| B4 | sherpa-first TTS with OS fallback works on a real run | ⬜ | `_synthesize` covered by unit tests only; no run has proven which branch fired |
| B5 | Fresh-machine TTS (model download/cache) | ⬜ | never exercised |

## C. Validator availability (Tier-1 on the run path)

| # | Criterion | Status | Evidence |
|---|---|---|---|
| C1 | No run dies on verifier availability | ✅ since `9a8e8d3` | run 015 (3x VERIFIER_UNAVAILABLE) → pinned Tier-1; runs 016–024 have zero availability failures |
| C2 | Tier-3 judge stays offline-only | ✅ | `benchmark_semantic_judge.py` 10/10 golden, untouched by the pin |
| C3 | Attempt trail carries the attempted utterance | ✅ since `e3c9353` | runs 020–024 `attempt_verdict` specs include `utterance` |

## D. Lexicon recall (evidence-driven, runs 016–024)

Each row: run → miss → fix commit. Every pattern carries a `source` field in
`semantic_lexicon_run_failures.json` (16 cases); both languages enforce it.

| Run | Miss | Fix |
|---|---|---|
| 016 | "Is there any flexibility on the price?" → 0.2 | `d735c0e`: flexib / willing to / go lower |
| 017–018 | visit-phrased arrange_visit → 0.2 | `d735c0e`: visit / stop by / take a look / come-and/to-see/look |
| 020 | ask-as-declaration ("I'm asking...", "provide the price") → 0.2 | `1f8908a` |
| 021 | agent price answer in words missed by evaluator | `b794010`: `_PRICE_STATEMENT_PATTERNS`, scoped to target==price |
| 022 | "I'm specifically interested in the price..." → 0.2 | `15ceb21`: interested in |
| 023 | "find out the price" / "confirm the price" / "asking for" → 0.2 | `54dbabf`: find out + asking for + (confirm,price) pair marker |

Blast-radius rule (applied every round): no hijack of another tier —
confirm tier, negotiate tier, financing detection, saturday golden all
re-verified unmoved before each commit.

## E. Agent-side satisfaction (evaluator)

| # | Criterion | Status | Evidence |
|---|---|---|---|
| E1 | Agent stating the price satisfies ask/price | ✅ since `b794010` | run 019 agent transcript verbatim in parity fixture; run 026 ("listed at $25,800" → ask satisfied) |
| E2 | Agent deflection does NOT satisfy (budget exhausts correctly) | ✅ | run 021 (`FAILED_MAX_TURNS` on "What can I help you with today?" x2), run 024 |
| E3 | A full 4-behavior call completes against a cooperative agent | ✅ run 026 | `026-contract-prodval-phase-d`: status done, `contract_scenario_end`, 6 turns — ask VALID x2, negotiate VALID x2 (incl. the run-016 flexibility phrasing), arrange_visit VALID, end clean |

## F. Assertions / reporting on the contract vocabulary

| # | Criterion | Status | Evidence |
|---|---|---|---|
| F1 | Recovery asserts count contract barges | ✅ | `c24ddc9`, `is_recovery_barge_event` |
| F2 | `ended_by` understands contract reasons | ✅ | `a178fd3`, `end_side_from_reason` |
| F3 | `interruption_count` counts contract cut-ins | ✅ | `a178fd3` |
| F4 | `script.verify` does not false-fail contract runs | ✅ | `c24ddc9` (`skipped: True`) |
| F5 | A clean Phase D run passes asserts + gate end to end | ⬜ | needs E3 first |

## G. Record / replay and cross-language proof

| # | Criterion | Status | Evidence |
|---|---|---|---|
| G1 | Record a successful run (`--record`) | ⬜ | needs E3 first |
| G2 | Replay it with no AI calls, same verdicts | ⬜ | unit-covered only (`test_record_replay.py`) |
| G3 | Rust Tier-1 parity on the Phase D lexicon | ✅ | `semantic_lexicon_run_failures.json` enforced both sides |
| G4 | Rust live-runtime parity | ⬜ | separate workstream; Rust has primitives, no driver |

## How to run the next attempt

```bash
uv run lks execute contract-prodval-phase-d --root C:/Users/ADMIN/Documents/Projects/voice-ai-agent
```

Requires the `voice-ai-worker-local` agent running. After the run, tick the
boxes above against the new report dir — a new miss goes through the
established loop (utterance log → targeted marker → parity both sides →
regression fixture with `source` → blast-radius check), never as an
unreviewed lexicon dump.

## Residual, out of scope for Phase D

- `silent_mode` compat bridge (needs a new DSL surface; no template demands it).
- `lks optimize` artifacts have no runtime effect on the contract path
  (documented at the call site in `run_orchestrator.py`).
- Bare `saturday` deliberately excluded from arrange_visit (would hijack the
  negotiate/delivery_date golden case).
