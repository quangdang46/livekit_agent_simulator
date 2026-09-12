# Caller Runtime state machine (§29.3 of NEW_ARCHITECTURE_FOR_LKS_AND_LKSR.md)

Canonical standalone spec artifact for the §29.1 pipeline. Every state,
event, transition, timeout, cancellation, and failure below traces to
existing code on `develop` — this document adds no new behavior, only the
single-blueprint view §29.3 calls for. Code references are Python (`lks`);
the Rust port (`lksr`) implements the same machine under the shared
behavioral contract (see §7).

Pipeline reference (§29.1):

```text
Scenario → Behavior Engine (WHAT) → Orchestrator (WHEN) → AI Language Adapter (HOW)
→ Caller Contract Validator (HARD BOUNDARY) → Interaction Planner (DELIVERY)
→ TTS / DTMF / Control → LiveKit → Agent → Observer → Behavior Evaluator → loop
```

The two absolute invariants (§29.2) guard every transition that publishes:
**#1** no audio reaches LiveKit without a `VALID` verdict;
**#2** every generated action carries the current
`(behavior_id + turn_id + generation_id)` triple — stale actions must not execute.

## 1. States

### 1.1 Call-level (owned by `ContractCallerDriver.run`, `driver.py:192-281`)

| State | Meaning | Code |
|---|---|---|
| `IDLE` | Before `run()` enters; after `run()` returns | implicit |
| `FIRST_SPEAKER_WAIT` | `first_speaker="agent"`: waiting for the greeting before the first caller action | `driver.py:240-256` |
| `RUNNING_ACTIONS` | Iterating `caller_actions` via `_run_actions` | `driver.py:266-277` |
| `HOLD_WATCHING` | Orthogonal: `_hold_watchdog` task armed (only when `hold_timeout_s` set) | `driver.py:258-264,283-323` |
| `DONE` | All actions completed, no failure | return path |
| `FAILED` | A `DriverResult` with `failure` set (any of the 7 codes) | `_fail()` paths |

### 1.2 Per-behavior (owned by `_run_behavior`, `driver.py:592-912`)

| State | Meaning | Code |
|---|---|---|
| `BEHAVIOR_START` | `start_behavior()` reset the per-behavior turn count | `driver.py:621-623`, `orchestrator.start_behavior()` |
| `GENERATING` | `adapter.generate_candidate()` in flight (bounded: `max_retries + 1` validator attempts; transport errors break immediately as `LANGUAGE_GENERATION_ERROR`) | `driver.py:672-678` |
| `VALIDATING` | `validator.validate()` on the candidate; verdict recorded in `_attempt_verdicts` trail; `assert_verdict` fires in replay mode | `driver.py:680-708` |
| `VIOLATION_CHECK` | Non-VALID verdict → `CALLER_BEHAVIOR_VIOLATION` (INVALID/UNKNOWN/ERROR after retries) | `driver.py:748-763` |
| `STALENESS_CHECK` | `orchestrator.is_stale()` after validate AND after TTS, before publish; stale → `stalled_spins += 1`, cap → `TRANSPORT_ERROR` | `driver.py:766-776,811-821` |
| `SILENCE_GATE` | `_wait_agent_silence()` (bounded `AGENT_SILENCE_WAIT_S = 6.0s`): non-barge `do:` waits for the agent to go silent before synthesizing (run 026 fix) | `driver.py:784-791` |
| `SYNTHESIZING` | `self._speak(text)`; `TTSSynthesisError` → `TTS_ERROR` | `driver.py:796-810` |
| `PUBLISHING` | `sink.publish()` (staleness re-check + drain); `PublishDrainTimeout` → `TRANSPORT_ERROR`; refused publish → stalled-spin path | `driver.py:822-847`, `publish_sink.py` |
| `WAITING_AGENT` | `asyncio.sleep(1.0)` settle (outside the agent timeout budget) + `_wait_agent_turn_with_policy(timeout_s=30.0)`; `None` → `AGENT_TIMEOUT` | `driver.py:855-889` |
| `EVALUATING` | `orchestrator.evaluate_behavior()` → `SATISFIED` returns `(turns, agent_text)`; otherwise loop back to the `check_max_turns` gate | `driver.py:890-898` |
| `BEHAVIOR_TIMEOUT` | Gate returned `FAILED_MAX_TURNS` → single canonical `BEHAVIOR_TIMEOUT` exit | `driver.py:899-912` |

