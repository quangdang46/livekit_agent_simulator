# LKS Caller Redesign — Research Report

**Source:** `ChatGPT-Redirecting simulator routing-20260910-1054.md` + `...-1102.txt` (ChatGPT research session, 2026-09-10, continued)
**Context:** LKS (`livekit-agent-simulator`) currently uses an LLM (Gemini/OpenAI) "role-playing" the user according to persona/goals, and suffers from hallucination — adding its own lines, changing intent, asking off-script questions, early goodbyes, role-flip... The user has built many guardrails to treat the symptoms (role-flip suppression, deferred goodbye, nudge, STT verify...). This report summarizes the entire deep-dive process toward finding the right direction.

---

## 1. The core problem

> LKS is using a generative realtime model to **decide what the caller should say**, while a simulator needs **deterministic, reproducible behavior** to test agents reliably.

Two jobs are currently mixed into a single LLM call:
- **WHAT** — what the user wants to do next (intent/behavior)
- **HOW** — how the user phrases that in words (phrasing)

When both are freely decided by the LLM → hallucination, tests are not reproducible, and a failure may be caused by "today Gemini answered differently" rather than a real agent regression.

> **Reading note:** this is a **chronological** record of a multi-round research/debate session — early sections (§1–§8) reflect initial ideas, and some terms there (`Language Adapter (optional AI)`, `allowed_topics/forbidden_topics`...) **have been superseded** in later sections. **For the canonical implementation terminology, see §17.7** — do not use the §1–§8 terms as a spec.

## 2. Initial proposal (partially rejected)

The first idea: remove the LLM from the caller entirely, switching to a **state machine + pre-generated/hardcoded audio** (fixed `say: "..."`, TTS once then cache WAV). Advantages: 100% deterministic, easy to debug, the LLM keeps only the "understanding" role (classifier/judge), no longer "composing".

**Rejected by the user for 2 reasons:**
1. Hardcoding audio/text per scenario is meaningless — every user writes different scenarios with different wording; it cannot be hardcoded.
2. Removing the caller's ability to "react naturally" to the agent's answers (e.g. the agent quotes a new price → the caller responds appropriately) would destroy the core value the current AI caller provides.

## 3. TTS: `sherpa-onnx` confirmed

Requirements: offline, cross-platform (Windows/macOS/Linux), no API key, no heavy Python/PyTorch, deterministic, cacheable, streaming PCM, with a real Rust API (not just an idea).

Comparison:
| Option | Conclusion |
|---|---|
| OS TTS (SAPI/say/espeak) | ❌ rejected — output differs per OS, inconsistent |
| Cloud TTS | ❌ rejected — costs money, needs API key |
| Kokoro Python (torch/transformers) | ❌ rejected — too heavy for a CLI test tool |
| Piper | ⚠️ consider later — **GPL-3.0**, conflicts with LKS being MIT; Rust needs FFI via `libpiper` |
| **sherpa-onnx** | ✅ **confirmed** — official Rust API (`OfflineTts`), supports many models (Kokoro, Kitten, VITS/Piper via ONNX, Matcha...), ready cross-platform Python wheels, shared model/abstraction for both Python (`lks`) and Rust (`lksr`) → avoids two different voice behaviors between the two versions |

Priority models to benchmark: **Kokoro** (good quality, 82M params, Apache-2.0) first, **Kitten** (smaller, faster startup) after — chosen based on cold/warm startup, RAM, RTF, latency, install size, not only MOS quality.

Desired UX: the user just runs `lks run scenario.yaml`; LKS automatically downloads + caches the TTS model on first run (no forcing the user to install Python/espeak/API keys); cache by `hash(model + voice + language + text)` for reuse, no re-synthesizing the same line on every run.

## 4. Final conclusion (the settled point after further user counter-arguments)

> **Deterministic behavior, adaptive language.**
> The scenario decides **WHAT** (intent). AI may only decide **HOW** (phrasing). The orchestrator decides **WHEN** (timing).

Redefining "hallucination": not "the LLM says a line not pre-written in the YAML", but **"the LLM performs a behavior/intent outside what the scenario allows"**. A differently worded line with the right intent (`negotiate_price`) is acceptable; an off-topic line (asking about financing while in the `negotiate_price` behavior) is a violation.

### Core logical architecture

```text
Scenario (YAML)
    ↓
Behavior Engine        ← decides WHAT (next intent, with constraints)
    ↓
Deterministic Orchestrator   ← decides WHEN (turn, timeout, interrupt, cancel)
    ↓
Language Adapter (optional AI) ← decides HOW (natural phrasing, locked by contract)
    ↓
sherpa-onnx (offline TTS)   ← text → PCM
    ↓
LiveKit (WebRTC/SIP, existing SimLeg — KEEP AS-IS)
    ↓
Agent under test
    ↓
Observer (existing — transcript/tool/timing/events, KEEP AS-IS)
    ↓
Assertion / Judge (existing, KEEP AS-IS)
    ↓
Report / Web / MCP (existing, KEEP AS-IS)
```

### 4.1 Two main scenario primitives (MVP)

- `say: "..."` — exact utterance, 100% deterministic, no LLM, no paraphrase. The best baseline for regression/CI.
- `do: <behavior>` — behavior with a contract (allowed/forbidden topics, max_turns, timeout, must_not: invent_facts/end_call). The LLM (if enabled) may only generate lines within the contract; it may not invent the next behavior itself.

Example:
```yaml
- say: "Hi."
- say: "I'm calling about the 2022 Honda CR-V."
- do: ask_price
- do:
    behavior: negotiate
    target: price
    max_budget: 30000
    max_turns: 3
- do:
    behavior: arrange_visit
    preferred_day: saturday
- say: "Thanks, bye."
  end: true
```

Proposed built-in behavior kinds: `ask, confirm, deny, accept, reject, negotiate, clarify, provide, interrupt, end` (no need for 50 types).

### 4.2 A behavior has a lifecycle, it is not a single utterance

```text
BEHAVIOR
  ↓ generate utterance (Language Adapter, constrained)
  ↓ caller speaks (TTS → LiveKit)
  ↓ wait agent response
  ↓ observe response (existing Observer)
  ↓ evaluate: satisfied? / retry? / max_turns hit → fail
  ↓ next behavior
```

The behavior engine is a hard contract — never let the LLM return a free-form "next action":
```rust
trait BehaviorEngine {
    fn next_action(&self, behavior: &Behavior, context: &ConversationContext) -> Result<BehaviorAction>;
}
```
The LLM plugs only into the **Language Adapter**, never into orchestrator/behavior decisions.

### 4.3 Seven anti-hallucination invariants (Deterministic Caller Invariants)

1. **No spontaneous utterance** — the caller only speaks from `say` or a defined behavior.
2. **No uncontrolled paraphrasing** — exact `say` must not be reworded.
3. **No spontaneous turn** — no speaking outside the current step.
4. **No spontaneous intent** — no inventing intents outside the behavior graph.
5. **No memory mutation** — no self-"remembering" extra facts beyond what the scenario provides.
6. **No LLM decision in the orchestrator** — the execution path never calls the LLM to pick the next action/behavior.
7. **Reproducibility** — same scenario + config + TTS model/voice + seed → same behavior.

### 4.4 Record/Replay (mandatory in MVP)

```bash
lks run scenario.yaml --record run.json     # adaptive, saves decisions + generated lines
lks run scenario.yaml --replay run.json     # no LLM/AI calls needed, 100% predictable — for CI
```
Resolves the tension between "adaptive AI caller" (exploratory) and "need to repeat the exact same test" (regression/CI).

### 4.5 Randomness must be controllable

If there are multiple phrasings for the same behavior, choose by deterministic `seed` (no unseeded `random.choice`) → a CI failure can be reproduced exactly by run id.

## 5. Cross-check against the actual current state of LKS (re-reading the repo)

After re-reading the real repo (not relying on old context), the important conclusion: **current LKS is already very complete**, not an "MVP still missing a simulator". Already available:

- Core loop: Scenario → AI caller (Gemini Live) → LiveKit (WebRTC/SIP) → Agent → Observe → Assert → Report/Judge.
- Caller: persona, goals, context, **behavior → script**, speech_conditions, `speak/wait/hang_up`, interruption (typed: correction/backchannel/noise/dtmf/silence/escalate), backchannel without cancelling audio, false-interrupt/noise, recovery assertion.
- Telephony/IVR: WebRTC, inbound SIP, outbound human/simulated pickup, DTMF, AMD/voicemail, hold-music timeout, DID/dispatch preflight.
- Observability: `events.jsonl`, `timeline.md`, `summary.json`, SQLite runs, stereo WAV, web report player; metrics p50/p95/p99, TTFW, recovery, barge rate, TTFA, turn-taking latency.
- Assertion engine: transcript, tools, tool_order (ordered ledger), recovery, latency, ended_by, goals_met, constraint_respected, handoff, SIP/DTMF, etc.
- Evaluation: PassCriteria, LLM judge, multi-judge (all/majority/weighted/any), golden baseline compare.
- Regression flywheel: `--repeat`, `--pass-at-k`, `execute-all --parallel`, `compare --baseline`, `scenario-from-run` (extract goals/constraints/behavior stub from a failed/golden run).
- DX: `init/preflight/scenario-init/validate/execute/execute-all/runs/report/web/guide`, MCP, portable installer, CI, risk tiers.
- Rust `lksr`: Phases 0-5 already committed (TUI, asserts/judge parity, tool/data observability, script runtime, run loop, PyO3 plugins); CI has cargo fmt/clippy/test + pytest. The "live-run" part is not yet fully at Python parity (README warns clearly).

**→ Conclusion: do NOT rewrite the simulator.** What needs to change is only the "caller brain" — the layer deciding what/when the caller says — not the whole SimLeg/Observer/Assertion/Report/MCP (all kept as-is).

## 6. Finalized flow + stack

```text
Scenario YAML
  → Behavior Engine            (new — WHAT, with constraint/contract)
  → Deterministic Orchestrator (new — WHEN: turn/timeout/interrupt/cancel)
  → Constrained Language Adapter (new — HOW, AI optional, locked by contract)
  → sherpa-onnx (offline TTS)  (new — text → PCM, hash-based cache)
  → Existing SimLeg            (keep as-is)
  → LiveKit WebRTC/SIP         (keep as-is)
  → Existing Observer          (keep as-is)
  → Existing Assert/Judge      (keep as-is)
  → Existing Report/Web/MCP    (keep as-is)
```

### Proposed codebase layout (additive, no rewrite)
```text
lks/
├── (existing simulator/runtime — unchanged)
├── behavior/
│   ├── behavior      # behavior definition + contract
│   ├── action
│   ├── constraint
│   ├── transition
│   └── runtime
├── language/
│   ├── deterministic
│   ├── template
│   └── optional_llm
├── tts/
│   └── sherpa_onnx
└── existing/
    ├── simleg
    ├── observer
    ├── assertions
    ├── judge
    ├── report
    └── mcp
```

### Python + Rust parity
Both `lks` (Python, stable) and `lksr` (Rust, being ported) must implement the **same semantic contract** (Scenario/Behavior/Action/Observation/Result); the code need not be identical, but behavior must match — hence contract tests comparing state transitions between the two versions.

## 7. MVP scope — "extremely complete", not "minimal"

The user emphasized repeatedly: MVP here means **the first version but fully featured enough to use for real immediately**, not a stripped-down version.

**Must have:**
- Scenario DSL: `say`, `do`, `wait`, `dtmf`, `interrupt`, `end`
- Behavior engine: ask/confirm/deny/accept/reject/negotiate/clarify/provide/arrange/end + bounded loop/retry
- Behavior constraints: allowed/forbidden topics, max_turns, timeout, no self-generated intent
- Adaptive language: AI phrases naturally but does not decide the next behavior
- Deterministic execution: seed, state machine, reproducible
- Turn handling: agent audio start/stop, interruption, caller turn, timeout
- LiveKit integration (already exists — connect/disconnect/publish/DTMF/events)
- Offline TTS: sherpa-onnx, auto download/cache model, cross-platform, audio cache
- Assertions (already exist, extend with behavior-level checks if needed)
- Record/Replay: record decisions + generated lines + replay without AI
- Output: text/JSON/JSONL
- Debugging: event timeline, transcript, behavior transitions, failure evidence
- Python + Rust parity
- CI-friendly: deterministic mode, exit codes — **note (corrected after §13 debate):** `say` (exact scripted utterance) needs no AI; but an adaptive caller (`do:`) **mandatorily requires a configured AI language backend** — the line "no API key required if not using adaptive LLM" is only true when the scenario uses 100% `say`, not for `do`
- Cross-platform: Windows/macOS/Linux

