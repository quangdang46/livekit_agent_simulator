# PLAN-20260910 — Real Semantic Verifier (tier-2/3 behind SemanticVerifierProtocol)

Date: 2026-09-10
Status: planned (not started)
Parent: single-path caller architecture (`aff0939` + `02553c3` on `develop`)
Scope: `caller_contract/semantic.py` replacement backend + golden cases + interface spec.
Explicitly OUT of scope: driver, orchestrator, publish sink, legacy caller,
BehaviorEvaluator, any change to the publish-gate flow.

## 1. Background — what is already true on `develop`

The single-path caller is shipped and live-verified:

```text
Scenario → Behavior → Orchestrator → AI Language Adapter → Validator
→ Interaction Planner → PublishSink + drain → LiveKit
```

Enforcement invariants already hold:

- No unvalidated utterance reaches LiveKit (validator is the gate).
- `max_turns` has one owner: `Orchestrator.check_max_turns()` (`02553c3`).
- Stale/TOCTOU protection, publish drain, failure taxonomy
  (`CALLER_BEHAVIOR_VIOLATION` vs `AGENT_TIMEOUT` vs `TRANSPORT_ERROR`
  vs `BEHAVIOR_TIMEOUT` vs `LANGUAGE_GENERATION_ERROR`) are distinct.
- `RuleBasedSemanticVerifier` is the default backend, and it is HONEST
  about its limits (`02553c3`):
  - keyword evidence for **act** only;
  - `observed.target` is ALWAYS `None` (never echoes `contract.target`);
  - `validator.py` already contains the `SEMANTIC_TARGET_MISMATCH` branch,
    currently a no-op for this tier by design.

