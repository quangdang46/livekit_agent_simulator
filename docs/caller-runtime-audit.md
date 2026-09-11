# Caller runtime audit — is the contract path the only way to the agent?

Date: 2026-09-11. Baseline: `7c23667` (architecture locked) through `a178fd3`.
Every claim below is reproducible from the repo at that revision; each finding
names the commit that resolved it. Evidence only — this file changes nothing.

## Audit sweep: every event-vocabulary reader checked

The recovery-barge fix (`c24ddc9`) was one instance of a *class*: the contract
path emits a new event vocabulary that legacy readers never learned. So every
`sim.X` kind read by the evaluators (`asserts.py`, `metrics.py`) was traced to
a **live** emitter before closing the audit:

- `sim.agent.audio_onset` / `sim.caller.audio_source_start` — pulse emitters
  (`livekit/observer.py`, the bridge speaker/audio latches in both
  `callers/*.py`), running normally on the contract path. Not dead.
- `sim.caller_midcall` — from `inject_reground`, which needs `_live_session`
  (unreachable). Not reachable, but no reader fails a run on its absence.
- `silence.detected` — `web/markers.py`; no assert/metric verdict hinges on it.
- `sim.script.*` cue/dtmf/wait/hang_up — `ScriptRunner` only; `script.verify`
  reading them is explicitly skipped with `skipped: True` for contract
  scenarios (finding 2 below), so they gate nothing.
- `sim.hang_up` — emitted by `bridge.sim_hang_up()` (still called by the
  contract path through the hold watchdog). Live.
- `sim.end_call_token` — emitted only from a `run()`-internal branch of the
  bridge Realtime loop: dead on the contract path, but the only reader
  (`_eval_ended_by_outcome`) treats a missing token as "not sim", never as a
  hard failure, so it can only under-attribute, never falsely fail.
- The two readers that could **fail a run on their absence** — barge recovery
  and `ended_by` (finding 1 / finding 6 below) — are now vocabulary-shared.

No other vocabulary reader was found that can decide a verdict it cannot see.

## Question

After the caller-contract migration, three things needed proving that code
review alone had not established:

1. **A** — is the contract path the *only* caller execution path to the agent?
2. **B** — when a candidate is rejected, does it truly never reach
   TTS/PCM/mixer/LiveKit?
3. Is the legacy that remains merely present, or can it still decide
   WHAT/WHEN or publish?

## A. Reachability — one run path, verified by call-site search

| Probe | Result |
|---|---|
| `run_orchestrator.py:403` | hard-fails when `scenario.caller_actions` is empty ("the legacy caller path is removed") |
| `bridge.run()` (Realtime session loop) | **0 call sites** anywhere in `src/` |
| `ScriptRunner(...)` | **0 instantiations** in `src/` (exported, tested directly, never constructed at runtime) |
| `interrupt_rate` / `caller_nudge` in `run_orchestrator` | **0 references** |
| `CALLER_MODES` (`scenario.py:48`) | transport modes only (webrtc_sim / sip variants), not caller-logic modes |
| `_live_session` assignment | exactly one site (`callers/gemini.py:525`), inside `run()` |

The bridge survives only as plumbing: `publish_mic`, `publish_validated_pcm`,
`watch_agent_tracks*`, `drain_persona_speech`, `stop`.

**Legacy present but inert.** Not a caller-execution architecture any more.

## B. Audio-side invariant — rejected candidates never become audio

Chain, from code:

```
do: → generate (AI) → validate [bounded retry]
                        ├─ not VALID → _fail(CALLER_BEHAVIOR_VIOLATION) → RETURN
                        └─ VALID     → plan_speak → _speak(validated text)
                                       → sink.publish → publish_validated_pcm
                                       → mixer.push_speech → LiveKit
```

The guard is `driver.py:738`, before `candidate = retry.candidate` (747) and
before `_speak` (774).

Four TTS call sites; only one takes model output:

| Site | Text source | Validated |
|---|---|---|
| `say:` (402) | scenario-authored | n/a (`bypasses_ai_and_validator`) |
| `interrupt:` (548) | scenario-authored | n/a |
| `do:` (774) | **AI-generated** | gated on VALID |
| policy cut-in (1004) | frozen constant ("uh-huh"/"Mhm.") | n/a — never model output |

`_speak` retries the *same* text, bounded; it never re-invokes the adapter.
The `first_speaker` greeting path synthesizes nothing.

Proven by `tests/test_contract_live_wiring.py`, which drives the real
`ContractCallerDriver` + real `BridgePublishSink` and spies on the TTS
callable, with a positive control. Mutation-checked:

```
shipping code : drift synthesized = 0 | pushed = 0
gate removed  : drift synthesized = 1 | pushed = 1
```

## Findings

Five real defects were found, all fixed. None was an architecture change.

### 1. Recovery asserts counted zero barges on the contract path — `c24ddc9`

Barge sources were `sim.script.cue{barge_in}` and `interruption{by=sim}`,
emitted only by ScriptRunner / InterruptRateRunner / the Realtime pump — none
instantiated. The contract path emits `contract.barge` / `contract.interrupt`,
which no reader understood, so `barge_ms` was always empty and every
`type: recovery` outcome with the default `min_agent_finals_after_barge_in=1`
failed the run.

Proof (same run, two log vocabularies):

```
legacy-shaped log   → pass True,  agent_finals_after_barge_in = 2
contract-shaped log → pass False, agent_finals_after_barge_in = 0
```

Impact: 4 shipped templates failed falsely; `barge_count` / `recovery_rate`
were wrong in every report.