### 1.3 Turn detection (owned by `TurnDetector`, `orchestrator.py:41-106`)

`WAITING_FOR_AGENT` → `AGENT_SPEAKING` (audio start) → `POSSIBLE_END`
(audio stop, inside debounce window) → `AGENT_TURN_COMPLETE` (debounce
expiry via `poll()`) → `CALLER_TURN` (caller takes the turn) →
`WAITING_FOR_AGENT`. Turn logic lives in LKS, never in the LiveKit SDK.

### 1.4 Say/do/control actions (owned by `_run_actions`, `driver.py:326-434`)

`say:` (bypasses AI+validator by construction) and control actions
(`wait`/`silence`/`dtmf`/`hangup`/`end`, Realtime never involved) run
inline; only `do:` traverses the full GENERATING → EVALUATING chain (§27.3).

## 2. Events

| Event | Source | Consumer |
|---|---|---|
| Agent audio started/stopped frames | LiveKit track → `TurnDetector` | `WAITING_FOR_AGENT` ↔ `AGENT_SPEAKING` ↔ `POSSIBLE_END` |
| Transcript received (`transcript.*.final/interim`, any source) | `Observer.on_transcript` | turn accounting, `ObserverAgentWait` tiers |
| Validator verdict (`VALID`/`INVALID`/`UNKNOWN`/`ERROR`) | `validator.validate()` | `VIOLATION_CHECK`, `_attempt_verdicts` trail |
| TTS result / `TTSSynthesisError` | `self._speak(text)` | `TTS_ERROR` branch |
| LiveKit publish result / `PublishDrainTimeout` | `sink.publish()` | `TRANSPORT_ERROR` branch |
| Behavior evaluator verdict (`SATISFIED`/`PARTIAL`/`NOT_SATISFIED`) | `evaluate_behavior()` | loop-back vs return |
| Interrupt/barge-in signal | seeded policy via `_wait_agent_turn_with_policy` (backchannel `"Mhm."`) | mid-answer cut-in, wait continues |
| DTMF / hangup / end | control actions | direct LiveKit/SIP, no AI/TTS |
| Timeout expiry (turn / behavior / call) | `TimeoutConfig` hierarchy (§3) | corresponding failure branch |

## 3. Transitions (per the §29.1 pipeline, every audio-publish edge guarded by invariants #1+#2)

```text
BEHAVIOR_START → GENERATING → VALIDATING → [VALID] → STALENESS_CHECK
  → SILENCE_GATE → SYNTHESIZING → STALENESS_CHECK → PUBLISHING → WAITING_AGENT
  → EVALUATING → [SATISFIED: return | else: BEHAVIOR_START (next turn)]
GENERATING --transport error--> LANGUAGE_GENERATION_ERROR (terminal)
VALIDATING --retries exhausted non-VALID--> CALLER_BEHAVIOR_VIOLATION (terminal)
STALENESS_CHECK --stale, spins capped--> TRANSPORT_ERROR (terminal)
SYNTHESIZING --TTSSynthesisError--> TTS_ERROR (terminal)
PUBLISHING --drain timeout/refused, spins capped--> TRANSPORT_ERROR (terminal)
WAITING_AGENT --30s no reply--> AGENT_TIMEOUT (terminal)
EVALUATING --budget exhausted--> BEHAVIOR_TIMEOUT (terminal)
```