What is NOT true yet (this plan's gap):

- The baseline cannot verify **target** (price vs delivery-date) and cannot
  generalize to open-ended phrasing (declarative follow-up answers score
  zero hits → `LOW_CONFIDENCE` false reject).
- No further keyword growth — chasing cases with regex is explicitly
  rejected (diminishing returns, growing false-negative surface).

## 2. Goal — close the semantic contract for real

### 2.1 `ObservedAct` schema (already stable, no change needed)

```python
ObservedAct(
    act: str,            # primary act derived from utterance evidence
    target: str | None,  # derived from utterance evidence, None = unknown
    slots: dict,         # derived from utterance evidence
    confidence: float,   # [0.0, 1.0]; < 0.5 ALWAYS rejects (validator gate)
    all_acts: list[str], # every other detected act (nested/forbidden intents)
)
```

Producer contract (already enforced by `SemanticVerifierProtocol`):

- `classify(utterance, contract) -> ObservedAct` — utterance text ALONE,
  never the generator's claimed `act/target/slots`.
- `target=None` means "not verified", never "verified as contract target".
- Low confidence is a reject signal, never a guess.

### 2.2 What a real backend MUST do that the baseline cannot

| Capability | Baseline (tier-1) | Real backend (tier-2/3) |
|---|---|---|
| act from paraphrase | keyword hits only | generalize to unseen phrasing |
| target from utterance | always `None` | populate from evidence (span/NLI) |
| declarative follow-ups ("I'm looking for details on…") | 0 hits → reject | classify as `ask` confidently |
| nested forbidden intent | lexical tags only | semantic detection |
| confidence calibration | hit-count buckets | calibrated scores |

## 3. Design — interface first, implementation second

### 3.1 Golden cases BEFORE implementation

Write `tests/fixtures/semantic_verifier/golden_cases.json` (or
`tests/test_semantic_golden.py`) FIRST, covering at minimum:

```text
ACT
├── negotiate/price paraphrases (must PASS, high confidence)
├── cross-act near-misses (negotiate vs ask — must distinguish)
├── multi-act sentences (primary + secondary in all_acts)
└── ambiguous filler ("interesting thought about the weather")
    → LOW_CONFIDENCE reject

TARGET (the current gap)
├── negotiate/price utterance vs negotiate/price contract → target=price
├── negotiate/delivery-date utterance vs negotiate/price contract
│   → target=delivery_date → SEMANTIC_TARGET_MISMATCH
└── target-ambiguous utterance → target=None → deterministic claim
    check (step 3) still applies, semantic layer stays silent

FORBIDDEN (nested)
├── "…although I could consider financing" inside valid negotiate
│   → all_acts contains financing → FORBIDDEN_INTENT_DETECTED
└── paraphrased forbidden intent (no lexicon keyword)
    → must STILL be caught (this is what tier-1 cannot do)

CONFIDENCE
├── every golden PASS has confidence >= 0.5 with margin
└── every golden REJECT is reject for the RIGHT reason
    (ACT_MISMATCH vs TARGET_MISMATCH vs LOW_CONFIDENCE vs FORBIDDEN)
```

Both current AND future backends run the SAME golden file — the file is
the contract, not any one implementation.

### 3.2 Backend options (evaluate in order, stop at first that passes goldens)

```text
tier-2a: local NLI (cross-encoder, e.g. MiniLM)
    + zero new API cost, deterministic, offline-friendly
    + real semantic generalization for act
    - target/slots still need a span-extraction layer on top
    - model download + platform matrix (same class of problem as
      sherpa-onnx TTS, already solved there — reuse the pattern)

tier-2b: embedding similarity against act exemplars
    + simplest to build
    - weakest calibration story; threshold tuning is fragile
    - only if 2a proves too heavy

tier-3: LLM judge (text-only, same pattern as text_backends.py)
    + best generalization, reuses existing HTTP plumbing
    + can populate act + target + slots + confidence in one call
    - cost/latency per turn; needs bounded retry + ERROR mapping
      (already exists: VERIFIER_UNAVAILABLE → ERROR, never VALID)
    - prompt must be evidence-constrained (quote the utterance span
      supporting act/target, else confidence capped)
```

Recommendation: prototype 2a and 3 against the golden file, keep the
winner behind the protocol. Do NOT ship both as competing defaults —
one default backend, the other (if built) stays an opt-in.

### 3.3 Swap mechanics (already zero-cost by design)

```python
# before (today)
ContractValidator(semantic_verifier=RuleBasedSemanticVerifier())

# after (this phase)
ContractValidator(semantic_verifier=<RealBackend>(...))
```

- `validator.py` unchanged (the `SEMANTIC_TARGET_MISMATCH` branch
  activates automatically once `observed.target` is populated).
- `driver.py`, `orchestrator.py`, `publish_sink.py`, `live_wiring.py`
  unchanged — none of them names a concrete verifier class.
- `RuleBasedSemanticVerifier` stays as the offline/dev fallback and the
  `semantic_verifier=None` default (unit-test isolation), never deleted.

## 4. Acceptance

```text
golden file exists and BOTH backends run it:
├── RuleBasedSemanticVerifier: documents its known failures
│   (target cases + declarative follow-ups → expected FAIL entries,
│   explicitly marked baseline-limitation, not regressions)
└── RealBackend: ALL golden cases pass, each for the right reason

no caller-path file touched:
├── git diff --stat shows ONLY semantic*.py + tests + this plan
├── driver/orchestrator/validator/publish_sink/live_wiring untouched
└── full suite green (979+), no golden-case special-casing
    (no `if utterance == "..."` in the backend — spot-check by review)

live smoke (same harness as single-path verification):
├── caller_steps scenario with a target-bearing do:
│   (negotiate/price) against the real agent
└── on-topic price talk publishes; off-topic date/financing talk
    → SEMANTIC_TARGET_MISMATCH or FORBIDDEN → never published
```

## 5. Explicitly NOT in this phase

- BehaviorEvaluator upgrades (separate question: "did the AGENT satisfy
  the behavior" — separate phase, separate golden file).
- Real-agent pacing / turn-budget tuning (already correct per `02553c3`).
- Legacy caller removal (single path already bypasses it; deletion is
  cleanup, not semantics).
- Any new keyword/pattern in `ACT_PATTERNS` (frozen as of this plan).
- Prompt-engineering the LanguageAdapter to "speak more verifiably"
  (that would move the boundary into the generator — wrong direction).

## 6. Suggested slice order

```text
1. golden_cases.json + runner (fail-against-baseline documented)
2. backend prototype(s) against goldens (2a and/or 3)
3. wire winner as default in live_wiring.py (+ fallback flag)
4. live smoke with target-bearing scenario
5. review → commit → push (same bar as aff0939/02553c3)
```
