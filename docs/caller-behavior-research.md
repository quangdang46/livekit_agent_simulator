# Caller behavior research — Hamming / Coval / Cekura → lks

**Date:** 2026-07-14  
**Purpose:** Deep research so caller-character work stays on the industry path without becoming SaaS.  
**Sources:** Hamming public runbooks (personas, interruption, tests-as-code, workflow, LiveKit testing, metrics); Coval persona/test-set docs; Cekura personality docs; LiveKit Agents testing/telephony; internal `caller-pattern-plan.md`, `WIP.md`.

---

## 1. Executive takeaway

| Vendor stance | Implication for lks |
|---|---|
| Hamming: persona = **test design artifact**, not a vibe | Keep goal + constraints + behavior + asserts + evidence |
| Hamming: barge-in is a **policy + event taxonomy**, not a bool | Type mid-call inputs (correction / backchannel / noise / DTMF / silence / escalate) |
| Hamming: workflow proof > “sounded right” | Layer Assert (tools, recovery, latency, goals) over transcript |
| Hamming on LiveKit: text pytest ≠ full call | We own the **WebRTC/SIP black-box** layer; do not replace Agents pytest |
| Coval: **persona ≠ test case** | Who/how they sound vs what they’re trying to do — compose, don’t merge |
| Coval: structured knobs + prompt | Interruption rate, silent mode, initiator, noise — not prompt-only |
| Cekura: suite mix 60/20/10/10 | Coverage recipe for target suites, not core hardcoding |
| All: fail → permanent regression | We have `scenario-from-run`; improve extraction quality |

**North star (unchanged):**  
*Dialog policy (LLM Persona) + interaction policy (deterministic Script/Behavior) + layered Assert + forensic evidence.*

---

## 2. Hamming deep dive

### 2.1 Persona extraction template (canonical schema)