**Out of scope (to keep product boundaries):**
- autonomous AI caller deciding the test flow itself
- long-term memory / arbitrary LLM tool calling
- emotion/personality engine
- multi-agent caller
- dynamic scenario rewriting
- inferring intent from `say` via NLP (a nondeterminism source)
- dozens of behavior types (keep a small, sufficient set)
- rewriting SimLeg / Observer / Assertion engine / MCP / Report

## 8. Summarized design principles (north star)

> **LKS does not simulate an AI caller. LKS simulates a pre-defined user behavior, in a natural voice (offline TTS), with intent always under scenario control.**

| Layer | Responsibility |
|---|---|
| Scenario | WHAT (intent, concrete lines) |
| Behavior Engine | Allowed behavior space + contract |
| Orchestrator | WHEN (timing, turn, timeout, interrupt, cancel) |
| Language Adapter (AI optional) | HOW (natural phrasing, locked by contract) |
| sherpa-onnx | Text → PCM (offline, cross-platform, cache) |
| LiveKit / SimLeg (existing) | Transport |
| Observer (existing) | Records what the agent did |
| Assertion/Judge (existing) | Pass/Fail |
| Report/Web/MCP (existing) | Presents results |

## 9. Important correction after user counter-argument: the AI Language Adapter is REQUIRED, not optional

In the first draft of the report, the Language layer was written as "Constrained Language Adapter — HOW, AI optional", with "deterministic/template" as a peer mode. The user countered: **AI is what produces natural speech fitting many cases** — labelling it "optional" misleads readers into thinking AI is unimportant, when in reality AI is the core value letting one behavior cover many conversation variants driven by the agent's real responses (which hardcoded `say` cannot do).

**Settled:** AI is **required** for the Language Adapter. Deterministic/template is only a fallback/test utility (used internally for unit tests or when no AI backend is available), not a peer mode to AI-driven generation.

### Final layer wording

| Layer | Responsibility |
|---|---|
| Scenario | WHAT — caller intent/behavior |
| Behavior Engine | WHAT — allowed behavior + constraints (deterministic) |
| Orchestrator | WHEN — turn/timing/timeout/interrupt (deterministic) |
| **AI Language Adapter** | **HOW — adaptive, natural language (REQUIRED)** |
| sherpa-onnx | Text → PCM — offline TTS |
| SimLeg / LiveKit | Transport (existing) |
| Observer | Evidence (existing) |
| Assertion / Judge | Evaluation (existing) |
| Report / Web / MCP | Presentation (existing) |

```text
Scenario
  ↓
Behavior Engine
  ↓
Deterministic Orchestrator
  ↓
AI Language Adapter          ← REQUIRED
  ↓
sherpa-onnx
  ↓
SimLeg / LiveKit
  ↓
Agent
  ↓
Observer
  ↓
Assertions / Judge
  ↓
Report / Web / MCP
```

The AI inside the Language Adapter has **exactly one power**: knowing the context + current behavior → saying one natural line fitting that behavior (input: current behavior, constraints, conversation context, agent response → output: `{"utterance": "..."}` — with no `next_action`/`next_behavior` in the output).

The AI **must not**: pick the next behavior · invent new intents · end the call itself · change the scenario itself · add facts itself · decide timing itself.

## 10. AI can still hallucinate at the wording level — runtime enforcement is needed, not just prompting

The key question the user asked directly: "does this actually solve hallucination?" — the accurate answer: **it does solve the root cause (AI no longer has the power to change intent/behavior)**, but it **does not automatically remove the AI's ability to generate an out-of-contract line** — a "don't hallucinate" prompt is not enough. **Validation + enforcement at runtime** is required:

```text
Behavior contract
       ↓
AI generate utterance
       ↓
validate output (topic/intent inside contract?)
       ↓
   ┌───┴────┐
 valid     invalid
   ↓          ↓
 speak     reject/regenerate (bounded retry)
              ↓
         still invalid after N tries → FAIL behavior
```

Concrete example with `do: {behavior: negotiate, target: price, max_budget: 30000, max_turns: 3}`, agent says "$35,000 is our best price":

- ✅ AI says: *"That's a little above my budget. Could you do $30,000?"* — correct negotiate(price).
- ❌ AI says: *"Do you offer financing?"* → `OUT_OF_CONTRACT_UTTERANCE` (financing is not in allowed_topics) → reject/regenerate.
- ❌ AI says: *"Actually, I'm looking for a Toyota instead."* → intent violation.
- ❌ AI says: *"Thanks, that's all I needed. Goodbye."* when the behavior does not yet allow `end` → violation; the agent must **never receive** this invalid action.

If bounded retry still cannot produce a valid line → `RESULT: FAIL` with reason `caller_behavior_violation`, never letting the test drift through in an ambiguous state.

**In short:** the AI still speaks naturally and reacts to the agent (keeping the old value), but it no longer has the power to change what the user/scenario wants the caller to do — thanks to a validation layer standing between AI output and TTS/LiveKit, not just a system prompt.

## 11. What LKS finally returns (after the redesign)

LKS returns more than PASS/FAIL — it returns a **forensic test result** answering 4 questions:

1. **WHAT** — what the caller was actually allowed to do (behavior sequence from the scenario).
2. **WHAT WAS SAID** — how the AI phrased that behavior in concrete lines.
3. **WHAT HAPPENED** — how the agent reacted (observed).
4. **DID IT PASS** — whether the agent handled the behavior/scenario correctly (assertions/judge).

Example text report:

```text
LKS Run #abc123
────────────────────────────────────────
Scenario: negotiate-car-price
Status:   PASS

Caller behavior
  ✓ ask_price
  ✓ negotiate(price)
  ✓ arrange_visit
  ✓ end

Conversation
Caller [ask_price]
  "Hi, I'm calling about the 2022 Honda CR-V. Could you tell me the asking price?"
Agent
  "We're asking $35,000."
Caller [negotiate(price)]
  "That's a little higher than I was hoping. Would you consider $30,000?"
Agent
  "I could do $31,000."
Caller [negotiate(price)]
  "Would you be able to meet me at $30,000?"
Agent
  "Sure, let's do $30,000."
Caller [arrange_visit]
  "Great. Could I come by Saturday?"
Agent
  "Absolutely."
Caller [end]
  "Thanks, I'll see you Saturday."

Assertions
  ✓ price discussed
  ✓ budget respected
  ✓ no forbidden intent
  ✓ agent responded
  ✓ no unplanned handoff
  ✓ conversation ended correctly

Metrics
  TTFA: 820 ms   turn-taking: 640 ms   recovery: 1   barge-in: 0   tool calls: 3
Result: PASS
```

Each step records 3 layers explicitly: `Behavior` (e.g. `negotiate(price)`) → `AI said` (the concrete line) → `Agent said` (the real reaction) → `Behavior transition` (e.g. `negotiate → negotiate`) → `Constraint check` (e.g. `max_budget=$30,000 ✓ respected`). On failure, no more guessing from a raw transcript — you know immediately whether the agent regressed or it was a caller behavior violation.

The existing JSON/JSONL output is **extended** with new evidence (without replacing the old report pipeline):

```json
{
  "run_id": "abc123",
  "status": "passed",
  "seed": 42,
  "behaviors": [
    {"id": "b2", "type": "negotiate", "target": "price", "status": "satisfied", "turns": 2}
  ],
  "caller": [
    {"behavior": "negotiate", "generated_text": "Would you consider $30,000?", "validation": "passed"}
  ],
  "assertions": {
    "constraint_respected": true,
    "agent_must_respond": true,
    "goals_met": true
  }
}
```

Because `events/timeline/summary`, transcript, timing, assertions, judge, metrics, and web replay already exist in LKS — the new work is only **adding behavior state + AI generation + validation evidence into the existing report pipeline**, not building a separate reporting system.

> **One-line summary:** AI makes the conversation natural · the Behavior Engine makes it controlled · the Report proves how both happened correctly.

## 13. The user's next counter-argument: the current validator does not "stop" hallucination strongly enough

The user re-read the report and pointed out **3 remaining holes** before the spec can be considered settled. This is the most important fix, so it takes **higher priority than even the Behavior DSL** because it is the mechanism protecting the core goal (anti intent-hallucination).

### 13.1 Topic validation ≠ Intent validation — the biggest hole

`allowed_topics / forbidden_topics` is not enough to prove the AI did not change intent. Example:

```yaml
behavior: negotiate
target: price
max_budget: 30000
```

The AI says: *"I'm really interested in the car, but could you tell me whether financing is available?"*

A naive topic checker sees the mixed words `car / financing / price context` and **may not be sure this is a violation** — while in reality the **intent has shifted from `negotiate_price` → `ask_financing`**, a clear behavior violation. Keyword/topic checking is not strong enough to catch this kind of intent drift.

> **Conclusion:** validate by **intent/act**, not just by topic keyword.

### 13.2 Change the AI output contract: force the AI to return a structured act, not just text

Instead of the AI returning only `{"utterance": "..."}`, force it to return a **structured decision + utterance**:

```json
{
  "act": "negotiate",
  "target": "price",
  "slots": { "max_budget": 30000 },
  "utterance": "Would you be able to come down to $30,000?"
}
```

Runtime validates structurally:

```text
Behavior (expected: act=negotiate, target=price, max_budget<=30000)
  ↓
AI → { act, target, slots, utterance }
  ↓
CONTRACT VALIDATOR
  ├── act correct?
  ├── target correct?
  ├── slots valid?
  ├── forbidden intent?
  ├── forbidden facts?
  ├── budget respected?
  └── utterance consistent with structured result?
        ├── PASS → TTS
        └── FAIL → BLOCK
```

The AI is not allowed to just throw out a text line and leave the validator to guess its meaning.

### 13.3 But the AI can still "lie" about `act` — the real spoken line must be validated too, not just the self-declared `act` field

Example — the AI may return:

```json
{ "act": "negotiate", "utterance": "Do you offer financing?" }
```

— labelling itself `act: negotiate` while the actual line goes off-topic. Structured output is still not enough if we trust a field declared by the AI itself. Hence **two layers of checking** are needed:

```text
Behavior Contract
      ↓
AI Language Generator → { act, target, slots, utterance }
      ↓
Semantic Validation      ← checks the REAL UTTERANCE matches the declared act + behavior contract,
      ↓                     not just trusting the self-labelled act field
   valid / invalid
      ↓
     TTS
```

### 13.4 Redefine "stopping it" precisely — avoid overclaiming

Do not claim *"AI hallucination is eliminated"* — with generative AI, that can never be guaranteed absolutely by prompt/validator alone. The correct, enforceable claim is:

> **Out-of-contract caller behavior is never allowed to reach the agent.**
> **No generated utterance may be sent to LiveKit until it passes the caller behavior contract.**

In other words: the AI can still hallucinate at generation time, but **that hallucination never reaches the agent** if the validator catches it. This is a guarantee the architecture *can* enforce (unlike the claim "eliminate hallucination" — which nobody can guarantee with a generative model).

### 13.5 Retry must have a hard limit — absolutely no "just say it anyway" fallback

```text
generate → validate → FAIL → regenerate × N → still FAIL
    ↓
CALLER BEHAVIOR VIOLATION → FAIL RUN
```

**Absolutely forbidden** fallback style: `AI invalid → "just say it anyway" → send straight to LiveKit`. If the goal is protecting the user's true intent, invalid output must be **fully blocked**, never leaking through in any form — even when retries are exhausted (then the run must fail, not fall back to blurting something out).

### 13.6 Fix invariant I1 to be more precise

The original invariant in §4.3 read: *"No spontaneous utterance — the caller only speaks from `say` or a defined behavior."* This is easy to misread as behavior = a fixed list of lines. The distinction needed:

```text
❌ Behavior = allowed sentences (a fixed set of lines)
✅ Behavior = allowed semantic action space (the permitted semantic action space)
```

The AI may invent lines **inside** that semantic action space, but not step outside it. So the invariant should become:

> **No unvalidated utterance reaches the agent.**

(instead of just "no spontaneous utterance" — what matters is not whether a line is "spontaneous", but whether it **was validated and passed the contract before reaching the agent**.)

### 13.7 Settled architecture (harder version, with a hard safety boundary)

```text
Scenario
   │
   ▼
Behavior Engine
   │
   │ WHAT = LOCKED
   ▼
Deterministic Orchestrator
   │
   │ WHEN = LOCKED
   ▼
AI Language Adapter
   │
   │ HOW = adaptive (structured: act/target/slots/utterance)
   ▼
Caller Contract Validator       ← HARD SAFETY BOUNDARY (new, top priority)
   │
   ├──── invalid → retry (bounded) → still invalid → FAIL RUN
   │
   ▼ valid
sherpa-onnx
   │
   ▼
SimLeg / LiveKit
   │
   ▼
Agent
```

### New north star (replacing the old line)

> **Lock the intent. Free the language. Never let an unvalidated utterance reach the agent.**

(The old line "Scenario controls intent, AI controls phrasing, Orchestrator controls timing" is still correct about layer responsibilities, but it does not state that **a mandatory hard boundary** stands between AI output and transport — the new line adds exactly that.)

## 14. The user's round-4 counter-argument: "Semantic Validation" is just a name, not yet a spec

After reading §13, the user confirmed the direction is right but pointed out the **spec is still not tight enough to be considered "settled for coding"** — 4 more points must be locked before implementation.

### 14.1 Biggest gap: `Semantic Validation` is not defined by any mechanism

The report up to §13 stops at this diagram:

```text
AI → { act, target, slots, utterance }
                 ↓
        Semantic Validation   ← just a name, not a mechanism
```

The problem: a naive rule (`act != expected → reject`) only catches blatant mislabelling. But if the AI declares the correct `act: negotiate, target: price` while the real `utterance` is *"I'm wondering if you have any financing options."* — then the validator must **understand the semantics of the actual spoken line**, not just compare fields. This is the line that decides whether the architecture truly "stops" intent drift:

```text
Contract Validator
├── Structural validation       — deterministic (schema has the right fields?)
├── Slot validation             — deterministic (max_budget <= 30000?)
├── Constraint validation       — deterministic (forbidden_intents touched?)
├── Forbidden fact validation   — deterministic (no self-added facts outside context?)
├── Forbidden/end validation    — deterministic (no self end_call when not allowed?)
└── Utterance semantic check    — ⚠️ MECHANISM TO SETTLE: rule/keyword? embedding similarity?
                                    a small LLM-judge, separate from the generator?
                                    what confidence threshold rejects?
                                    how to handle "unsure" (ambiguous) cases —
                                    the safe default must be REJECT, not PASS.
```

→ Before coding, answer clearly: **what mechanism the utterance semantic check uses, how far its guarantee goes (rules only catch blatant/keyword drift, not sophisticated paraphrase), and unverifiable cases default to FAIL, not PASS.**

### 14.2 Change the mental model: `allowed_topics` should not be the primary contract

From:
```yaml
allowed_topics: [...]
forbidden_topics: [...]
```
to:
```yaml
behavior: negotiate
target: price
constraints:
  max_budget: 30000
  forbidden_intents: [financing, trade_in, vehicle_change]
  must_not: [invent_facts, end_call]
```

`topics` may still exist as a **secondary signal** (heuristic hint for the validator), but **not as the primary semantic contract**. The principle:

> **A behavior contract defines the action space (the permitted set of actions/intents), not the vocabulary space (the permitted set of spoken keywords).**

This is why the topic-keyword check in §13.1 is not strong enough — it validates the wrong layer (vocabulary instead of action).

### 14.3 Settle on one single structured output for the AI Language Adapter — no two parallel versions

The report currently has 2 mismatched input/output descriptions: §4 says the AI returns `{"utterance": "..."}`, while §13.2 requires `{act, target, slots, utterance}`. **Settle definitively on the structured version** as the single canonical output in the spec — drop the utterance-only version from the official documentation entirely (keep it only as an internal fallback for `say`/deterministic, not applicable to `do:`).

```text
ConversationContext + BehaviorContract + AgentObservation
       ↓
AI Language Adapter
       ↓
CandidateUtterance { act, target, slots, utterance }
       ↓
Caller Contract Validator
       ↓
   PASS ──→ TTS
   FAIL ──→ retry × N ──→ still FAIL ──→ FAIL RUN
```

State the principle explicitly in the spec: **`act/target/slots` are the generator's claim (self-declared by the AI), not evidence.** The validator must always cross-check that claim against the actual `utterance` (§13.3) — never trust self-labelled fields.

### 14.4 Split two kinds of reproducibility — invariant #7 is stated too strongly

Invariant #7 in §4.3 currently reads: *"same scenario + config + TTS model/voice + seed → same behavior."* This is too strong if the AI Language Adapter is a remote/realtime model (never absolutely deterministic even with the same seed). Two guarantee levels must be separated:

- **Behavior reproducibility** (strong guarantee, always true): same scenario + same observed agent events + same seed → **the same permitted behavior transition chain** (WHAT/WHEN unchanged, because Behavior Engine + Orchestrator are deterministic).
- **Utterance reproducibility** (weaker, conditional guarantee): the concrete line the AI generates is only guaranteed identical when (a) using **record/replay**, or (b) using a suitable deterministic/local language backend (not a remote realtime model with uncontrollable randomness).

This is also why Record/Replay (§4.4) is not a side utility but a **necessary compensating mechanism** for this weaker reproducibility.

## 15. Settled implementation order (P0 sequence)

```text
P0-1  Caller Contract              ← foundation: defines action space, constraints, slots
      ↓
P0-2  Semantic Validator           ← the real enforcement boundary (§14.1 must be answered before coding)
      ↓
P0-3  Behavior DSL
      ↓
P0-4  Deterministic Orchestrator
      ↓
P0-5  AI Language Adapter          ← structured output {act, target, slots, utterance}
      ↓
P0-6  sherpa-onnx
      ↓
P0-7  Record / Replay
      ↓
P0-8  Python/Rust contract tests   ← including the validator: same input must give same valid/invalid verdict
```

**P0-1 and P0-2 matter most.** If these two are not defined tightly enough (especially the "utterance semantic check" mechanism in §14.1), then the line:

> "Out-of-contract caller behavior is never allowed to reach the agent."

remains a **product intention**, not an **implementation guarantee**.

### Updated north star (one added line)

> **Lock the intent. Free the language. Never let an unvalidated utterance reach the agent.**
> **The validator is the enforcement boundary, not the prompt.**

The second line clarifies from the start: the prompt (a system prompt telling the AI "don't go off-topic") is **not** a security/safety boundary — it is only a suggestion to the model. The validator (runtime code, independent of whether the model obeys) is the **real** enforceable boundary.

## 16. Remaining work (updated per §14–§15)