Fix: one shared predicate (`script/models.py::is_recovery_barge_event` /
`is_interruption_event`) covering both vocabularies, used by `asserts.py` and
`metrics.py` so they cannot drift apart. `contract.policy_interrupt` is
deliberately NOT a recovery barge (it is a backchannel). `contract.barge` now
logs the class the action always carried.

### 2. `script.verify` failed the CI gate for contract scenarios — `c24ddc9`

`evaluate_script_log` matches step_ids against `sim.script.cue` only, which
the contract path never emits → every step "not fired" → `pass: False` →
`suite.py:56` adds it to `hard_reasons` → **CI blocks**. 5 shipped templates.

Fix: skip explicitly (`skipped: True` + reason + inapplicable step ids) when
`caller_steps` drives the contract path, matching the existing `assert_verify`
`skipped` convention. A baseline test asserts a *real* `script.verify` failure
still blocks CI, so the skip is not masking signal.

### 3. `inject_cue` was a latent speech-layer escape hatch — `d3f17ba`

`BridgeAssetPlayer` fell back to `bridge.inject_cue(delivery="room_pcm")` when
the mixer had no `push_noise`. For a `voice.*` asset that calls `push_speech`
directly — onto the validated-speech channel, with no validator, no sink and
no staleness re-check. Guarded by an attribute check, not by construction.

Fix: the fallback is removed; the player refuses (`contract.audio_refused`),
which the driver maps to `TRANSPORT_ERROR`. Property now holds by
construction. Three pinning tests.

### 4. Inert caller-brain computation on every run — `cd42772`

`run_scenario_instance` built `CallerPolicyContext` → `midcall_cues()` and
`persona_system_prompt()`, both consumed only inside `run()`. Removed at the
call site after confirming which parameters are live: `voice_gain` (scales
validated speech in `publish_validated_pcm`), `audio_effects`
(`ParallelMicMixer(effects=...)`), `silent_mode`, `first_speaker`, `recorder`
were all kept.

**Surfaced product gap (not fixed, documented at the call site):** a saved
`lks optimize` artifact (`Scenario.caller_policy`) has no runtime effect on
the contract path. The contract AI adapter prompts from the BehaviorContract
(`caller_contract/text_backends.py` uses its own `_SYSTEM_PROMPT` + contract
context), not the persona prompt the optimizer tunes. Re-wiring that is a
design decision, not a cleanup.

### 5. `interaction.pace/hesitation/stumble` accepted but never applied — `fd3cc97`

`plan_speak` computes them; the driver uses only `pre_delay_ms`. They cannot
be applied on the contract path: it synthesizes the exact validated string,
so inserting a token after validation would put unvalidated words on the wire
and break finding B's property.

Fix: new authoring warning `interaction_shaping_not_applied`. Chosen over a
parse-time reject (breaking DSL change for a capability that may yet land as
planner-level timing) and over silence (an author setting `hesitation: low`
would reasonably expect shaping and blame the agent under test).

### 6. `type: ended_by` reported "detect" for every contract run — `a178fd3`

Same class again: `run.end_condition.reason` is written by whichever engine
ran — legacy wrote `sim_end_call` / `agent_disconnected`, the contract path
writes `contract_scenario_end` / `contract_caller_end` / `contract_agent_end`
(`live_wiring.py`'s `EndedBy` map). The reader knew only the legacy spelling,
so any `type: ended_by` naming a side failed. Proof: contract log → pass
False (actual=detect); legacy-shaped log → pass True. 2 shipped templates
set `ended_by: sim`.

Fix: one shared mapping (`script/models.py::end_side_from_reason`) over both
vocabularies, mirroring the barge-predicate placement. `contract_timeout`
joins the no-side branch (like `max_turns`); unrecognized reasons read
explicitly instead of passing silently.

### 7. `interruption_count` was 0 for every contract run — `a178fd3`

`metrics.py` counted only the legacy `interruption` kind. It now uses the same
`is_interruption_event` predicate as `asserts.py`, so the report and the
assert can never disagree on how many cut-ins occurred. The legacy line is
preserved: a seeded backchannel cut-in (`contract.policy_interrupt`) IS an
interruption but is NOT a recovery barge (correction/escalate only).

## Residual, deliberately not actioned

- **`silent_mode` compat bridge.** Not closable by migration: the silent
  scenario is wait/end-only, so its persona flag is a no-op at the action
  level, and dropping it without a scenario-level equivalent would silently
  change behaviour the moment either silent template gains a speak step.
  Closing it needs a new DSL surface — deferred until a template or run
  demonstrates the need.
- **`lks optimize` ↔ contract path** (finding 4) — a design decision.
- **Full Rust runtime.** `caller_contract.rs` holds the parity logic; the
  Rust side has no contract driver/publish path. Separate workstream.

## Reproducing

```bash
uv run pytest tests/test_contract_e2e_hard_boundary.py -q   # no unvalidated utterance reaches the agent
uv run pytest tests/test_contract_live_wiring.py -q         # INVALID → no TTS/PCM/mixer + play_audio layer pins
uv run pytest tests/test_asserts.py tests/test_suite.py -q  # barge/ended_by vocabulary + CI gate
uv run pytest tests/test_metrics.py -q                      # interruption/barge lines per vocabulary
uv run pytest tests/test_authoring.py -q                    # shaping warning
uv run pytest -q                                            # 1158 passed, 2 skipped, 2 xfailed
```

Note: `f22991b` was the pre-amend SHA of finding 4; `cd42772` is canonical.