Stale-identity rule (#2) is checked at THREE points per turn: after
validate, after TTS, and inside `sink.publish` (defense in depth against
TOCTOU). `stalled_spins` is a liveness guard (cap `max_retries + 1`), never
a budget — only `Orchestrator.check_max_turns()` owns the turn budget.

## 4. Timeouts (hierarchy, `TimeoutConfig`, `orchestrator.py:268-272`)

| Level | Default | Expiry action |
|---|---|---|
| Turn (`turn_timeout_ms`) | 10_000 | agent-turn wait gives up → `AGENT_TIMEOUT` (per-turn wait itself is 30s in `_run_behavior`; the config level is the detector/classifier horizon) |
| Behavior (`behavior_timeout_ms`) | 60_000 | budget via `check_max_turns` → `BEHAVIOR_TIMEOUT` |
| Call (`call_timeout_ms`) | 600_000 | whole-run ceiling |
| Greeting (`greeting_timeout_s`) | scenario timeout | `first_speaker="agent"` wait → `AGENT_TIMEOUT` (phase `first_speaker_greeting`) |
| Agent silence gate (`AGENT_SILENCE_WAIT_S`) | 6.0s | bounded fall-through: publish anyway (a talkative agent must not wedge the caller) |
| Publish drain (`DEFAULT_DRAIN_TIMEOUT_S`, PCM-scaled) | 12.0s floor | `PublishDrainTimeout` → `TRANSPORT_ERROR` |
| Hold watchdog (`hold_timeout_s`, optional) | unset | `sim.hold_timeout` + `on_hold_timeout` (bridge hangup) |

## 5. Cancellation

- Candidate under validation superseded mid-generation → `is_stale` at the
  post-validate check → `stalled_spins`, next turn re-generates (no forward
  progress consumed).
- Running TTS superseded → post-TTS `is_stale` check → same stalled-spin path.
- Sink refused (stale re-check or mixer not ready, no raise) → same path.
- Barge-in: seeded interruption policy cuts in mid-answer with a fixed
  backchannel; the wait continues for the real turn text (policy cut-in
  failures are best-effort decoration, logged and continued, never terminal).
- Hold watchdog task is cancelled in `run()`'s `finally` (`driver.py:278-281`).

## 6. Failure states (1-1 with the 7 settled codes, §28.4)

| Code | Where raised | Meaning |
|---|---|---|
| `CALLER_BEHAVIOR_VIOLATION` | `driver.py:757-763` | retries exhausted non-VALID (validator rejected) |
| `LANGUAGE_GENERATION_ERROR` | `driver.py:726-733` (+ replay-divergence exhaustion) | backend transport failure |
| `VALIDATION_ERROR` | `driver.py:611-618` | `do` action unexpectedly bypasses AI+validator |
| `TTS_ERROR` | `driver.py:803-810` | validated text, broken audio |
| `TRANSPORT_ERROR` | stalled-spin caps, `PublishDrainTimeout` | no forward progress possible |
| `AGENT_TIMEOUT` | greeting wait, `WAITING_AGENT` `None` | agent silence, never a caller violation |
| `BEHAVIOR_TIMEOUT` | single canonical exit, `driver.py:906-912` | budget out, agent never satisfied |

Replay asserts both levels loudly: per-attempt `assert_verdict` (verdict
divergence → `ReplayMismatchError` immediately) and terminal
`assert_outcome` (outcome divergence → same). Agent silence maps to
`AGENT_TIMEOUT`, never to a caller violation — "agent slow ≠ caller violation".

## 7. Rust (`lksr`) mapping

The machine above is implemented in `crates/lks-core/src/caller_contract.rs`
(pure logic: `evaluate_behavior`, `OrchestratorState`, `TurnDetector` with
explicit clocks) and shared via `tests/fixtures/parity/*.json` step-list
fixtures replayed identically by `tests/test_parity_vectors.py` (Python) and
`#[cfg(test)] mod parity_tests` (Rust). The transcript-evidence half lives
in `crates/lks-core/src/observer.rs` (`on_transcript` state machine) with
its own `observer_transcript.json` fixture. The live `lks-livekit` bridges
(room IO, realtime sessions, TTS playout) are the transport/execution layer
around this machine, not part of it.