From [Voice Agent Test Personas From Support Calls](https://hamming.ai/resources/voice-agent-test-personas-support-calls-template):

```yaml
id: refund_duplicate_delivery_impatient_v1
owner: support_qa
risk: blocking   # exploratory | scheduled | blocking_ci
source_pattern:
  label: failed_delivery_refund_request
  evidence: production_cluster
  private_data_status: redacted
persona:
  caller_goal: "Get a refund … without repeating the order history."
  caller_context: "Believes the company already has the delivery record."
  communication_style: "Impatient, interrupts confirmations, short answers."
  language: "en-US"
  constraints:
    - "Will not provide payment details over the phone."
    - "Will hang up if asked to restart from the main menu."
scenario:
  entrypoint: inbound_support_line
  starting_state: known_customer_with_failed_delivery_fixture
  required_workflow: refund_eligibility_check
fixtures:
  customer_fixture_id: customer_refund_017
  order_fixture_id: order_failed_delivery_017
assertions:
  - type: outcome
  - type: tool_call
  - type: side_effect
  - type: recovery
evidence:
  retain: [call_id, transcript, tool_trace, assertion_results, fixture_state]
promotion:
  gate: blocking_ci
  run_frequency: on_prompt_or_tool_change
```

**Map to lks kinds**

| Hamming | lks |
|---|---|
| `persona.caller_goal` | `Persona.goals[]` (+ numbered checklist prompt) |
| `caller_context` | `Persona.brief` + `Context.notes` |
| `communication_style` | `style` + `traits[]` |
| `constraints` | `Persona.constraints[]` |
| `language` | `Persona.language` / Scenario locale |
| `scenario.entrypoint` / call path | `Caller.mode` + `Telephony.*` |
| `fixtures` | **opaque** `Context.fixtures` + `Dispatch.metadata` (never parsed in core) |
| `assertions.outcome` | PassCriteria / `goals_met` / `transcript_contains` |
| `assertions.tool_call` | Assert.tools |
| `assertions.recovery` | Assert outcomes `recovery` |
| `assertions.latency` | Assert outcomes `latency` |
| `assertions.side_effect` | **Target-side / plugins** (core cannot see sandbox CRM) |
| `evidence` | reports + WAV + events.jsonl + SQLite |
| `risk` / promotion | suite tags + CI policy docs (not runtime yet) |

**Quality rule (Hamming, lock this):**  
*If two agents can both pass while taking different workflow paths, the persona is underspecified or the assertions are too weak.*

**Persona quality rubric (0–2 each, sum 0–14)**

| Dimension | 2 means |
|---|---|
| Source pattern | Repeated prod pattern / known incident |
| Privacy | Synthetic fixtures, explicit redaction status |
| Behavior | Behavior **affects** workflow or recovery |
| Fixture | Stable account/order/tool fixture (target-owned) |
| Assertion | Outcome **plus** tool/side-effect/evidence |
| Risk label | Clear exploratory / scheduled / blocking |
| Owner | Named owner |

| Score | Treatment |
|---|---|
| 0–6 | Do not automate |
| 7–10 | Scheduled only |
| 11–14 | Blocking CI candidate |

### 2.2 Weak vs useful persona (Hamming examples)

| Weak | Why | Useful rewrite pattern |
|---|---|---|
| “Frustrated customer wants help” | No goal, fixture, workflow, assert | Goal + N interruptions + constraint + expected eligibility path |
| “Confused elderly patient” | Demographic shortcut | Ambiguous dates + identity confirm before write |
| “Spanish speaker” | Label only | Starts Spanish, English product name preserved, flow continues |
| “VIP user” | Status without policy | Fixture flag → priority route after one failed step |

**lks authoring rule:** reject vibe-only Persona in guides/validate warnings when goals empty or barge without recovery assert.

### 2.3 Interruption handling runbook (most important for caller sim)

From [Interruption Handling Runbook](https://hamming.ai/resources/voice-agent-interruption-handling-runbook):

**Barge-in is not a boolean.** Classify caller input during agent speech:

| Input class | Means | Default agent action | Evidence |
|---|---|---|---|
| True correction | “No, I meant Friday” | Stop, new turn, keep state | duration, transcript, playback position |
| Backchannel | “uh-huh” / “okay” | Continue; don’t cancel critical audio | text, backchannel decision |
| DTMF | Keypress | Route by prompt policy | digit, menu state |
| Short noise / echo | False interrupt | Resume from safe point | energy, no transcript |
| Long silence | No input | Reprompt / wait / escalate | silence duration |
| Safety escalate | “I need a human” | Stop + handoff | intent, outcome |

**Event taxonomy Hamming wants** (adapt to our forensic `events.jsonl`):

| Event | Why |
|---|---|
| `user.speech_started` / `stopped` | Interruption candidate window |
| `agent.speech_started` / `interrupted` | Playback position + interruptible flag |
| `interruption.candidate_detected` | Why detector fired |
| `interruption.decision_made` | stop / continue / resume / escalate |
| `interruption.recovered` | Task state preserved? |
| `interruption.false_positive` | Noise/backchannel mistakes |
| `silence.timeout` | Separate from barge-in |

**lks today:** `interruption`, `sim.script.cue` (barge_in), `silence.detected`, recovery asserts.  
**Gap:** no **class** on cues; backchannel not first-class; no DTMF; limited false-positive modeling.

**Test matrix Hamming ships (minimum 5–7 cases)**

1. True correction mid-agent  
2. Short backchannel  
3. Background noise / false interrupt  
4. DTMF during prompt  
5. Silence timeout after question  
6. Escalation interrupt (“human”)  
7. Long entity (account number) — patient endpointing (agent-side; we stress with slow/pause persona)

**Metrics (Hamming production, 4M+ calls)**

| Metric | Good | Warning | Critical |
|---|---|---|---|
| Barge-in recovery rate | >90% | 80–90% | <80% |
| Turn latency P50 | <1.5–1.7s | — | — |
| Turn latency P90 | <2.5–3.5s | — | — |
| Turn latency P95 | <3.5s aspirational; ~5s common | 3.5–5s | >5–7s |
| TTFW / TTFA | <400–800ms ideal band | — | >1.5–1.7s feels broken |
| Task completion | >85% | <80% | <70% |
| Reprompt rate | <10% | — | — |

We already hard-gate **black-box** turn/TTFW/recovery via Assert `latency` / `recovery`. Component STT/LLM/TTS breakdown is **agent-white-box** — out of scope.

### 2.4 Tests as code

From [Tests as Code YAML](https://hamming.ai/resources/voice-agent-tests-as-code-template):

Required reviewable fields: `id`, `owner`, `agent_ref`, `persona`, `setup`, `call_path`, `assertions`, `evidence`, `cleanup`.

Risk classes: **blocking** | **scheduled** | **ephemeral**.

```yaml
persona:
  language: en-US
  caller_goal: Move my Tuesday appointment to Friday afternoon
  speech_conditions:
    accent: neutral
    background_noise: office
call_script:
  - user: I need to move my appointment…
assertions:
  outcome: { task_completed: true }
  tools: { required_order: [lookup_identity, …] }
  latency: { turn_p95_ms_max: 1500 }
```

**lks equivalent:** scenario JSONL in Git under target `.agent-sim/scenarios/` — already “tests as code.”  
Missing authoring fields: `owner`, `risk`, `source_pattern` (can live in Scenario metadata tags / comments without core schema break).

### 2.5 Workflow testing (tools / state / handoffs)

From [Workflow Testing Runbook](https://hamming.ai/resources/voice-agent-workflow-testing-runbook):

```text
contract = preconditions + caller scenario + allowed tool sequence
         + state transitions + side effects + handoff + cleanup
```

Layers: conversation · tool call · state · side effect · handoff · regression retention.

**Portable boundary for lks**

| Layer | Core can do | Target must do |
|---|---|---|
| Tool name/args/order | Assert.tools (+ min/max count, args_contains) | Real tool names in scenario |
| Side effect in CRM/calendar | ❌ | sandbox + optional verify plugin |
| Handoff / warm transfer | observe events later | agent implements WarmTransferTask |
| State machine | soft via tools + goals_met | fixture state |

**Do not** put customer state machines in `src/`.

### 2.6 LiveKit-specific Hamming guidance

From [Testing LiveKit Voice Agents](https://hamming.ai/resources/testing-livekit-voice-agents-complete-guide) + [LiveKit integration](https://hamming.ai/integrations/livekit):

| Layer | Tool |
|---|---|
| Text logic | LiveKit Agents pytest |
| Full-stack WebRTC | Hamming (or **lks**) |
| Load | `lk perf` / Hamming concurrent |
| Prod observe | Hamming plugin / OTel |

Hamming LiveKit product features we **partially** match:

| Hamming | lks |
|---|---|
| LiveKit-to-LiveKit rooms | ✅ webrtc_sim (+ SIP modes beyond Hamming’s default pitch) |
| Synthetic caller | ✅ Gemini Live |
| Interruptions / noise | ✅ Script + mixer |
| Transcripts + audio replay | ✅ |
| 50+ cloud metrics / accents / 1k concurrent | ❌ by design |
| Auto-gen scenarios from agent prompt | ❌ (black-box; no agent prompt required) |
| `livekit-plugins-hamming` prod export | ❌ different product |

**Differentiator to keep:** MCP + local forensic + portable MIT + multi-mode SIP without SaaS.

---

## 3. Coval deep dive

### 3.1 Split persona vs test case (critical design rule)

From [Coval Personas](https://docs.coval.ai/concepts/personas/overview) + Test Sets:

| Object | Owns |
|---|---|
| **Persona** | Who + how they sound (voice, accent, noise, interruption rate, silent mode, style prompt) |
| **Test case** | What they’re trying to do (scenario / transcript / exact script) |
| **Metrics** | How you score |
| **Agent connection** | Who is under test |

> Keep behavioral traits in the persona and task instructions in the test case — **don't mix the two**.

**lks mapping**

| Coval | lks |
|---|---|
| Persona characteristics prompt | Persona brief/style/traits/constraints |
| Test case Simulation Input (scenario) | Persona.goals + Execute + Context |
| Exact script input | Script steps `say` |
| Conversation initiator | `Execute.first_speaker` + `caller_nudge` |
| Interruption rate (None/Low/Med/High ~ never/90s/45s/30s) | Behavior barge_ins or speech_conditions (timer-based still weak) |
| Silent mode | silence hold / future silent persona preset |
| Background noise + volume | speech_conditions.noise + noise_gain |
| Hold music timeout | hang_up / timeout after silence (partial) |
| Backchanneling sound pack | cues `voice.backchannel` (exists) — needs non-barge path |
| DTMF/IVR in persona prompt | **missing** as Script action |

### 3.2 Prompt craft for TTS-driven sim callers (Coval)

Coval notes emotion comes from **word choice**, not pitch knobs:

- Prefer concrete frustration language over “you are angry”
- Emotional **progression** (calm → frustrated → escalate)
- Filler words: `um`, `uh`, `hmm` — not `ummmm` / `...` (TTS may spell)

Useful for our Gemini Live prompts (dialog layer), not Script.

### 3.3 Audio-quality persona matrix (Coval)

Same **test set**, vary persona:

| Persona | Stress |
|---|---|
| Standard Customer | baseline |
| Impatient | short answers, low patience |
| Confused | clarification |
| Interruptive | overlap |
| Super Fast Speaker | compressed speech |
| High Background Noise | SNR |
| Low Volume | quiet speaker |

**lks recipe:** suite matrix = goals fixed × trait/speech_conditions variants (docs + templates), not a new runtime product.

### 3.4 LLM people-pleaser problem (Coval)

Sim callers powered by LLMs tend to be **too cooperative** — they don’t stammer, refuse, or derail.

**Countermeasures we already lean on:**

- Hard `constraints[]`
- Deterministic barge/silence/hang_up Script
- `goals_met` judge (caller must pursue goals)
- Explicit refusal / hangup_threat traits + hard hang_up action

**Still weak:** cooperative drift mid-call; fix with more Script anchors + constraint asserts (judge: “did not share card”).

---

## 4. Cekura (shorter)

- Personality = language + noise + interruption + pace + tone  
- Test profile = name/DOB for verification flows (≈ our Context fixtures notes)  
- Suite mix recommendation: **60% standard / 20% challenging / 10% non-native / 10% edge**  
- Expected outcome + metrics after conversation  

Aligns with Hamming layered asserts + our suite tags.

---

## 5. Cross-vendor synthesis → lks principles

### 5.1 Locked principles (do not regress)

1. **Character is a test artifact** — goal, constraints, behavior that changes workflow.  
2. **Dialog vs interaction split** — LLM says what; Script says when.  
3. **Persona vs task compose** — Coval rule; goals not buried only in traits.  
4. **Layered proof** — tools + recovery + latency + goals; transcript alone is weak.  
5. **Evidence first** — events + audio + behavior_summary always.  
6. **Portable fixtures** — core never seeds CRM; opaque metadata only.  
7. **Promotion tiers** — blocking suite small; scheduled broad (process, not SaaS).  
8. **Complement LiveKit pytest** — full-stack room only.  
9. **Gemini always the human** — no observe-only PSTN mode.  
10. **Mode is orthogonal** — WebRTC/SIP does not redefine character.

### 5.2 Capability matrix (caller-relevant)

| Capability | Hamming | Coval | Cekura | lks now | Next |
|---|---|---|---|---|---|
| Goal-driven persona | ✅ | ✅ test case | ✅ | ✅ goals + goals_met | polish authoring |
| Constraints / refusals | ✅ | prompt | partial | ✅ constraints | judge constraint respect |
| Traits / style | ✅ | ✅ | ✅ | ✅ trait library | keep soft |
| Structured barge | ✅ | interruption rate | patterns | ✅ Behavior/Script | **typed classes** |
| Backchannel (non-cancel) | ✅ | backchannel sound | — | weak | **C1** |
| Silence / dead air | ✅ | silent mode | — | ✅ wait | silent preset |
| Noise / SNR | ✅ | noise library | noise | ✅ mixer + gain | pack + docs |
| DTMF / IVR | ✅ | persona DTMF | — | ❌ | **P1.A** |
| Voicemail / AMD stress | partial | — | — | ❌ | **P1.B** |
| Accent matrix | ✅ SaaS | ✅ | partial | locale + trait | defer |
| Multi-judge metrics | ✅ 50+ | ✅ rich | ✅ | PassCriteria + goals_met | multi-judge |
| Fail → golden | ✅ | ✅ | ✅ | scenario-from-run | better extract |
| Concurrent load | ✅ | partial | ✅ | ❌ | `lk perf` |
| MCP / local MIT | ❌ | ❌ | ❌ | ✅ | keep |

### 5.3 What Hamming optimizes that we should **not** copy into core

| Hamming SaaS | Why skip / defer |
|---|---|
| Auto-gen hundreds of scenarios from agent system prompt | Requires white-box prompt; breaks black-box default |
| 50+ cloud metrics + human-agreement dashboards | SaaS; we keep local forensics + hard asserts |
| 1k–50k concurrent Gemini callers | Cost/complexity; use `lk perf` for media load |
| Neural accent catalog | Stay language + non_native trait + target cues |
| Production monitoring plugin | Different product (`livekit-plugins-hamming`) |
| Side-effect sandbox platform | Target-owned; plugins only |

---

## 6. Interruption taxonomy — recommended lks model

Extend Behavior/Script **metadata** (additive):

```text
class:
  correction     # true barge, expect recovery
  backchannel    # short ack, barge_in=false preferred
  noise          # false interrupt / energy only
  dtmf           # digits (action=dtmf)
  silence        # hold / dead air
  escalate       # "human" / supervisor intent (dialog + optional assert)
```

```json
{"kind":"Behavior","spec":{
  "barge_ins":[
    {"id":"fix","after_agent_ms":800,"say":"No — Friday","class":"correction","barge_in":true}
  ],
  "backchannels":[
    {"id":"uh1","after_agent_ms":1500,"asset":"builtin:voice.backchannel","class":"backchannel","barge_in":false}
  ],
  "false_interrupts":[
    {"id":"click","after_agent_ms":400,"asset":"builtin:noise.loud","class":"noise"}
  ],
  "dtmf":[
    {"id":"pin","after_agent_ms":0,"digits":"1234#","class":"dtmf"}
  ]
}}
```

**Assert mapping**

| class | Assert |
|---|---|
| correction | `recovery` (+ optional timing) |
| backchannel | agent continued (event heuristic / judge later) |
| noise | no spurious tool storm; optional recovery |
| dtmf | sip/sim dtmf sequence |
| silence | agent finals after silence / reprompt phrases |
| escalate | tool handoff or transcript + ended_by |

---

## 7. Persona authoring standard (for templates + validate)

Minimum viable **blocking** character:

```text
[ ] goals[] non-empty, actionable (job-to-be-done)
[ ] ≥1 constraint if policy-sensitive
[ ] language/locale set
[ ] If interaction stress required: speech_conditions or Behavior or Script
[ ] If barge present: Assert recovery (or script_verify)
[ ] If hang-up path: Script hang_up + Assert ended_by
[ ] If workflow: Assert.tools for critical calls
[ ] Optional PassCriteria / goals_met
[ ] tags include risk: smoke | regression | telephony | …
```

**Suite mix (Cekura-style recipe for targets)**

| Share | Profile |
|---|---|
| 60% | standard / polite / clean audio |
| 20% | interrupt + noise |
| 10% | non_native / confused / slow |
| 10% | angry / hangup_threat / adversarial / silent |

---

## 8. Metrics we should gate on (black-box only)

Align defaults/docs with Hamming production bands, measured from **room events** (not agent internals):

| Metric | lks source | Suggested hard-gate examples |
|---|---|---|
| Turn p50/p95 | `summary.metrics` + Assert latency | p95 ≤ 3500–5000 ms (env-dependent) |
| TTFW | same | ≤ 3000–5000 ms first agent audio |
| Barge recovery rate | metrics + Assert recovery | ≥ 0.9 when barges fired |
| Recovery ms | behavior_summary | max_ms_after_barge_to_agent_final |
| Task / goals | goals_met + PassCriteria | strict-judge optional |
| Tools | Assert.tools | required names/order soft via min_count |
| SIP | Assert.sip | dial_answered, participant_present |

**Not in core:** WER, MOS, component TTFT, sentiment SaaS scores (optional plugins later).

---

## 9. Recommended work packages (caller-only)

| ID | Package | Industry driver | Effort |
|---|---|---|---|
| **C1** | Typed interruption classes + backchannel path | Hamming interruption runbook | M |
| **C2** | DTMF Script action + events + assert | Hamming + LiveKit + Coval IVR | M |
| **C3** | Silent / voicemail / IVR-only presets | Coval silent mode + AMD stress | M |
| **C4** | Authoring validate + quality rubric warnings | Hamming persona rubric | S |
| **C5** | Multi-judge PassCriteria | Hamming/Coval layered eval | M |
| **C6** | scenario-from-run → goal/constraint/Behavior extract | Fail→golden flywheel | M |
| **C7** | Timer interruption_rate compile (Low/Med/High) | Coval behavioral setting | S |
| **C8** | Constraint-respect judge outcome | People-pleaser counter | S |

Priority for lks niche: **C1 → C2 → C4 → C3 → C5**.

---

## 10. Anti-patterns (research-backed)

| Anti-pattern | Who warns | Why |
|---|---|---|
| Vibe persona (“angry customer”) | Hamming | No measurable failure mode |
| Transcript-only pass | Hamming workflow | Tools/side effects wrong while speech OK |
| Prompt-only interruptions | Coval/Hamming | Non-deterministic CI |
| Mix task into voice preset only | Coval | Can’t reuse persona across goals |
| Global barge bool | Hamming | Backchannel/noise/legal disclosure differ |
| Demographic stereotypes as traits | Hamming | Unsafe + weak tests |
| Every test blocking | Tests-as-code | Noisy CI |
| Core parses business fixtures | our AGENTS.md | Portability break |
| Replace LiveKit pytest | Hamming LiveKit guide | Wrong layer |

---

## 11. References

### Hamming
- Personas template: https://hamming.ai/resources/voice-agent-test-personas-support-calls-template  
- Interruption runbook: https://hamming.ai/resources/voice-agent-interruption-handling-runbook  
- Tests as code: https://hamming.ai/resources/voice-agent-tests-as-code-template  
- Workflow testing: https://hamming.ai/resources/voice-agent-workflow-testing-runbook  
- LiveKit complete guide: https://hamming.ai/resources/testing-livekit-voice-agents-complete-guide  
- LiveKit integration: https://hamming.ai/integrations/livekit  
- Evaluation metrics: https://hamming.ai/resources/how-to-evaluate-voice-agents  
- Metrics formulas: https://hamming.ai/resources/voice-agent-evaluation-metrics-guide  
- Testing guide: https://hamming.ai/resources/voice-agent-testing-guide  
- Load testing: https://hamming.ai/resources/voice-agent-load-testing-guide  

### Coval
- Personas: https://docs.coval.ai/concepts/personas/overview  
- Simulations: https://docs.coval.ai/concepts/simulations/overview  
- Audio quality matrix: https://docs.coval.ai/guides/testing-across-audio-qualities  
- Evaluation guide: https://www.coval.ai/blog/voice-ai-agent-evaluation-guide/  

### Cekura
- Personalities: https://docs.cekura.ai/documentation/key-concepts/evaluators/personality  
- Barge-in testing: https://www.cekura.ai/discover/voice-ai-barge-in-testing-asr-latency-tts-overrun  

### Internal
- `docs/caller-pattern-plan.md` (v1 shipped)  
- `WIP.md` (roadmap)  
- `docs/telephony.md`, `docs/PROBLEM.md`  
- `src/.../persona_traits.py`, `behavior_compile.py`, `asserts.py`, `script/models.py`

---

## 12. One-paragraph product decision

Hamming says: build **reviewable failure modes** from call patterns, classify interruptions, prove workflows with tools and recovery, promote only high-signal cases to blocking CI, and use full-stack WebRTC for timing. Coval says: separate **who** from **what**, expose structured pacing knobs, and fight LLM people-pleasing. Cekura says: cover a realistic personality mix. **lks already sits on the right architecture** (Persona + Behavior→Script + Assert + forensics + MCP). The research-backed next moves are **typed mid-call interaction classes, DTMF, silent/machine presets, and stricter authoring quality** — not a new persona runtime and not Hamming’s SaaS surface.