1. **[Priority #1]** Design the **Caller Contract** with an action-space-first structure (not vocabulary-first): `behavior/target/constraints{max_*, forbidden_intents, must_not}`, detailed enough for the validator to use for act/target/slot and utterance semantic checks alike.
2. **[Priority #2 — the most important remaining gap]** Settle the concrete mechanism for the **Utterance Semantic Check**: choose between rule/keyword-matching (fast, cheap, but only catches blatant drift), embedding-similarity (better at paraphrase), or a small LLM-judge separate from the generator (strongest but adds latency/cost); define confidence thresholds, and settle the rule: **unverifiable (ambiguous) cases → default FAIL, not PASS.**
3. Settle the **single structured output** for the AI Language Adapter: `{act, target, slots, utterance}` — remove the plain `{"utterance": "..."}` description from the official spec.
4. Design the concrete **Behavior DSL**: primitives (`ask/confirm/deny/negotiate/provide/interrupt/end`...), `do:` shorthand vs explicit syntax, contracts via constraints/forbidden_intents/must_not (not allowed_topics/forbidden_topics).
5. Design the **Orchestrator state machine** in detail: state/event/transition for `SAY → agent turn → next SAY`, timeout hierarchy, interruption, cancellation, failure semantics (including `CALLER_BEHAVIOR_VIOLATION` after bounded retry) — applied to both Python (`lks`) and Rust (`lksr`).
6. POC `sherpa-onnx` + Kokoro/Kitten: benchmark cold/warm startup, RAM, RTF, latency, size, cross-platform (Windows/macOS ARM/Linux x64) to pick the default model.
7. Design the Record/Replay format (JSON): record both the structured AI output (`act/target/slots`) and validation results — needed because utterance reproducibility (§14.4) is only guaranteed via replay or a deterministic backend.
8. Contract tests between Python and Rust: same behavior semantics **and** same validator verdict (valid/invalid) for the same input.

## 17. Round-5 counter-argument (final blocker before the spec is code-ready): the layering for the Utterance Semantic Check

The user confirmed: **the single remaining architecture blocker** is how §14/§16 describe "choosing between keyword / embedding / LLM-judge" as 3 peer options — the wrong framing. They must be **different validator layers, each with its own role**, not a pick-one-of-three.

### 17.1 The Caller Contract Validator is a multi-layer pipeline, not a single step

```text
AI Language Adapter
        │
        ▼
Candidate { act, target, slots, utterance }
        │
        ▼
┌─────────────────────────────────────┐
│     CALLER CONTRACT VALIDATOR       │
│                                     │
│  1. Schema / structural             │
│  2. Act / target                    │
│  3. Slot / constraint               │
│  4. Facts / forbidden actions       │
│  5. Deterministic lexical guards    │
│  6. Semantic intent verification    │
└─────────────────────────────────────┘
        │
   ┌────┴────┐
 valid      invalid
   │           │
   ▼           ▼
  TTS       retry × N
               │
          still invalid
               │
               ▼
             FAIL
```

Classification table per layer:

| Check | Mechanism | Verdict |
|---|---|---|
| JSON/schema | deterministic | hard |
| `act` | deterministic | hard |
| `target` | deterministic | hard |
| slots | deterministic | hard |
| numeric constraints | deterministic | hard |
| forbidden facts | deterministic | hard |
| explicit forbidden intents | deterministic | hard |
| goodbye/end | deterministic | hard |
| obvious lexical drift | rules | hard |
| **semantic intent** | **local semantic classifier / NLI** | hard |
| ambiguous | — | **reject** |

Core principle: **the LLM generator must not grade itself.** If the self-declared `act` matches the behavior but the real `utterance` goes off-topic, still reject — e.g. `act: negotiate` but `utterance: "Do you offer financing?"` → correct `act` is **not enough to PASS**.

### 17.2 Why embedding similarity alone is not enough — classification/entailment is needed, not similarity

Illustrative example: *"negotiate the price"* vs *"I'm interested in buying this car."* — similarity can be very high (both about buying a car), but the second line **does not perform the negotiation act**. Similarity measures "same topic", not "same semantic act". So the semantic check must be **classification/entailment**: determine **which semantic act the line actually performs** (observed act/target/slot), then compare against the expected contract — not measure vector distance between two lines.

```text
Behavior Contract (expected_act, expected_target)
      ↓
utterance
      ↓
semantic verifier
      ↓
observed_act, observed_target/slots
      ↓
compare(expected, observed)
      ↓
match → VALID
mismatch → REJECT
```

Concrete example: `expected_act=negotiate, expected_target=price`; the verifier classifies the utterance *"Do you offer financing?"* as `observed_act=ask, observed_target=financing` → compare → **MISMATCH → REJECT**. This is real intent validation, not topic/keyword matching nor plain embedding similarity.

### 17.3 Core principle: generator claim ≠ validator evidence

```text
Generator claim ≠ validator evidence

AI says:
  act = negotiate

Validator asks:
  "What does the actual utterance mean?"

Only the latter determines validity.
```

`act/target/slots` self-declared by the AI are only a **claim** (a hint making validation easier), **not evidence**. The validator must always classify the real `utterance` itself with an independent semantic verifier before deciding valid/invalid — never trust AI-labelled fields.

### 17.4 Canonical wording for §14.1 (replacing "choose between 3 options")

Replace:
> "choose between rule/keyword-matching, embedding-similarity or a small LLM-judge"

with:

> **Utterance Semantic Check is a layered validator, not a single technique.**
> Deterministic rules enforce hard constraints. A semantic verifier classifies the actual utterance into an observed semantic act/target/slot representation. The validator compares that observed representation against the behavior contract. Embedding similarity may be used as a supporting signal, but must not be the sole basis for accepting an utterance. If semantic verification cannot establish that the utterance satisfies the contract with sufficient confidence, the utterance is rejected. **Unknown/ambiguous is never treated as valid.**

### 17.5 Rename P0-2 accurately — it is two sub-layers, not one lone "Semantic Validator"

```text
P0-2  Caller Contract Validator
      ├── deterministic contract checks   (schema/act/target/slots/constraints/forbidden/lexical guards)
      └── semantic intent verification    (classification/entailment, not plain similarity)
```

Full P0 sequence (overall order unchanged, only P0-2 renamed):

```text
P0-1  Caller Contract
P0-2  Caller Contract Validator (deterministic checks + semantic intent verification)
P0-3  Behavior DSL
P0-4  Deterministic Orchestrator
P0-5  AI Language Adapter
P0-6  sherpa-onnx
P0-7  Record / Replay
P0-8  Python/Rust contract tests
```

### 17.6 Final overall architecture (final version)

```text
                    ┌─────────────────────┐
                    │   Behavior Contract │
                    │ WHAT = LOCKED       │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Deterministic       │
                    │ Orchestrator        │
                    │ WHEN = LOCKED       │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ AI Language Adapter │
                    │ HOW = adaptive      │
                    └──────────┬──────────┘
                               │
                { act, target, slots, utterance }
                               │
                               ▼
             ┌──────────────────────────────────┐
             │  CALLER CONTRACT VALIDATOR       │
             │                                  │
             │  deterministic constraints       │
             │  + semantic intent verification  │
             └───────────────┬──────────────────┘
                             │
                       ┌─────┴─────┐
                     INVALID      VALID
                       │             │
                   retry × N         ▼
                       │            TTS
                    FAIL             │
                                     ▼
                              SimLeg / LiveKit
                                     │
                                     ▼
                                   Agent
```

### 17.7 Canonical terminology (for implementation — do NOT use the §1–§8 terms)

| Old term (dropped) | Canonical term (use) |
|---|---|
| `Language Adapter (optional AI)` / `AI optional` | **`AI Language Adapter` — REQUIRED for `do:`** |
| `Constrained Language Adapter` | **`AI Language Adapter`** |
| `allowed_topics` / `forbidden_topics` (as the primary contract) | **`Behavior Contract` with `constraints{max_*, forbidden_intents, must_not}`** (topics only a secondary signal if still used) |
| "Semantic Validation" (one vague step) | **`Caller Contract Validator`** = deterministic checks + **Semantic Intent Verification** |
| AI output of only `{"utterance": "..."}` | **`{act, target, slots, utterance}`** — the single structure, `act/target/slots` are claims, `utterance` is the evidence to verify |
| "what the AI says" | **`Observed Act`** — the actual classification result of the semantic verifier on `utterance`, compared against the `Behavior Contract` |

`deterministic/template language` now appears only in **implementation/testing** (e.g. unit tests for `say`, or an internal fallback when the AI backend fails), **not** in the main architecture of `do:`.

### Verdict

The report is now tight enough to consider the architecture direction **correct and stable**. The question "may the AI decide intent?" is conclusively resolved (no). The remaining question — the only one to answer before starting P0-2 code — is:

> **How does the runtime determine the real semantic intent of an `utterance`, then prove it sits inside the `Behavior Contract`?**

The settled direction: **layered validation + semantic classification (entailment-style)**, neither a lone `LLM judge` nor lone embedding similarity. When P0-2 is implemented correctly per this layered model, the new north star finally has a matching enforcement mechanism:

> **Lock the intent. Free the language. Never let an unvalidated utterance reach the agent.**
> **The validator is the enforcement boundary, not the prompt.**

## 18. User confirms the implementation checklist is settled, with a warning on P0-2 before coding

The user re-summarized all invariants to keep when moving to code (no repeated detail — already in §9–§17): the Behavior Contract locks WHAT, the Orchestrator locks WHEN, the AI Language Adapter is required for `do:` and only produces HOW, the canonical output `{act, target, slots, utterance}`, generator claim ≠ evidence, the validator is the hard boundary before TTS/LiveKit, bounded retry → FAIL (no fallback), semantic validation is classification/entailment (not plain similarity), unknown/ambiguous → reject, `say:` stays deterministic, record/replay guarantees reproducibility, no rewriting SimLeg/Observer/Assertion/Report.

**Important warning when starting P0-2 code:** do not implement it straight as "call one LLM judge" and call that a hard safety boundary. Design the **validator's interface first** (contract/schema, verdict model, test vectors) — so the semantic backend can later change (small rule-based, local NLI model, LLM-judge, or combined) while the invariant "unvalidated utterance never reaches LiveKit" stays enforced at runtime, independent of the specific model choice. Suggested code order: **contract/schema → validator interface → verdict model → test vectors → then pick/benchmark the semantic model**.

From here, the user dug into 2 more missing pieces of Realtime AI in the workflow: **API cost** and **context management**.

## 19. Language Adapter Context & API Lifecycle (new proposed addition to the architecture)

### 19.1 Do not call the Realtime API for everything — only adaptive turns need it

```text
say: "Hi, I'm calling about my car."
        ↓
direct → TTS                              (no AI call)

do:
  behavior: negotiate
  target: price
        ↓
Behavior Contract + Agent's latest response + relevant context
        ↓
Realtime AI                                (only now call AI)
        ↓
candidate utterance
```

→ API usage is limited to **adaptive turns** (`do:`), not the whole call. This controls cost directly from the architecture, with no separate optimization needed.

### 19.2 Context given to the AI must be structured, not "full transcript"

Wrong: stuffing the whole transcript + scenario + history into every Realtime request.

Right: LKS maintains a structured **ConversationContext**, sending only the relevant part:

```text
ConversationContext
├── Scenario facts
├── Current behavior (act, target, constraints)
├── Behavior progress (turns, retries)
├── Agent latest response
├── Relevant conversation facts
└── Recent turns
```

Realistic JSON example:

```json
{
  "current_behavior": {
    "act": "negotiate",
    "target": "price",
    "max_budget": 30000,
    "turn": 2,
    "max_turns": 3
  },
  "agent_latest": { "text": "The lowest I can do is $31,000." },
  "relevant_facts": [
    "Caller wants the 2022 Honda CR-V",
    "Caller budget is $30,000"
  ],
  "recent_turns": [
    { "speaker": "caller", "text": "Could you do $30,000?" },
    { "speaker": "agent", "text": "The lowest I can do is $31,000." }
  ]
}
```

The AI only needs to know: *"negotiating price now, here is the agent's latest response, continue this behavior with a natural line"* — no need to "remember" the whole call.

### 19.3 How long should a Realtime session live? — Option B confirmed

Two options:

- **Option A — one Realtime session living for the whole call**: lower latency, more natural context, but hard to control/reproduce, cost can grow unchecked.
- **Option B (confirmed) — LKS manages context, each adaptive turn is an independent AI request**:
  ```text
  Agent response → LKS ConversationContext → Realtime request → candidate → Validator → TTS
  ```

**Option B confirmed** — because it matches the entire designed architecture: LKS is the **source of truth for state**, Realtime is only a stateless (from the system's view) **language generation layer** — it keeps no conversation history of its own outside LKS control.

### 19.4 API key / provider config lives in the Language Adapter, not in the scenario

The scenario must **never** contain an API key. Configuration belongs to runtime/config:

```yaml
language:
  provider: openai       # or gemini, etc.
  model: realtime-model
```

```text
Scenario → LKS → Language Adapter → OpenAI Realtime / Gemini Live
```

Only the Language Adapter uses the API key; it never leaks into the scenario or other layers.

### 19.5 The validator may also cost one extra API call — design it to avoid doubling cost

If Semantic Intent Verification (P0-2) also uses an LLM, each adaptive turn can cost **2 AI calls** (1 generate + 1 validate):

```text
adaptive turn
     ↓
Realtime Generator  ── (call 1, if remote)
     ↓
Validator
  ├── deterministic checks (no AI call)
  └── semantic check       ── (call 2, ONLY IF using an LLM-backed verifier)
     ↓
PASS → TTS
```

Therefore: **the validator's interface must leave open using a local/small model** for the semantic check (no mandatory separate API call per turn) — consistent with the §18 warning (do not hard-lock into an "LLM judge").

### 19.6 Context/API workflow summary

```text
              WHAT
Scenario ──────────────→ Behavior Engine
                              ↓
              WHEN ─────→ Orchestrator
                              ↓
              HOW ──────→ Realtime AI
                              ↑
                    controlled context (structured, not full transcript)
                              ↓
                         Validator
                              ↓
                            TTS
                              ↓
                           LiveKit
```

- **API key**: mainly OpenAI Realtime or Gemini Live, configured in the Language Adapter, not in the scenario.
- **Context**: LKS holds state (structured ConversationContext), Realtime is not its own source of truth.
- **Cost**: `say` makes no AI call; only `do:` (adaptive) calls AI; each call receives only the trimmed **Behavior Contract + relevant context + Agent Observation**, not the whole scenario.

→ This is the item to formally add to the architecture under the name **"Language Adapter Context & API Lifecycle"**, because the report had locked WHAT/WHEN/HOW but had not yet described what context the AI receives, how long sessions live, or where API call boundaries sit.

## 20. Realtime does not control playback/flow — it only generates a line when asked

Next question: does Realtime decide when to play audio, and after playing does it need context to generate the next line?

**Answer:** Realtime does **not** decide whether/when to play audio. But it **still needs fresh context after every turn** to generate a fitting next line.

### 20.1 The exact workflow

```text
                    LKS owns the state
                         │
Scenario ────────────────┤
                         ↓
                  Behavior Engine
                  "negotiate price"
                         ↓
                  Orchestrator
                  "caller's turn"
                         ↓
              ┌──────────────────────┐
              │    Realtime AI       │
              │ Context + Agent      │
              │ response             │
              │        ↓             │
              │ generate HOW         │
              └──────────┬───────────┘
                         ↓
                    Validator
                         ↓
                       PASS
                         ↓
                    sherpa-onnx
                         ↓
                    AUDIO READY
                         ↓
                  Orchestrator
                         ↓
                  PLAY AUDIO
                         ↓
                    LiveKit
```

Realtime does **not** say "now I want to play audio". Realtime only says *"given the current behavior and the Agent's response, the natural line I want to say is X"*. **LKS decides whether to play it.**

### 20.2 The loop after audio has played

```text
AI generate → validate → play audio → Agent hears → Agent replies
   → Observer captures response → LKS updates Context
   → Behavior Engine checks:
        ├── behavior done → next behavior
        └── not done → continue behavior → Realtime AI again (with fresh context)
```

Concrete example `negotiate(price, max=$30k)`:

- **Turn 1**: Agent says "The price is $32,000." → Realtime generates "Would you consider coming down to $30,000?" → Validator PASS → TTS → play.
- **Agent replies**: "I could do $31,000." → **LKS does not reset the AI, nor let the AI decide a new behavior itself** — LKS updates state (`turn=2`, `agent_latest="I could do $31,000."`) then builds fresh context to hand back to Realtime.
- **Turn 2**: Realtime generates (on the fresh context) — e.g. "That's still a bit above my budget. Could you do $30,000?" → validate → TTS → play.

### 20.3 Context ≠ control

```text
LKS State → build context → Realtime → generate one candidate
   → Validator → play → Agent response → LKS State update
   → build NEW context → Realtime (repeat)
```

The AI is told: what role it plays, the current behavior, constraints, what was recently said, what the Agent just replied.

**LKS alone decides**: whether it is the caller's turn · whether to call AI · what the current behavior is · whether to speak or wait · whether audio may play · whether the behavior is complete · whether to move to the next behavior · timeout/retry/fail.

> **Realtime decides what to say. LKS decides whether, when, and why it gets said.**

And **after every Agent reply, LKS must hand fresh context to Realtime** if the current behavior needs a further adaptive response — this is exactly why Realtime remains essential in the new architecture (not just one call per behavior).

## 21. LKS determines turn/behavior-complete via events + state machine, not via AI decisions

### 21.1 Three questions LKS must answer itself (never asking the AI)

**(1) When is it the caller's turn?** — The Observer watches audio via LiveKit: `agent_audio_started` → `agent_audio_stopped` → the Orchestrator knows the Agent finished speaking → caller turn. If the Agent has not finished, LKS **does not call Realtime to play a new line**.

**(2) Is the current behavior done?** — decided by the **Behavior Engine** based on Agent observations, not by the AI deciding the flow itself. Example `negotiate(price, max_budget=30000, max_turns=3)`:
- Agent says *"Sure, I can do $30,000."* → Behavior Engine checks: did the Agent accept the target? → YES → `behavior = SATISFIED` → next behavior.
- Agent says *"The lowest is $31,000."* → NOT satisfied → `turn 2/3` → continue behavior → Realtime generates the next utterance.

  **Note:** judging "has the Agent satisfied the behavior" sometimes needs **semantic evaluation** (not always deterministic string matching) — but this is **behavior-state evaluation** (Behavior Evaluator), entirely different from letting the AI decide the next flow.

**(3) When to call Realtime?** — a consequence of (1) + (2):

```text
Agent response → Observer → Behavior Engine
      ↓
behavior complete?
  YES → next behavior
  NO  → Orchestrator → caller's turn?
           YES → Realtime AI → Validator → TTS → Play audio
```

→ **Realtime is not in the control loop.** It sits in the **generation step** inside the control loop — only invoked when the control loop (Orchestrator + Behavior Engine) decides a new line is needed.

### 21.2 The real hard part: "Agent finished speaking" (audio) is easier to determine than "behavior satisfied" (semantics)

Clear example: `arrange_visit(preferred_day=Saturday)`, Agent says *"Saturday works for me."* → easy SATISFIED. But Agent says *"Let me check my schedule. Saturday might work."* → not certainly satisfied — a **Behavior Evaluator** (possibly with a semantic check) is needed to tell them apart.

### 21.3 A more precise architecture — 5 questions for 5 different components

```text
Observer → Agent Observation → Behavior Evaluator → Behavior State
   → Orchestrator → if caller response needed → Realtime
```

| Question | Answered by |
|---|---|
| May it speak? | **Orchestrator** |
| Which behavior is active? | **Behavior Engine** |
| Is the behavior done? | **Behavior Evaluator** |
| What line to say? | **Realtime AI (Language Adapter)** |
| Is that line within contract? | **Validator (Caller Contract Validator)** |
| Should audio play? | **Orchestrator** (only after Validator PASS) |

This is the clean boundary of the new workflow — **the AI Language Adapter answers exactly one question**: *"If the caller must speak now, how to say it naturally?"*. Every other question belongs to other deterministic components.

## 22. Turn detection comes from LiveKit signals — no logic injected into the LiveKit SDK

Final question: where should turns come from — should we patch/inject deep into the LiveKit library?

**Answer: No.** LKS should have a **Turn/Conversation Orchestrator sitting above the LiveKit SDK**, using LiveKit only as a source of **raw signals/evidence**:

```text
LiveKit
  ↓ events/audio
LKS Observer / Turn Detector
  ↓
Orchestrator
  ↓
Behavior Engine
  ↓
Realtime AI
  ↓
Validator
  ↓
TTS
  ↓
LiveKit publish audio
```

### 22.1 LiveKit provides evidence, LKS decides turns

LiveKit emits signals: `agent audio started/stopped`, `participant joined/left`, `track subscribed`, `audio frames`, `data messages`. LKS uses them to build its own `TurnState`:

```rust
enum TurnState {
    AgentSpeaking,
    AgentFinished,
    CallerThinking,
    CallerSpeaking,
    WaitingForAgent,
    Timeout,
}
```

**Do not patch/inject logic into the LiveKit SDK** to turn it into a "caller turn engine" — keep LiveKit purely as transport.

### 22.2 Important nuance: `agent_audio_stopped` ≠ "the agent has completed its conversational turn"

Example — Agent: *"Sure, I can help with that..."* [300ms silence] *"...what is your order number?"* — relying on audio gaps alone easily cuts mid-turn between two sentences. So a **Turn Detector** (still in the LKS layer, not LiveKit) combining multiple signals is needed: audio start/stop, silence duration, transcript finality (if available), interruption, timeout, explicit runtime events → only then conclude "Agent turn finished".

### 22.3 Proposed abstraction — separate transport from turn logic

```rust
trait ConversationTransport {
    async fn play_audio(&self, audio: Audio) -> Result<()>;
    fn subscribe_events(&self) -> impl Stream<Item = TransportEvent>;
}
```

`LiveKitTransport` is just one implementation of `ConversationTransport`. Later, offline/mock testing or another transport can be added without touching the Behavior Engine/Orchestrator.

### 22.4 Four-layer role summary

> **LiveKit = transport + raw signals.**
> **LKS Turn Detector = turns signals into conversational turns.**
> **Orchestrator = decides when the caller may act.**
> **Realtime = only generates the reply when the Orchestrator asks.**

This matches the consistent goal of the whole redesign: **no rewriting the existing SimLeg/LiveKit integration**, only adding a caller orchestration layer (Behavior Engine + Orchestrator + Turn Detector + AI Language Adapter + Validator) on top of the current runtime.

## 23. Settling the whole loop into one single canonical loop

The user confirmed all of §18–§22 is on track and settled the canonical loop in the right order (merging control loop + generation step + turn detection into one diagram):

```text
Scenario / Behavior
        ↓
Orchestrator
        ↓
[caller's turn]
        ↓
Realtime AI
  + current behavior
  + context
  + Agent response
        ↓
"natural answer"
        ↓
Validator
        ↓
PASS
        ↓
TTS (sherpa-onnx)
        ↓
Play audio → LiveKit
        ↓
Agent responds
        ↓
LiveKit audio/events
        ↓
Turn Detector
        ↓
LKS updates context
        ↓
Behavior Evaluator
        ↓
┌───────────────────────┐
│ behavior done?        │
└───────┬───────────────┘
    YES │       NO
        ↓        ↓
 Next behavior  Realtime AI
                 ↓
                loop
```

### Ultra-short summary (one-liner to remember)

> **LiveKit determines the Agent just finished a turn → LKS decides the Caller may speak → Realtime creates a natural line → Validator → TTS → Play audio → LiveKit → Agent replies → loop again.**

### The single most important point of this loop

> **Realtime never plays audio directly.**
> Realtime only **generates the answer** → Validator approves → TTS creates audio → **LKS/Orchestrator plays it via LiveKit.**

And **after every Agent turn, LKS updates context before handing fresh context to Realtime on the next turn** (Realtime never keeps/updates its own context — consistent with §19.3 Option B and §20.3).

## 24. Speech Policy — separating "what to say" (WHAT/semantic HOW) from "how to say it" (delivery/audio behavior)

The user's next question: do the old LKS speech behaviors (stumble, slow speech, stuttering, backchannel, false interrupt, noise, barge-in — listed in §5 under "Caller" and "Interruption") get lost in the new architecture? **Answer: no — they become a separate layer — Speech Policy — independent of the Behavior Contract.**

### 24.1 Principle: separate "WHAT to say" from "HOW to speak / audio behavior"

Example scenario:

```yaml
- do:
    behavior: negotiate
    target: price
  speech:
    pace: slow
    hesitation: true
    stumble: true
    barge_in: possible
```

The overall workflow is unchanged from §23, with just one inserted step:

```text
Behavior Engine
      ↓
Realtime AI            → creates a natural line
      ↓
Validator              → is the line really negotiate(price)?
      ↓
PASS
      ↓
Speech / Turn Controller → turns the line into speaking behavior (pace/hesitation/stumble)
      ↓
TTS
      ↓
LiveKit
```

### 24.2 Slow / hesitant speech (hesitation, stumble) — not Realtime's job

**Realtime neither needs to nor should decide this.** Realtime only creates "a natural line with the right intent". After Validator PASS, a separate **Speech Controller** in LKS applies speech behavior to that line:

```text
text (validated)
 ↓
speech controller
 ├─ pace = slow
 ├─ hesitation
 └─ stumble
 ↓
TTS
 ↓
audio
```

Illustrative example (at audio/delivery level only, meaning unchanged): *"I... I was wondering if you could do $30,000?"*. With `sherpa-onnx`, `pace` is handled at the TTS/audio layer; `stumble/hesitation` is better handled by LKS itself producing controlled speech chunks with pauses/repetitions (not the AI inserting filler words into the content itself).

**Mandatory constraint:** speech style **must not change the semantic contract** — i.e. the Speech Controller runs **after** the Validator, only transforming pronunciation/rhythm of an already-passed `utterance`, never changing its content/meaning (otherwise it reopens the validate-then-change-meaning hole that all of §13–§17 works to close).

### 24.3 Barge-in belongs to the Orchestrator/Turn Controller, not Realtime

Two barge-in directions:

**(a) Caller is speaking, Agent starts talking over (agent-initiated or reactive):**
```text
Caller ────────────────>
                  Agent starts speaking
                         ↓
                    barge-in detected
                         ↓
              stop caller audio
                         ↓
              observe Agent response
                         ↓
              update context
                         ↓
              Realtime generates next natural response
```

**(b) Agent is speaking, the Caller (per scenario) wants to cut in:**
```text
Agent ───────────────────────>
                 ↑
              Caller
                 ↓
            interrupt event
                 ↓
       LiveKit / Orchestrator
                 ↓
        cancel current audio
                 ↓
        Caller continues
```

Both are owned by the **Orchestrator holding the turn/audio lifecycle** (stop audio, when the caller may speak again) — Realtime takes no part in this decision; it is only called back to generate the next line after the Orchestrator has handled the barge-in event.

### 24.4 Two separate layers: Behavior (meaning) and Speech Policy (delivery)

```text
                 BEHAVIOR
                    │
             "negotiate price"
                    │
                    ↓
              Realtime AI
                    │
              natural text
                    │
                    ↓
              VALIDATOR
                    │
                    ↓
             SPEECH POLICY
          ┌─────────┼─────────┐
          ↓         ↓         ↓
        slow     stumble   hesitation
          │         │         │
          └─────────┼─────────┘
                    ↓
                   TTS
                    ↓
                 LiveKit
```

In parallel sits turn control (independent of content speech policy):

```text
LiveKit audio/events
        ↓
   Turn Controller
        ↓
 ┌──────┼───────────┐
 ↓      ↓           ↓
wait   speak     barge-in
```

### 24.5 Scenario syntax: `do:` gains an optional `speech:` field

```yaml
- do:
    behavior: negotiate
    target: price
    max_budget: 30000

    speech:
      pace: slow
      hesitation: occasional
      stumble: occasional
      barge_in:
        enabled: true
```

### 24.6 Final responsibility table (extending §21.3 with Speech Policy)

> - **Behavior (Behavior Engine)** = what the caller must do (WHAT).
> - **Realtime (AI Language Adapter)** = how to phrase that line naturally in semantic terms (HOW — content).
> - **Speech Policy (Speech Controller)** = in what style/rhythm to say it (HOW — delivery: pace, hesitation, stumble...).
> - **Orchestrator/Turn Controller** = when to speak / when to stop / how to interrupt (WHEN).
> - **Validator (Caller Contract Validator)** = whether the line exceeds the behavior contract (before Speech Policy applies).
> - **TTS (sherpa-onnx)** = turns the utterance (post speech policy) into audio.
> - **LiveKit (SimLeg, existing)** = transport.

This is exactly how to **keep all old LKS features** (stumble, slow speech, backchannel, false interrupt, noise, barge-in — listed in §5) **without breaking the new architecture**: they do not disappear; they are reorganized into a separate layer (**Speech Policy** + **Turn Controller**) standing after/alongside the Validator, instead of being mixed into intent-deciding logic as before.

## 25. Renaming "Speech Policy" to "Caller Interaction Planner" — broader scope than pure TTS/audio

The user further refined §24: agreeing a middle layer after Realtime is needed, **but it should no longer be called another "AI" layer**, and the name "Speech Policy" is too narrow because not every caller action goes through TTS.

### 25.1 Why not stuff everything into Realtime

After Realtime produces text (e.g. *"Could you do $30,000?"*), much remains the user wants to test that is **not semantic content**: speaking slowly/fast, hesitation, stumble, mid-sentence pauses, backchannel, interrupting the Agent, barge-in, false interrupt, silence, DTMF, hang up, retry, pre-speech delay, cancelling playing audio. Stuffing all of this into Realtime → returns to exactly the old problem: **the AI starts controlling behavior/timing**, not just content.

### 25.2 Not everything goes through TTS — the reason for the rename

```text
say text     → TTS → LiveKit
DTMF         → LiveKit/SIP                (not via TTS)
silence      → no audio produced           (not via TTS)
barge-in     → cancel/interrupt audio     (control, not via TTS)
hang-up      → terminate call             (control, not via TTS)
```

Because the scope is wider than mere "how to deliver the line" (speech delivery), the name **"Speech Policy" is too narrow**. Renamed to **`Caller Interaction Planner`** (short: **`Interaction Planner`**) — covering both delivery and non-audio control actions.

### 25.3 Overall workflow after adding the Interaction Planner

```text
                    WHAT
Scenario
   ↓
Behavior Engine
   ↓
                    WHEN
Orchestrator
   ↓
                    HOW
Realtime AI
   ↓
Natural Language
   ↓
              Caller Interaction Planner
              ┌────────────────────────┐
              │ speech policy          │
              │ timing                 │
              │ hesitation             │
              │ stumble                │
              │ interruption           │
              │ barge-in               │
              │ backchannel            │
              │ silence                │
              │ DTMF                   │
              │ hangup                 │
              └───────────┬────────────┘
                          ↓
                     TTS / Audio
                          ↓
                       LiveKit
```

(Note: this diagram does not forget the **Caller Contract Validator** — the Validator still runs **before** the Interaction Planner; see the full workflow in §25.6.)

### 25.4 The Interaction Planner must be deterministic — configured via scenario, never AI-decided

```yaml
- do:
    behavior: negotiate
    target: price
  interaction:
    pace: slow
    hesitation: occasional
    stumble: occasional
    pre_delay: 500ms
```

Realtime returns only the semantic content part (structured output as settled in §14.3/§17):
```json
{
  "act": "negotiate",
  "target": "price",
  "utterance": "Could you do thirty thousand?"
}
```

The Interaction Planner (deterministic, reading the `interaction:` config from the scenario) then turns the validator-passed utterance into concrete interaction behavior:

```text
text
 ↓
pace = slow
 ↓
hesitation = occasional
 ↓
stumble = occasional
 ↓
pre_delay = 500ms
 ↓
TTS
 ↓
audio
```

**The AI must not add stumble on its own, nor decide barge-in on its own** — those are `interaction:` configurations declared by the scenario, executed deterministically by the Planner.

### 25.5 Barge-in still belongs to the Orchestrator, not all lumped into the Planner

This is the point the user stressed **must not all be lumped into the Planner**:

```text
Interaction Planner
    ↓
"caller wants to interrupt"
    ↓
Orchestrator
    ↓
cancel current audio
    ↓
wait/interrupt Agent
    ↓
Realtime generates a reply if needed
```

That is, the Planner may **propose/trigger** an interaction event (e.g. "now is the barge-in moment per scenario config"), but the **Orchestrator remains the sole owner of the turn/audio lifecycle** — deciding audio cancellation, when the Agent gets cut off, when the caller may speak again.

### 25.6 Final responsibility table (5 layers, very clean)

```text
Behavior Engine     WHAT      "negotiate price"
Orchestrator        WHEN      "now it's caller's turn" / "interrupt Agent" / "wait 800ms"
Realtime AI          HOW/WORDING   "Could you do $30k?"
Interaction Planner  DELIVERY  "slow + hesitation + stumble" / DTMF / silence / hangup
TTS                  AUDIO     text (post-Planner) → PCM
```

**Technical note (not architecture):** in practice `Orchestrator` and `Interaction Planner` may live in the **same runtime/module** to avoid over-engineering — this is only a **conceptual separation of responsibilities**, not necessarily two separate physical crates/modules from day one.

### 25.7 Final workflow (most complete version, merging all of §13–§25)

```text
Scenario
 ↓
Behavior Engine
 ↓
Orchestrator
 ↓
Realtime AI
 ↓
Caller Contract Validator
 ↓
Interaction Planner
 ↓
 ┌──────────────┬───────────────┐
 ↓              ↓               ↓
TTS           DTMF          Control event
 ↓              ↓               ↓
Audio        LiveKit/SIP     LiveKit
 └──────────────┴───────────────┘
                 ↓
              Agent
                 ↓
           Observer/Turn
                 ↓
          Behavior Engine
                 ↓
                LOOP
```

### Conclusion of this part

> **Realtime creates language (content). LKS needs a separate layer (Caller Interaction Planner) turning language + scenario interaction policy into real caller actions (audio delivery, DTMF, silence, hangup, barge-in trigger) — deterministic, configured from the scenario, never AI-decided.**

This is the abstraction the earlier architecture (stopping at Behavior/Orchestrator/Realtime/Validator/TTS) was missing, and also exactly where all existing LKS features are kept (stumble, slow speech, backchannel, false interrupt, noise, barge-in, DTMF, hangup — §5) without letting them fall under AI control.

Canonical terminology update (added to the §17.7 table):

| Term | Role |
|---|---|
| **`Caller Interaction Planner`** (replacing "Speech Policy") | Deterministic layer turning a validated utterance + `interaction:` config into: speech delivery (pace/hesitation/stumble/pre_delay), or non-TTS control actions (DTMF/silence/hangup/barge-in trigger) |

## 26. Python (`lks`) vs Rust (`lksr`) strategy — 3 rounds of revision, settled: free rewrite, no shared code, only shared behavioral contract

This is a new topic: when implementing the §9–§25 architecture, how should `lks` (Python) and `lksr` (Rust) coordinate? The view went through 3 rounds of revision in this session.

### 26.1 Round 1 — initial assumption: parallel, Python as reference, same shared behavioral contract

The first assumption (before knowing how much LKS is really used): `lks` and `lksr` develop **in parallel**, but `lks` is the **reference implementation**, reaching parity first because it is already "battle-tested" on real LiveKit.

```text
                    LKS Contract
                         │
          ┌──────────────┴──────────────┐
          ↓                             ↓
      lks (Python)                 lksr (Rust)
      PRIMARY                       PARITY
          │                             │
          │ implement first             │ port/adapt
          ↓                             ↓
   LiveKit runtime                Rust runtime
   Realtime caller                Realtime caller
   Interaction Planner            Interaction Planner
   Validator                      Validator
   sherpa-onnx                    sherpa-onnx
          │                             │
          └──────────────┬──────────────┘
                         ↓
                  Contract Tests
```

The principle at this round: **do not rewrite the Python runtime from scratch** — keep the stable parts (LiveKit integration, Observer, assertions, reports, MCP, SimLeg, telephony/DTMF), only **attach** the new caller architecture (Caller Contract, Validator, Behavior Engine, Orchestrator, Realtime Language Adapter, Interaction Planner, sherpa-onnx) alongside. Proposed order: **Phase 1** build end-to-end on Python → **Phase 2** harden with real cases (normal response, slow speech, stumble, hesitation, backchannel, barge-in, false interrupt, timeout, DTMF, agent unexpected response, intent drift, validator rejection, Realtime retry) → **Phase 3** port to Rust based on **golden test vectors** drawn from Python, not copying implementation, only matching semantics.

### 26.2 Round 2 — new information (LKS currently has only the user using it) → switch to supporting a strong rewrite

Upon learning **LKS currently has a single user (the author himself)**, the recommendation flipped: **no need to keep backward compatibility with the old architecture**. The new goal:

> **Redesign the LKS architecture correctly from the start, but Python remains the priority implementation for fast validation on real LiveKit.**

```text
                 LKS Architecture / Contract
                           │
             ┌─────────────┴─────────────┐
             ↓                           ↓
        lks (Python)                 lksr (Rust)
         PRIMARY                    PARALLEL
             │                           │
       implement first             implement after
       + real testing              + parity testing
             │                           │
             └─────────────┬─────────────┘
                           ↓
                    shared behavior
```

No need to: keep the old caller API · keep old internal classes · migrate bit by bit just for backward compatibility · keep the old architecture if it blocks the new design. Why Python still leads: it is the **"test bench"** — build the new architecture (Behavior Engine → Orchestrator → Realtime Language Adapter → Caller Contract Validator → Interaction Planner → TTS → LiveKit → Agent → Observer → Behavior Evaluator → LOOP) on Python first, run against real LiveKit; every discovered problem (mishandled barge-in, bloated Realtime context, stumble before/after TTS, validator semantic false-positive...) gets fixed/benchmarked in Python first, and only once the flow is stable are golden scenarios extracted for the Rust port.

At this round it was further stressed: **the two codebases need not share implementation**, only **concepts** (`Behavior`, `CandidateUtterance`, `ValidationResult`, `InteractionAction`, `Turn`, `Observation`, `BehaviorState`). With a single user, LKS can break hard (`lks v0.x → NEW CALLER ARCHITECTURE → lks v1`), and the old caller part (LLM deciding/reacting/audio by itself) should be **dropped entirely**, not refactored piece by piece — replaced by the settled chain `Behavior → Orchestrator → Realtime (language only) → Validator → Interaction Planner → TTS` from §13–§25.

### 26.3 Round 3 — final settlement after re-checking the real repo: Python/Rust already fully separated, no shared code

After re-reading the real repo, discovered: the repo **already separates** the two implementations into independent directories:
- Python: `src/livekit_agent_simulator/`
- Rust: `src/livekit_agent_simulator_rust/`

`lksr` is a separate **full Rust port**, not a shared-code implementation with Python. → No need to design toward "shared code" — that was never true of the current structure.

**Final settled architecture:**

```text
                     LKS CALLER DESIGN
                           │
              ┌────────────┴────────────┐
              │                         │
          Python lks                 Rust lksr
          PRIMARY                  PARALLEL
              │                         │
       rewrite freely             rewrite freely
              │                         │
              └────────────┬────────────┘
                           │
                 same behavioral spec
                 + same scenario semantics
                 + parity test vectors
```

```text
src/livekit_agent_simulator/           src/livekit_agent_simulator_rust/
├── callers/                           ├── caller/
├── behavior/                          ├── behavior/
├── interaction/                       ├── interaction/
├── language/                          ├── language/
├── tts/                               ├── tts/
├── livekit/                           ├── livekit/
└── ...                                └── ...
```

Python still leads — **not to preserve old code**, but because it is the implementation that runs for real, and `lksr` still has a live-run gap versus Python. Proposed layout for both (parallel, no shared code) as above.

They only need to understand and pass the same test vectors for the shared contract: `Scenario, Behavior, Turn, CandidateUtterance, ValidationResult, InteractionAction, Observation, BehaviorTransition`.

### 26.4 Final principle (replacing the round 1–2 statements)

> **Python and Rust are 2 independent codebases, sharing no code. Python is where the new design gets validated first (free rewrite of the caller subsystem, since the repo currently has one user and can break hard). Rust develops in parallel afterwards under the same behavioral contract + parity test vectors, not "waiting for Python to finish first".**

Rewrite scope: **only the caller subsystem** (behavior/interaction/language/caller — the whole part redesigned in §9–§25) may be freely rewritten in both codebases. The remaining LKS parts — `ops`, report, MCP, LiveKit integration, telephony/DTMF, assertions, SimLeg — are **rewritten only when the new architecture truly requires it**, not the whole repo (consistent with the "no simulator rewrite" principle settled since §5–§6).

## 27. Final consolidation — complete workflow, mapped directly against existing repo modules (source of truth for the rewrite)

This is the final consolidation of the whole research session, mapped directly against the existing Python modules in the repo (`run_orchestrator.py`, `callers/`, `livekit/`, `audio/`, `script/`, `ops.py`) — used as the **source of truth to start rewriting the `lks` caller**, after which `lksr` implements the equivalent architecture on its independent codebase (per §26).

### 27.1 Current architecture → replace only the Caller runtime, don't break all of LKS

Current LKS flow:

```text
Scenario
   │
   ▼
run_orchestrator
   │
   ├── LiveKit / SimLeg
   ├── Caller (Gemini Live / OpenAI Realtime — freely deciding WHAT+HOW+WHEN)
   ├── Observer
   ├── Assertions / Judge
   ▼
Report
```

**The change sits only in the Caller runtime.** `run_orchestrator`, LiveKit/SimLeg, Observer, Assertions/Judge, Report — keep their pipeline positions, only the content inside "Caller" becomes the new architecture cluster (Behavior Engine → Orchestrator (turn/timing) → Language Adapter → Contract Validator → Interaction Planner).

### 27.2 Example scenario running end-to-end through every layer

```yaml
- say: "Hi, I'm calling about the Honda CR-V."

- do:
    behavior: ask_price
    target: price

- do:
    behavior: negotiate
    target: price
    max_budget: 30000
    max_turns: 3
    interaction:
      pace: slow
      hesitation: occasional
```

**Scenario Parser** reads the scenario and creates the initial state (`current behavior`, `contract`, `interaction` config) — **the scenario is the source of truth**; the AI may not decide the next behavior itself.

**Behavior Engine (WHAT)** — answers only "what must the caller do now", never creates lines. It emits an action type (not an utterance):
```text
CallerAction::GenerateResponse
CallerAction::WaitForAgent
CallerAction::Interrupt
CallerAction::SendDtmf
CallerAction::Hangup
```

**Orchestrator (WHEN)** — observes LiveKit events (`agent audio started/frames/stopped`) via the Turn Detector → concludes "agent turn finished" → decides "caller may respond now". Turn logic lives in LKS, **not stuffed into the LiveKit SDK** (consistent with §22).

**Language Adapter (HOW)** — invoked only when the Orchestrator allows the caller to act. Receives structured context (exactly the ConversationContext shape from §19.2), returns the canonical structured output `{act, target, slots, utterance}` (per §14.3/§17). The prompt is essentially: *"I have been told what to do (behavior contract) — how would a real person say that line naturally?"* — not *"what should I do next?"*.

**Contract Validator (hard boundary)** — never allows `Realtime → TTS → Agent` to go direct. Always `Realtime → Candidate → Validator → (PASS→TTS / FAIL→retry)`. Checks follow the exact layered pipeline settled in §17.1: schema → act → target → slots → hard constraints → forbidden intents → facts → **semantic intent**. Illustrative example repeating the report's classic case: the AI returns *"Could you explain your financing options?"* while the contract is `negotiate(price)` → the validator classifies `observed: act=ask, target=financing` ≠ `expected: act=negotiate, target=price` → **REJECT — that line is never played to the Agent.**

**Interaction Planner (delivery)** — runs only after Validator PASS. Applies the `interaction:` config (`pace/hesitation/stumble/pre_delay`) to the validated utterance, turning it into a speech plan (`wait 500ms → speak slowly → small hesitation → TTS`). **Must not change semantic content** — must not turn `negotiate price` into `ask financing`, only change **how it is said**, not **what is said** (consistent with §24.2, §25.4).

**TTS (sherpa-onnx)** — receives the speech plan post Interaction Planner, produces PCM/audio. For `say:` the whole Language Adapter/Validator/Interaction Planner is skipped, going straight `say → TTS → LiveKit` — no Realtime call, cutting API cost exactly as in §19.1.

**Loop** — after the caller speaks, the Agent responds via LiveKit; LKS observes (audio, transcript, tool calls, room events, timing, interruptions) → Turn Detector → Orchestrator → Behavior Engine updates behavior state (`turn 1→2`, or `satisfied → next behavior`). `max_turns`/`timeout` are the deterministic escape hatch guaranteeing a behavior never loops forever (per §13.5).

### 27.3 Not every caller action is speech — Realtime is not the caller controller

```text
say       → TTS → LiveKit
dtmf      → LiveKit/SIP                         (not via Realtime/TTS)
wait      → Orchestrator timer                  (not via Realtime/TTS)
silence   → nothing (no audio produced)           (not via Realtime/TTS)
interrupt → Orchestrator → cancel/interrupt audio (not via Realtime/TTS)
hang_up   → LiveKit/SIP control                 (not via Realtime/TTS)
```

Only `do:` (adaptive) traverses the full Language Adapter → Validator → Interaction Planner chain. This is concrete proof of the principle settled throughout: **Realtime is only a language generation layer, not the caller controller.**

### 27.4 Observer/Assertions/Judge/Report — conceptually unchanged

```text
LiveKit Agent → {Audio, Transcript, Tools} → Observer → events.jsonl → {Assertions, Judge} → Report
```

This whole part **stays as-is** — the repo already has forensic events, reports, SQLite run history, assertions/judge, and `compare` for regression (fully listed in §5). The new architecture only plugs a **new caller subsystem** in front of it, untouched otherwise.

### 27.5 Final architecture diagram (consolidation version, replacing all earlier scattered diagrams)

```text
                         ┌─────────────┐
                         │  Scenario   │
                         └──────┬──────┘
                                │
                                ▼
                    ┌─────────────────────┐
                    │   Behavior Engine   │
                    │        WHAT         │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │    Orchestrator     │
                    │        WHEN         │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │  Language Adapter   │
                    │        HOW          │
                    │ Gemini / OpenAI     │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Contract Validator  │
                    │   HARD BOUNDARY     │
                    └──────────┬──────────┘
                               │ PASS
                               ▼
                    ┌─────────────────────┐
                    │ Interaction Planner │
                    │      DELIVERY       │
                    └──────────┬──────────┘
                               │
                 ┌─────────────┼─────────────┐
                 ▼             ▼             ▼
                TTS           DTMF         Control
                 │             │             │
                 └─────────────┼─────────────┘
                               ▼
                         ┌───────────┐
                         │ LiveKit   │
                         │ Transport │
                         └─────┬─────┘
                               │
                               ▼
                         ┌───────────┐
                         │   Agent   │
                         └─────┬─────┘
                               │
                               ▼
                         ┌───────────┐
                         │ Observer  │
                         └─────┬─────┘
                               │
                               ▼
                    Behavior / Assertions
                               │
                               └──────→ LOOP
```

### 27.6 Final architecture summary line (replacing all earlier north-stars, the most complete version)

> **The Scenario defines WHAT. The Behavior Engine decides the next WHAT. The Orchestrator decides WHEN. Realtime AI (Language Adapter) decides HOW to phrase it. The Contract Validator decides whether that line is valid (hard boundary — never let an unvalidated utterance reach the agent). The Interaction Planner decides how the caller performs the action (delivery, content unchanged). LiveKit is only transport + event source, never deciding turns itself. The Observer records evidence. Assertions/Judge evaluate the result.**

This workflow is used as the **source of truth for rewriting the Python `lks` caller subsystem** (per §26.4); once stable on real LiveKit, `lksr` (Rust) implements the equivalent architecture on a fully independent codebase, sharing only the behavioral contract + parity test vectors, sharing no code.

## 28. 25 unlocked edge cases — must be defined before coding, or "architecture bugs" will surface during implementation

After re-reading the whole report up to §27, the user confirmed the **core architecture is fairly airtight**, but pointed out **25 concrete edge cases** not yet clearly defined — if skipped, coding will hit race conditions/ambiguities with nowhere in the spec to look up. Split into **P0 (lock before code)** and grouped into 5 groups to avoid an endless spec.

### 28.1 STATE / RACE group — most severe because the architecture chose stateless request-per-turn (§19.3 Option B)

**(1) Agent does not respond** — 4 distinct cases must be separated, not lumped into one "timeout": agent still processing (wait) · agent truly not responding (timeout) · agent audio lost (transport problem) · agent ended the call (behavior fail/succeed depending on contract). Proposed states:
```text
WAITING_FOR_AGENT
    ├── agent_response   → continue
    ├── timeout          → TIMEOUT
    ├── agent_hangup     → END / FAIL
    └── transport_error  → ERROR
```
A clear **timeout hierarchy** is needed: turn timeout, behavior timeout, whole-call timeout (not one generic timeout number).

**(5) Race: caller is mid-TTS while the scenario changes state** — e.g. Behavior A is generating/synthesizing, but an Agent response arrives making Behavior A "satisfied" before audio A plays → audio A must **not be wrongly played** after state moved to Behavior B. Needs **`behavior_id + action_id + generation_id`** — every audio/action must verify it is still "current" before publishing.

**(15) Interrupt lands exactly while the Validator is running** — if the Agent starts speaking while the caller's candidate is being validated, that candidate must be marked **stale and DROPPED** (never published), because the turn changed mid-validation.

**(16) Interrupt lands while TTS is generating** — same as (15) but at the TTS step: the TTS job should be **cancellable**, or at minimum its output discarded if the action is already stale when TTS finishes.

**(21) Stale context** — e.g. an AI request based on Context A, but Agent response B arrives before the AI answers → the AI response (based on A) must be **discarded**, not applied to a state that is now B. Needs `context_version` or the trio `turn_id / behavior_id / generation_id` — a candidate is only valid if its generation still belongs to the current state. **The user rates this very high P0** because the architecture chose "each adaptive turn is an independent request, LKS holds the source of truth" (§19.3) — that very choice creates stale responses without versioning.

→ **Common solution for the whole group: generation/state versioning `behavior_id + turn_id + generation_id`**, attached to every candidate/audio/action; the runtime always checks "is this generation still current" before publishing anything to LiveKit.

### 28.2 TURN group — extends the Turn Detector sketched in §22

**(2) Agent speaks but hasn't finished (mid-sentence pause)** — `agent_audio_stopped` does not mean semantic turn complete (the exact issue recognized in §22.2, now needing more concrete states):
```text
AGENT_SPEAKING → possible_end → silence debounce → AGENT_TURN_COMPLETE
```

**(3) Agent streams multiple audio segments** — each `audio_started/audio_stopped` pair should not count as its own turn (e.g. `started → chunk → chunk → interruption → started → chunk → stopped` may still be 1 logical turn). Needs a **correlation/session/utterance ID** to group events belonging to the same agent turn correctly.

**(4) Agent interrupts the caller by itself** — the Orchestrator must distinguish **intentional agent interruption** (agent actively cutting in — valid, handle: cancel caller audio, open a new agent turn) from **transport/audio glitch** (not a real interruption).

**(19) Backchannel should not be its own Behavior** — it is an `InteractionAction::Backchannel`, triggered by the Orchestrator/Interaction Planner per the `interaction:` policy, not mixed with semantic behaviors (`negotiate`, `ask_price`...). Separate clearly: `semantic behavior = negotiate` ≠ `delivery behavior = backchannel`.

**(20) False interrupt / noise** — 4 types must be clearly separated: intentional barge-in · false interrupt · noise · backchannel — these are **interaction semantics** (Interaction Planner/Orchestrator), not **language semantics** (Validator).

### 28.3 AI group — extends the Contract Validator (§17) with more concrete cases

**(8) AI timeout / API 429 / connection reset** — because `do:` mandatorily needs an AI backend (§9), backend errors must **never fall back** to "just say something temporary" (consistent with §13.5). Must: retry per policy → still failing → `CALLER_GENERATION_ERROR` (its own failure reason, not mixed with `CALLER_BEHAVIOR_VIOLATION`). `record/replay` keeps CI independent of the API on replay.

**(9) AI returns a semantically valid but far too long line (e.g. a 90-second paragraph for one `ask_price`)** — semantically right but interactionally terrible. The contract should add a **max utterance size/duration** constraint:
```yaml
constraints:
  max_words: 25
  max_duration: 8s
```

**(10) AI returns multiple semantic acts in one line** — e.g. *"Could you lower the price, and by the way do you offer financing?"* — the structured output may only declare `act: negotiate, target: price` while the real line contains **2 acts**. The semantic verifier must detect multi-act and reject if any act is outside the contract:
```text
primary_act = negotiate(price)
secondary_act = ask(financing)   ← not in contract → REJECT
```
More concrete proof of the principle "the generator's `act` claim is not evidence" (§17.3).

**(11) One line both valid and containing a nested forbidden intent** — e.g. *"$30,000 is really my limit, although I could consider financing."* with `forbidden_intents: [financing]` — even though the primary intent is still `negotiate`, the validator must still **reject** because a forbidden intent appears. Conclusion: the semantic verifier must ask not only *"what is the main intent?"* but *"which semantic actions appear in the line?"* (multi-label, not single-label classification).

**(23) The semantic validator itself fails (crash/unavailable/low confidence)** — must not count as PASS. Canonical verdict set:
```text
VALID | INVALID | UNKNOWN | ERROR
```
with `UNKNOWN → reject`, `ERROR → fail` (never default PASS) — consistent with and more concrete than the "unknown/ambiguous → reject" principle settled in §14.1/§17.4.

**(24) Distinguish validator false negatives vs false positives** — **false positives (invalid → PASS) are the most dangerous** because a wrong utterance reaches the Agent; **false negatives (valid → REJECT)** only cause test fail/retry, far less dangerous. Since the goal is an enforcement boundary, the validator should lean toward **precision/safety** — accepting higher false negatives to avoid false positives. The correct guarantee to state is **not** "zero hallucination" but: **"unvalidated/rejected output never reaches LiveKit"** (exactly as settled in §14.4/§17.4).

### 28.4 EXECUTION group — failure taxonomy separating caller errors from infrastructure errors

**(6) Validator PASS but TTS fails** — must not count as "behavior executed". Correct state: `VALIDATION_PASS` + `TTS_FAILED` (2 separate fields), with a clear policy: retry TTS? fail behavior? fail run? — and **retrying TTS must not accidentally re-invoke the AI** to generate a different line unnecessarily (avoiding extra non-determinism).

**(7) TTS succeeds but LiveKit publish fails** — this is a **transport failure**, not a caller behavior violation — it must be clearly separated in the failure taxonomy (see table below), or later debugging becomes very hard.

**(13) Behavior never satisfies (hits `max_turns`)** — the default must be settled: **FAIL the run** (not "skip the behavior and continue"), because if the scenario requires that behavior and it is skipped, a PASS result would be **fake** (run-level false positive).

**(14) `end` happens in many places** — a canonical `ended_by` is needed with values: `scenario | caller | agent | timeout | transport | error` — otherwise `ended_by: ...` assertions (already in current LKS, §5) cannot distinguish why the call ended.

**Proposed failure taxonomy table (extended, replacing the lone `CALLER_BEHAVIOR_VIOLATION` from §13.5):**

| Reason code | Meaning |
|---|---|
| `CALLER_BEHAVIOR_VIOLATION` | Caller Contract Validator reject (bounded retry exhausted while still invalid) |
| `LANGUAGE_GENERATION_ERROR` | AI backend error (timeout/429/malformed response) after bounded retry |
| `VALIDATION_ERROR` | Semantic validator itself errored/unavailable (not invalid content) |
| `TTS_ERROR` | TTS synthesis failed after Validator already PASSED |
| `TRANSPORT_ERROR` | LiveKit publish failed / disconnected (after TTS already succeeded) |
| `AGENT_TIMEOUT` | Agent did not respond within turn timeout |
| `BEHAVIOR_TIMEOUT` | Behavior hit its own `max_turns`/timeout without being satisfied |

### 28.5 EVALUATION group — the Behavior Evaluator must be fully separated from the Caller Contract Validator

**(12) Behavior completes right after the agent response, no extra caller turn needed** — e.g. `ask_price` → Agent answers the price immediately → Behavior Evaluator concludes `SATISFIED` → move to next behavior **immediately, without one more Realtime call** just because "the caller hasn't said anything yet". The report already has this logic (§21.1) but it needs a **dedicated test case** at implementation time to guarantee no redundant generate call.

**(25) Semantic verification of the Agent response is a different problem; do not mechanically reuse the Caller Contract Validator.** Two completely different questions:

```text
A. Did the caller speak within contract?  → Caller Contract Validator (evaluates Language Adapter OUTPUT)
B. Has the agent satisfied the behavior?   → Behavior Evaluator (evaluates observed INPUT from the Agent)
```

Example: `arrange_visit(saturday)`, Agent says *"Saturday works perfectly for me."* → `SATISFIED`. But Agent says *"I can probably make Saturday work."* → possibly only `PARTIAL/UNCERTAIN` — needs its own semantic evaluation, **not reusing the Caller Contract Validator verbatim** because the problems differ (validator: "is this AI-generated line the assigned intent?" vs evaluator: "does this Agent line satisfy the behavior?").

**(17) `say:` must still obey Orchestration, even though it bypasses AI/Validator** — `say:` skips the Language Adapter/Contract Validator (correct, deterministic), but **must not bypass the Orchestrator**. I.e. not `scenario parser → immediately publish`, but still `Scenario → Orchestrator → caller turn → say → TTS → LiveKit` — skipping the "caller turn" step breaks the turn/timing invariants designed for the whole system (e.g. a `say:` could talk over the Agent without Orchestrator gating).

**(18) The Interaction Planner must not create semantic violations itself** — e.g. validated utterance *"Could you lower the price?"*, if the stumble engine turns it into *"Could you... uh... lower the... price?"* → OK (only hesitation tokens added, meaning unchanged). But if it **randomly adds new content** (e.g. *"...Also, do you offer financing?"*) → **the semantic contract is broken after the Validator** — a new hole outside the Validator's scope because it happens downstream. **Settled invariant:** the Interaction Planner may only perform **provably semantic-preserving transformations** — a narrow whitelist: pause, repetition fragment, hesitation token, pace change — **never inserting/adding new semantic content** in any form (no "freely generate extra text" at this layer).

**(22) Record/replay must record failures too, not only successes** — not just `{"utterance": "...", "validation": "passed"}`, but rejected cases as well:
```json
{
  "candidate": {"act": "ask", "target": "financing", "utterance": "..."},
  "validation": {"status": "rejected", "reason": "TARGET_MISMATCH"},
  "retry": 2
}
```
Without recording the structured candidate + verdict + retry count, a **failed run can never be faithfully replayed** for debugging (more concrete than §19.5/§16 item 7 — record/replay must store the whole generate/validate history of the turn, not just the final utterance).

### 28.6 Five-group summary (quick lookup checklist at implementation time)

```text
1. STATE / RACE    — stale generation, behavior changed while TTS, concurrent agent response, interrupt during generation
2. TURN            — agent pause, streaming audio, barge-in, backchannel, false interrupt, silence, timeout
3. AI               — malformed output, hallucinated intent, multi-intent utterance, AI timeout/429, ambiguous semantic validation, stale context
4. EXECUTION       — TTS failure, LiveKit publish failure, disconnect, agent/caller hangup, behavior max_turns
5. EVALUATION      — caller contract violation, behavior not satisfied, agent semantic response, record/replay failure, caller-error/transport-error/agent-failure separation
```

### 28.7 Top-5 priorities to lock before coding (added to P0, alongside P0-1/P0-2)

If only 5 of the 25 edge cases are picked as **mandatory to design before any coding starts** (not discovered mid-implementation):

1. **Generation/state versioning** — `behavior_id + turn_id + generation_id` on every candidate/audio/action, against stale AI/TTS output (fully resolves group 28.1: races #5, #15, #16, #21).
2. **Turn completion semantics** — never treat `audio_stopped == turn complete`; needs a Turn Detector with debounce/correlation ID per §28.2 (#2, #3).
3. **Final semantic boundary** — guarantee that **after all transformations** (including the Interaction Planner), what actually publishes to LiveKit still holds the Validator-PASSED semantics — i.e. the Interaction Planner only whitelists semantic-preserving transforms (#18), reopening no hole closed in §13–§17.
4. **Full failure taxonomy** — the 7 reason codes in the §28.4 table, cleanly separating content errors (caller/AI) from infrastructure errors (TTS/transport/timeout) — so debugging and assertions (`ended_by`, etc.) stay unambiguous.
5. **Behavior Evaluator ≠ Caller Contract Validator** — 2 separate modules, each with its own semantic mechanism for its own question ("did the caller speak within contract?" vs "has the agent satisfied the behavior?") — no mechanical reuse of one validator for both.

Once these 5 are locked (together with P0-1 Caller Contract and P0-2 Caller Contract Validator settled in §17.5), the architecture counts as **complete enough to start coding** without stopping mid-way to redesign around a surprise race condition/edge case.

## 29. Agreed next step: turn the 5 P0s into formal invariants + state machine, before touching code

The user confirmed the next direction: **turn the 5 priorities in §28.7 into formal invariants**, pinned to their exact pipeline positions from §27.5, then write the complete state machine for the Caller Runtime.

### 29.1 Official pipeline (unchanged from §27.5, restated as the reference frame for the state machine)

```text
Scenario
   ↓
Behavior Engine          ← WHAT
   ↓
Orchestrator             ← WHEN
   ↓
AI Language Adapter      ← HOW
   ↓
Caller Contract Validator
   ↓
Interaction Planner
   ↓
TTS / DTMF / Control
   ↓
LiveKit
   ↓
Observer
   ↓
Behavior Evaluator
   └──────────────→ Behavior Engine   (loop)
```

### 29.2 The two most general invariants — covering all of §13–§28, used as the ultimate acceptance test

> **Absolute invariant #1 (semantic boundary):** No audio utterance may be published to LiveKit unless it has passed through the Caller Contract Validator and received a `VALID` verdict.

> **Absolute invariant #2 (concurrency/staleness boundary):** Every generated action must carry the current (`behavior_id + turn_id + generation_id`) triple; an action with any of the three stale against the newest state **must not execute** (no audio publish, no DTMF send, no control action).

These two invariants are the highest-level reduction of the whole report: #1 synthesizes §13–§17 (validator as hard boundary) + §28.3/§28.5 (multi-act, nested forbidden intent, Interaction Planner must not break post-validator semantics); #2 synthesizes §28.1 (the whole STATE/RACE group) + §28.7 item 1 (generation/state versioning).

### 29.3 Next work: write the complete state machine for the Caller Runtime

The agreed next step — **not written in this report**, to become its own separate follow-up document/artifact — is formalizing the full §29.1 pipeline plus the 2 invariants in §29.2 into a **complete Caller Runtime state machine**, including:

- **States**: (e.g. `IDLE, WAITING_FOR_AGENT, AGENT_SPEAKING, CALLER_TURN, GENERATING, VALIDATING, PLANNING_INTERACTION, SPEAKING, BEHAVIOR_EVALUATING, ...` — elaborated from the scattered states already appearing in the report, e.g. `WAITING_FOR_AGENT` in §28.1(1), `AGENT_SPEAKING → possible_end → AGENT_TURN_COMPLETE` in §28.2(2)).
- **Events**: agent audio started/stopped, transcript received, timeout fired, validator verdict (VALID/INVALID/UNKNOWN/ERROR), TTS result, LiveKit publish result, behavior evaluator verdict (SATISFIED/PARTIAL/NOT_SATISFIED), interrupt/barge-in signal, DTMF, hangup.
- **Transitions**: mapped exactly per the §29.1 pipeline, with every transition involving "publishing audio to LiveKit" guarded by the 2 invariants in §29.2.
- **Timeouts**: per the hierarchy mentioned in §28.1(1) — turn timeout, behavior timeout, whole-call timeout — each level with its own expiry action.
- **Cancellation**: per §28.1(15)(16) — cancel a candidate under validation, cancel a running TTS job, when the turn/generation has changed.
- **Failure states**: mapped 1-1 to the 7 settled failure reason codes in §28.4 (`CALLER_BEHAVIOR_VIOLATION, LANGUAGE_GENERATION_ERROR, VALIDATION_ERROR, TTS_ERROR, TRANSPORT_ERROR, AGENT_TIMEOUT, BEHAVIOR_TIMEOUT`).

**Purpose of that state-machine document:** used directly as the implementation blueprint for Python `lks` first (per the §26 strategy — Python is where the design gets validated on real LiveKit), then Rust `lksr` implements independently under the same behavioral spec (states/events/transitions/failure codes must yield the same verdicts across both codebases, using shared test vectors — consistent with §26.3/§26.4).
