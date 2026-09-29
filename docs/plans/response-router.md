# Plan — Response Router for livekit-agent-simulator

**Status:** designed, not implemented
**Date:** 2026-09-29
**Solves:** `PROBLEMS.md` §2 (no decision layer) and §3 (script exhaustion)
**Method:** 20-agent workflow — 4 research fan-out (116 findings), 3 contract-mechanics, 2 competing designs, 10 adversarial verifications (5 lenses × 2 designs), 1 synthesis. **All 10 verifiers refuted both designs**; the spec below is the post-refutation merge.

---

## 1. The contract (fixed by owner — not reopened here)

```ts
route(agent_transcript, responses) -> responseId
```

The router returns **exactly one** value: a `responseId` from the scenario's authored catalog. It does not generate text, does not reason, does not touch the agent or the backend, and never returns a value outside the catalog.

The routing unit is a **response**, not a fact. A response may inform, refuse, confirm, re-ask, agree, or end the call. An earlier `facts` shape was rejected as over-fitted to the happy path (company / phone / address).

```yaml
responses:
  company_name:
    intent: ask_company_name
    instruction: Provide the company name.
    text: "It's Bluebird Property Management."
  off_script:
    system: true
    text: "I'm sorry, could we stay focused on the property inquiry?"
```

- `intent` — stable semantic key for eval/debug. The model sees **only** intent+instruction semantics, never the `text`.
- `instruction` — what the router matches against.
- `text` — ground truth the backend publishes (TTS).
- `system` — framework-owned entry, not a scenario-authored intent.
- `reusable` — may answer the same question more than once.

### `off_script` is what makes "always valid" structural

When no authored intent matches, the router returns the system response's id. It is a **normal catalog entry**, not a code path and not an AI fallback — so *"always a valid responseId"* holds by construction, not by hope. The catalog always contains it (see D7).

The router does **not** correct the agent; it has no authority to. It responds as a reasonable caller and emits a **verdict** so the deviation is attributable:

| verdict | meaning |
|---|---|
| `matched` | argmax was not the system id |
| `off_script` | the **agent** deviated from the flow |

This is the point of the whole design: without attribution, an agent bug is indistinguishable from a flaky test.

### Not in the contract

`null`, `askForClarification`, any fallback branch, any probability, and **`confidence`** — an earlier draft carried it as telemetry; it is now removed from the return type entirely. Quality metrics belong in evidence, not the contract.

### config.yaml — engine only, no business logic

```yaml
router:
  provider: openai          # openai | gemini   (jev parses, then fails loud — no adapter)
  model: gpt-4.1-nano
  api_key: sk-...           # config.yaml is gitignored; no api_key_env indirection
  timeout_ms: 1500
  temperature: 0

text_planner:
  enabled: true             # false => publish catalog text verbatim (byte-exact regression)
  # drives the EXISTING caller_contract/text_backends.py, not a new port
  provider: openai
  model: gpt-4.1-nano
  api_key: sk-...
```

**Every key has a named consumer** — `provider` selects the port implementation, `model` /
`api_key` / `temperature` build the adapter request, `timeout_ms` bounds the port call,
`text_planner.enabled` selects byte-exact versus paraphrased publication.

> **Two keys were cut in polish round 3** as dead surface (AGENTS.md: *"if you can't name the flow
> that calls it, it is dead on arrival"*):
> - **`unknown_policy`** — its only legal value was `off_script` and nothing branched on it. A
>   validated constant is still a dead knob, and the off-script verdict is a **catalog lookup**,
>   not a config branch. It returns only if a second terminal behaviour is ever designed.
> - **`prompt_version`** — existed for cross-backend benchmarking that no bead performs, and Jev
>   has no adapter. It returns only alongside a real benchmark bead.
>
> `reasoning_effort` is a **code constant in the adapters**, not a key — an operator choice nobody
> would exercise. A test asserts all three stay absent from both the config and the snapshot, so
> adding one back fails a test rather than starting an argument.

> **Two keys were cut in polish round 3** as dead surface (AGENTS.md: *"if you can't name the flow that calls it, it is dead on arrival"*):
> - **`unknown_policy`** — its only legal value was `off_script` and nothing branched on it. A validated constant is still a dead knob; the catalog's system entry already *is* the policy, so the key was a second source of truth. It returns only if a second terminal behaviour is ever designed.
> - **`prompt_version`** — existed for cross-backend benchmarking that no bead performs, and Jev has no adapter. It returns only alongside a real benchmark bead.
>
> `reasoning_effort` is a **code constant in the adapters**, not a key — an operator choice nobody would exercise.
>
> Every remaining key has a named consumer: `provider`/`model`/`api_key`/`temperature` → the adapters, `timeout_ms` → the port, `text_planner.enabled` → the driver.

The enum is built **at runtime** from the response ids in the scenario. Nothing hardcodes `company|phone|address`.

### Two ports, two responsibilities

```
agent transcript
      │
      ▼
 Decision Router          ← WHAT: picks a responseId
      │
      ▼
 Response Catalog         ← authored ground truth
      │
      ▼
 Text Backend            ← HOW: persona phrasing  (caller_contract/text_backends.py — REUSED, not new)
      │
      ▼
 ValidationResult(VALID, reason="ROUTED")   ← carve-out, see D2
      │
      ▼
 TTS
```

The router never writes words. The text backend never chooses a response. Keeping them separate is what lets selection stay deterministic while wording stays natural.

**The cost, stated plainly:** with `text_planner.enabled: true` the published wording is **not** byte-stable across runs, so a router scenario is not byte-replayable. `text_planner.enabled: false` restores byte-exact output. Combined with D12 this means the two are the same constraint seen from two sides.

### Schema shape and limits (v2-1 research, verified against primary vendor docs)

**The enum MUST be wrapped.** A bare top-level `{"type": "string", "enum": [...]}`
is invalid and the API will not accept it, so one shape serves both providers:

```json
{"type": "object",
 "properties": {"responseId": {"type": "string", "enum": ["<authored ids>"]}},
 "required": ["responseId"], "additionalProperties": false}
```

`required` is not decoration on either provider. OpenAI strict mode requires it outright; on
Gemini its **absence is what makes `{}` reachable**, because every property is optional by
default there.

**Two ceilings, and the second one usually binds first.** From the OpenAI structured-outputs
guide:

- *"A schema may have up to 1000 enum values across all enum properties."*
- *"For a single enum property with string values, the total string length of all enum values
  cannot exceed 15,000 characters when there are more than 250 enum values."*

A count-only check waves a 400-id catalog straight through to a live 400 — and that failure
would be attributed to the agent under test. Both are enforced at **parse** time with file:line
(`responses.py`: `MAX_RESPONSES`, `ENUM_LENGTH_LIMIT_FROM`, `MAX_ENUM_TOTAL_CHARS`).

**Refusal is not a distinct status.** On OpenAI it arrives as HTTP **200** with
`content[].type == "refusal"` (Responses) or `message.refusal != null` (Chat Completions). The
adapter maps it to a harness fault. Fabricating a real-looking authored id there is the one
outcome the whole catalog design exists to prevent.

**Gemini publishes no numeric enum cap.** A large enum is one of four named complexity triggers
for a hard `InvalidArgument: 400`, so the OpenAI ceilings are used for both providers as the
conservative shared bound. Gemini's other documented traps: don't duplicate the schema in the
prompt, and set `maxOutputTokens` explicitly so thinking tokens cannot consume the whole budget
and return a candidate with zero text parts.

**Ranking, for the record:** OpenAI strict mode is the stronger guarantee against an out-of-set
label — reasoned from both providers' own docs, **not measured**. Neither publishes a conformance
rate.

**A trap worth naming:** `interaction_planner.plan_speak()` is **not** a text-planning port. `interaction_planner.py:84-85` says so outright — *"Deterministic delivery-layer planner. No AI, no free-text generation."* It inserts hesitation/stumble tokens from an authored `InteractionConfig`, and the `do:` path already discards it (`driver.py:786-790`, to keep TTS input byte-identical to what the validator saw). Toggling *that* would add filler tokens and break validator identity without making anything more natural. The AI phrasing layer is `caller_contract/text_backends.py`.

---

## 2. What the research changed

Three findings materially altered the design. Each is cited in the synthesis; the ones that mattered:

**The `confidence` vs `probabilities` framing was wrong.** Both derive from the same distribution; `confidence` is a *peakedness scalar compressed from* that distribution, not "certainty the decision is right". TypeSafe's own docs contradict their launch blog on this. This is why `confidence` was dropped rather than kept as telemetry — the concept is easy to misread, and a misread confidence in a test harness is worse than none.

**"A correct agent can never produce a false `off_script`" is not free.** It required a `routeable()` gate (D9) and a selection-rule phrasing for the system entry (D10) — a model handed N imperatives has a strong prior to return one of them.

**A degenerate router must be a red run, not a green one** (D5). A router returning the same id three turns running is a harness defect and raises `RouterTerminal` → `EndedBy.ERROR`. Repeated `off_script` is deliberately **not** terminal: killing the call would be the simulator correcting the agent, which the contract forbids.

---

## 3. The fourteen decisions

| # | Decision | Why it matters |
|---|---|---|
| **D1** | Insert at the `do:` retry loop, guard **inside** the `try` | A harness fault must fail the run loudly, not be silently turned into an `off_script` line spoken into a live call |
| **D2** | Validator bypass is real; state it, don't treat it as "no checks" | The router path skips `evaluate_behavior` — say so explicitly |
| **D2a** | **Amendment:** the text planner runs on the router path; only the *validator* is bypassed | See Amendment 1 below — the routed text is a persona paraphrase, not the authored fixture |
| **D3** | Bypass `evaluate_behavior` **and** `BEHAVIOR_TIMEOUT` on the router path | The router picks; the author no longer describes a behavior to validate |
| **D4** | Exactly two verdicts; `undetermined` **rejected** | A third verdict would let a network blip publish "could we stay focused…" into a real call and fail a `must_not_phrases` guard — a harness fault failing a test |
| **D5** | Constant-router → `RouterTerminal` → red. Repeated `off_script` → not terminal | Degenerate router must never be a green suite |
| **D6** | Never map a router condition onto `EndedBy.AGENT` | Otherwise caller-harness failures masquerade as the agent hanging up |
| **D7** | System entry is **authored + required**, reserved id | `AGENTS.md:33` forbids business strings in `src/`; and "always valid" becomes a **parse-time** guarantee the author can see in `lks export` |
| **D8** | `reusable` keeps its meaning; enum shrinks when options are spent — reconciled by **schema caching** (`sha1(sorted(options))`) | Avoids paying OpenAI's per-schema decoder compile every turn; amortises to ~0 over a run |
| **D9** | `routeable(agent_text)` gate: unroutable → no router call, no publish, **no verdict** | A correct agent must never be falsely accused of going off-script |
| **D10** | `matched` == "argmax was not the system id", stated out loud; event carries `agent_text` **and** `agent_text_sha` | Truncation can never hide a divergence during audit |
| **D11** | `config_snapshot` emits `router` only when configured | Keeps existing scenario snapshots byte-identical |
| **D12** | `--record` / `--replay` **forbidden** on router scenarios in v1 | Record/replay cannot capture an LLM decision; **and** with the text planner on there is a second non-deterministic call per turn |
| **D13** | Parse + export land **together**; `caller_steps` stays required | A parse-only change would let `lks export` silently drop the catalog |
| **D14** | Vendor neutrality: no `openai` / `google-genai` import outside the two adapters; raw `urllib` like `text_backends.py` | The port is one method: `_call(options) -> tuple[str | None, int, int]` |

---

## 3a. Amendment 1 — the text planner stays on the router path

**Status:** accepted by owner 2026-09-29. Supersedes the D2 rationale and the "Byte-faithful publish" paragraph in Appendix A (§2, line ~916) **only where they say the routed text is never passed through a generator**.

**Change.** `_route_candidate` gains one step between the catalog lookup and publish. **No new port.** The existing text backend is reused; only its *input* changes from a `BehaviorContract` to a `ResponseCatalog` entry.

```python
response = self.responses.get(decision.response_id)

utterance = response.text
if self.text_backend is not None:              # config: text_planner.enabled
    # Through the ADAPTER, not the backend directly: generate_candidate
    # (language_adapter.py:122-147) owns the bounded retry loop. The earlier
    # snippet called self.text_backend.generate(...) and silently lost it.
    candidate = await self.text_backend.generate_candidate(
        build_routed_context(
            contract=contract,                 # REQUIRED — see NOTE 1
            response=response,
            turn=turns,
            agent_latest=agent_text or None,
            recent_turns=log,
        )
    )
    utterance = candidate.utterance
```

**NOTE 1 — `contract` is not optional.** The earlier signature omitted it, which is a latent
crash, not a style issue: `_REQUIRED_CANDIDATE_KEYS = ("act", "utterance")` is checked at
`language_adapter.py:97` with a *falsy* test, and `CandidateUtterance.validate()` raises
`ValueError("act must be a non-empty string")`. With no contract the model has no `act` to echo
and would have to **fabricate** one to survive the parse. The driver's in-hand contract is free —
`generate_candidate` already accepts `contract` and never reads it — and it leaves
`_parse_backend_response` genuinely untouched.

**NOTE 2 — the routed context is a two-line delegate, not a second builder.**

```python
def build_routed_context(*, contract, response, turn, agent_latest, recent_turns, relevant_facts=None):
    ctx = build_context(contract=contract, turn=turn, agent_latest=agent_latest,
                        relevant_facts=relevant_facts or [], recent_turns=recent_turns)
    ctx["canonical_text"] = response.text
    return ctx
```

Re-emitting the four key literals would create a second shape that can silently drift from
`build_context` — the AGENTS.md "one clear API" failure. `DEFAULT_RECENT_TURNS_CAP` is inherited
this way, so the routed path cannot drift to a different turn cap.

**`relevant_facts` stays present and `[]`.** It is a `run()` keyword (`driver.py:209`) that
nothing populates — the only production call site, `live_wiring.py:491-502`, omits it — so it is
`None → [] → []` on every real run. Omitting it would require the divergent second builder above.

**REJECTED, and it is a trap:** putting `response.text` into `relevant_facts` as well as
`canonical_text` shows the model the authored line twice — once as *the line to phrase*, once as
*a known fact the persona has* — which is an explicit invitation to contextualise or embellish.
The caller's `off_script` count would then mix router deviations with wording drift, and
attribution stops being interpretable. `canonical_text` is the **only** place the authored text
appears in a routed context.

**One prompt addition is unavoidable — do not claim this is input-only.** The existing `_SYSTEM_PROMPT` (`text_backends.py:33-57`) says *"you only phrase the CURRENT behavior naturally"* and the response envelope is `{"act", "target", "slots", "utterance"}`. The context has nowhere to carry the canonical line:

```python
{
  "current_behavior": {"act", "target", "max_budget", "turn", "max_turns"},
  "agent_latest": {"text": ...} | None,
  "relevant_facts": [...],
  "recent_turns": [...],
}
```

So `build_routed_context` adds exactly one key and the prompt gains one clause:

```python
    "canonical_text": response.text,     # the single new key
```

> *"`canonical_text` is the line to phrase. Stay strictly on it."*

`act` stays in the envelope and is echoed back unchanged — harmless, and D3 already established the behaviour verb is cosmetic on this path. `_REQUIRED_CANDIDATE_KEYS = ("act", "utterance")` (`language_adapter.py:90`) keeps working, so the parse path is untouched.

> ⚠️ **Three pre-existing, distinct things are easy to confuse with a "text planner". Check the real signature before assuming an API:**
>
> | Thing | What it is | AI? |
> |---|---|---|
> | `caller_contract/text_backends.py` | The LLM phrasing layer — `generate(context: dict) -> dict`, urllib, no SDK. **This is what the router path reuses.** | **yes** |
> | `CallerInteractionPlanner.plan_speak(validated_utterance, interaction)` | Deterministic token-level shaper — inserts hesitation, duplicates a token as a stumble, from an authored `InteractionConfig`. Docstring: *"Deterministic delivery-layer planner. No AI, no free-text generation."* | **no** |
> | `behavior_compile` / `semantic` | Rule-based behaviour compilation and the lexical verifier D2 bypasses. | no |
>
> `interaction_planner.py:87` is `def plan_speak(self, validated_utterance: str, interaction: InteractionConfig | None) -> InteractionOutcome:` — **two** parameters, called as `plan_speak(candidate.utterance, _interaction(action))` at `driver.py`. There is no `canonical_text`, `persona`, `behavior_instruction`, `allow_hesitation`, or `allow_stumble` parameter anywhere in the code. The `do:` path already discards `plan_speak` output to keep TTS input byte-identical to what the validator saw (`driver.py:786-790`), and this amendment keeps that: the shaper stays off on both planner modes.

**Why the validator carve-out still holds — with a corrected reason.** The original D2 argued "the routed text is authored ground truth, not a generation". That was true before the planner was re-enabled and is no longer. The carve-out is still required, for a stronger reason:

D3 already establishes that on the router path **the behavior verb is genuinely cosmetic** — the flow is one `do: provide`, and `caller_steps` reuses `provide` rather than adding a verb. The validator scores *utterance against that verb*. Scoring a persona paraphrase of `"It's Bluebird Property Management."` against the verb `provide` at `_NO_MATCH_CONFIDENCE = 0.2 < 0.5` produces `CALLER_BEHAVIOR_VIOLATION` — **an agent bug reported as a caller bug**, which is the exact attribution inversion `off_script` exists to prevent. The carve-out is now *more* load-bearing than it was, not less.

**What this costs.**

| | `text_planner.enabled: true` | `false` |
|---|---|---|
| Wording | persona paraphrase | authored verbatim |
| Byte-replayable | ❌ | ✅ |
| LLM calls per turn | 2 (router + planner) | 1 |
| Attribution (`off_script` verdict) | ✅ unchanged | ✅ unchanged |

D12 is therefore a hard requirement rather than a recommendation, and the smoke test (§5) must cover **both** modes — the routed run is otherwise only verified in its non-deterministic form.

**Still bypassed, unchanged:** `interaction_planner.plan_speak()`. It is a deterministic token-level delivery shaper, not a text AI, and the `do:` path already discards it to keep TTS input byte-identical to what the validator saw. Re-enabling it would add filler tokens and break that identity for no naturalness gain.

### Rejected verifier suggestions

The synthesis applied 22 mustFixs. It **rejected** the rest of what the verifiers proposed, notably: keeping a `confidence`-gated third verdict, and making repeated `off_script` terminal. Both would have re-introduced the branch the contract deliberately removed.

---

## 4. Build order

Each step independently reviewable and revertable. Steps 1–3 are pure additions with zero integration, so they can be reviewed without touching the driver.

| Step | Change | Size | Integration |
|---|---|---|---|
| **0** | Revert the committed mutants (see §6) | **4 sites, not 3** | blocks everything |
| **1** | `caller_contract/responses.py` — catalog, reserved id, parse-time ceilings | ~250 lines | none |
| **2** | `config.py` — additive `router` / `text_planner` blocks, validated at load | ~90 lines | none |
| **3** | `caller_contract/router.py` + `router_openai.py` / `router_gemini.py` | ~600 lines | none |
| **4** | Scenario parse + export, landing together (D13) | 4 files, ~45 lines | schema only |
| **5** | `driver.py` — the routed branch | ~90 lines | **the risky step** |
| **5b** | `live_wiring.py` — **attach the router**, or Step 5 is dead code | ~70 lines | **see below** |
| **6** | Evidence: `contract_summary` router key, events, `_describe` cases | ~60 lines | reporting |
| **7** | *(separate commit)* extract `http_json.post_json` shared by text backend and router | — | refactor |

Only **Steps 5 and 5b** touch the driver control flow. Everything before it is additive and
testable in isolation.

**Step 5b was missing from this table originally, and its absence is why the router shipped
dead.** A branch that is never wired is not implemented — the branch was correct, every unit test
was green, and no real run ever reached it. Step 5b's exit criterion is mutation, not coverage:
replace `_attach_response_router(driver, cfg, scenario)` with `pass` and confirm a test fails.

---

## 5. First smoke test

The failure mode this design exists to prevent is not a crash — it is a **wrong route that still reads as PASS**. The first test must therefore assert attribution in both directions:

1. A correct, on-script agent produces **zero** `off_script` verdicts.
2. A deliberately off-script agent (one that asks something no response covers) produces an `off_script` verdict, and the run is attributed to the **agent**, not the caller.
3. A repeated question returns the same `responseId` twice when `reusable`, and never when not.
4. **Archived-scenario regression output is byte-identical.** ⚠️ Correction: the seven archived
   scenarios live in `voice-ai-agent/.agent-sim/scenarios/_archive/`, not in this package
   (AGENTS.md Boundary), so no test here can observe them. The regression net that *is* testable
   from here is `templates/*` + `templates/examples/*` + `tests/fixtures/*` + the parity vectors.
   Checking the archived seven is a manual step in the target-repo hand-off
   (`docs/migration-caller-steps-to-responses.md`).
5. **Both planner modes** (§3a): with `text_planner.enabled: true` the verdict stream is identical to `false` — a persona paraphrase must not change attribution. With `false`, output is byte-identical run to run.

If (1) or (4) or (5) fails, nothing downstream is trustworthy.

---

## 6. Blocker before any of this: committed mutants — ✅ RESOLVED

`c0d3e26 test(mutation): inject A/B/C mutants in caller-contract for judge checks` was on `main`, with `d431686` on top. **Reverted in `eda4216`** (bead `…-3tv.1`, the shared DTMF/router prerequisite).

| File | Line | Was | Restored to |
|---|---|---|---|
| `caller_contract/live_wiring.py` | 58 | `f"MUTANTA {reason} MUTANTB {detail}"` | `f"{reason}: {detail}"`-style message |
| `caller_contract/live_wiring.py` | 324 | **the `record_path`/`replay_path` mutual-exclusion guard, deleted** | `raise` on both set |
| `caller_contract/live_wiring.py` | 537–542 | `MUTANTC_caller_end` … `MUTANTC_end` | `contract_caller_end` … `contract_error` |
| `caller_contract/dsl.py` | 191 | `if self.kind == "say": self.bypasses_ai_and_validator = True` | do not bypass for `say` |

⚠️ **There were FOUR sites, not three.** The hand-off listed three; the fourth (`live_wiring.py:324`)
was found by the peer session while executing the revert. Reverting three would have left
`run_contract_driver_path(record_path=…, replay_path=…)` running both branches instead of raising.

Verify, not assume: `git diff c0d3e26~1 -- caller_contract/dsl.py caller_contract/live_wiring.py`
returns empty — the tree is byte-identical to pre-mutant, which is stronger than checking each
line.

**And the suite was green with the mutants in it** — 1156 passed either way. They were never
caught, because all four `end_reason` assertions in `test_contract_live_wiring.py` reach the
`SCENARIO` ending, which `MUTANTC` does not touch; the other five keys were unobserved. That is
why the coverage-gap bead exists (pin all six, plus the message format and the record/replay
guard). Separately, **4 of the 6 keys in the map are unreachable at all**: `_fail()` always
attaches a `RunFailure` and `run_contract_driver_path` raises before reaching the map, so only
`contract_scenario_end` and `contract_agent_end` can ever be returned — a pre-existing
`no dead features` violation, tracked as `dead-end-reason-keys-jrp`.


### 6a. Sibling track — DTMF keypad restore

Already tracked as beads, and **must be implemented alongside this plan** rather than deferred:

| Bead | Title |
|---|---|
| `…-3tv` | Restore DTMF keypad support to the Python `lks` port (P0 feature) |
| `…-3tv.1` | Revert `c0d3e26` mutants before any DTMF work (P0) — see §6 |
| `…-3tv.2` | Decision: shared-room-only DTMF, or authorise PSTN plumbing (P0) |
| `…-3tv.4` | Foundation: publisher module, driver branch, room gate, dsl trigger (P0) |
| `…-3tv.4.1` | New module `caller_contract/dtmf.py`: `DtmfPublisher` seam and `RoomDtmfPublisher` (P0) |
| `…-3tv.4.2` | Add the dtmf branch to `ContractCallerDriver`, close the silent fall-through (P0) |
| `…-3tv.4.4` | Construct the publisher in `live_wiring` with a fail-fast room-identity gate (P0) |
| `…-3tv.6` | Verification: unit tests, the tripwire gate, a real negative check (P0) |
| `…-3tv.6.1` | Write `tests/test_contract_dtmf.py` covering all six behaviours (P0) |
| `…-3tv.6.2` | Add the template tripwire: spot-drive `dtmf-ivr-menu` through the real driver (P0) |

**Two collisions between this plan and the DTMF track — both resolved by landing the revert first:**

1. **`caller_contract/dsl.py`** — DTMF needs a new trigger kind; the revert touched `dsl.py:191`. Same file, same commit window. ✅ `eda4216`.
2. **`caller_contract/live_wiring.py`** — DTMF `3tv.4.4` constructs a publisher there; the revert touched lines 58, 324 and 537–542 there. Same file, and the mutant revert is DTMF's own `3tv.1` prerequisite. ✅ `eda4216`.

The revert landed first and on its own, as the plan called for. The DTMF track now rebases on it; this
plan's own later work in `live_wiring.py` (the attach seam, `--no-router` threading) came after,
so the two do not interleave.

---

## 7. Still open

**Resolved since the first draft** (research bead `v2-1`, findings in §1):

- ~~Provider guarantee asymmetry~~ → **the enum must be wrapped.** A bare
  top-level `{"type":"string","enum":[…]}` is invalid, so one shape serves
  both providers: `{"type":"object","properties":{"responseId":{…}},"required":["responseId"],"additionalProperties":false}`.
  `required` is not decoration — OpenAI strict mode requires it, and on
  Gemini its *absence* is what makes `{}` reachable, since every property is
  optional there by default.
- ~~`MAX_RESPONSES` ceiling~~ → **two ceilings, and the second binds first.**
  OpenAI publishes "up to 1000 enum values" *and* "total string length of
  all enum values cannot exceed 15,000 characters when there are more than
  250 enum values". A count-only check waves a 400-id catalog straight into a
  live 400, and that failure would be attributed to the agent under test.
  Both are enforced at parse in `responses.py`, with file:line.
- **Refusal is not a distinct status.** On OpenAI it arrives as HTTP **200**
  with `content[].type == "refusal"`. It maps to a harness fault — never to a
  fabricated `response_id`, which is the one outcome the catalog design
  exists to prevent.
- **Ranking, for the record:** OpenAI strict mode is the stronger guarantee,
  reasoned from both providers' docs and **not measured** — neither publishes
  a conformance rate.

**Still open:**

- **Latency headroom.** `timeout_ms: 1500` is asserted but not justified
  against measured GPT-4.1 nano / Gemini Flash Lite figures in this loop.
- **Jev as a third adapter.** Unbuilt by design, and deliberately *not*
  scaffolded: `provider: jev` parses, then raises a named error, because
  AGENTS.md forbids shipping surface nobody runs. It is a good future fit —
  its `Choice` primitive is a typed decision with no text generation, which is
  exactly this contract.
- **Turn alignment is still open.** `PROBLEMS.md` §1 (the `silence` trigger
  firing mid-turn because `active_speakers_changed` lags ~2.4 s) is a *separate*
  problem from the router, with its own parent and beads. A router does not
  fix a dropped utterance. Until that is addressed, a run can still lose a
  caller turn and the router will not know — and §1 research found the signal
  is a participant attribute (`lk.agent.state`), not an event, with the lag
  deliberately left unmeasured because measuring it needs a live duplex call.
- **`v2-27` — `lks init` must not scaffold a `responses:` template its own
  config cannot run.** OWNER DECISION. A scaffolded `responses:` scenario with
  no `router:` block raises `ConfigError` at run time, so adding the router
  block to `templates/config.yaml` would *fix* that — while shipping a key
  every new project pays for and does not use.

---

# Appendix A — Full merged implementation spec

> Verbatim synthesis output from the 20-agent workflow. Read the sections above
> first — this appendix is the supporting detail: per-decision evidence with
> `file:line` provenance, and the actual Python for each build step.
>
> ⚠️ **Amended after synthesis.** The text below is preserved unedited so the
> research provenance stays auditable. Two passages are superseded by
> **§3a Amendment 1** (owner, 2026-09-29):
> - **§2 D2** — "The routed text is authored ground truth, not a generation."
>   The text planner now runs on the router path; only the validator stays
>   bypassed. The carve-out itself is unchanged and now rests on a stronger
>   reason (the behavior verb is cosmetic on this path — see D3).
> - **§3 Step 5, "Byte-faithful publish"** (~line 916) — the byte-faithful
>   claim holds only for `text_planner.enabled: false`. With the planner on,
>   wording is a persona paraphrase and is not byte-replayable, which is why
>   D12 is now a hard requirement. `interaction_planner.plan_speak()` remains
>   bypassed either way.
>
> Everything else stands as written.
>
> ### ⚠️ Citation accuracy (re-checked 2026-09-29, after v2-9/10/11/20/21)
>
> The `file:line` references **in this appendix are from the synthesis agents and several no longer
> match the tree.** `driver.py` and `language_adapter.py` both shifted when the routed branch and
> the attach seam landed. Re-verified against the current source:
>
> | Cited | Actually at now | Status |
> |---|---|---|
> | `driver.py:620` `should_invoke_adapter` | 660 (call site), 75 (import) | ❌ was right before v2-9 |
> | `driver.py:919-922` `evaluate_behavior` | 1049 | ❌ |
> | `driver.py:946-959` `BEHAVIOR_TIMEOUT` | 1082 | ❌ |
> | `driver.py:1239` `_wait_trigger` | 1368 | ❌ |
> | `driver.py:1292-1310` silence branch | 1292+ | ⚠️ verify |
> | `driver.py:786-790` byte-faithful comment | 917 | ❌ |
> | `language_adapter.py:90` `_REQUIRED_CANDIDATE_KEYS` | 93 | ❌ |
> | `language_adapter.py:97` falsy check | 99 | ❌ |
> | `interaction_planner.py:84-85`, `:87` | as cited | ✅ |
> | `text_backends.py:33`, `:108`, `:155` | as cited | ✅ |
> | `dsl.py:191` (the injected mutant) | reverted; peer `eda4216` restored all **four** mutants | ✅ |
> | `live_wiring.py:58`, `:537` (mutants A and C) | reverted with the above | ✅ |
>
> The mutant citations are worth noting: the hand-off listed three, and there were **four** — a
> fourth in `live_wiring.py` had removed the `record_path`/`replay_path` mutual-exclusion guard.
> Reverting three would have left a path that ran both branches instead of raising.
>
> They are left in place so the synthesis's reasoning stays auditable. **Before implementing,
> open each cited line and confirm it** — the implementation beads carry corrected numbers where
> these were load-bearing, but the appendix is the reference an implementer will read first.

# Response Router — Merged Implementation Spec (livekit-agent-simulator)

Reconciles 4 research reports + 2 proposed designs + 10 adversarial verdicts. Every file:line below was re-verified against the working tree during this merge; corrections to the input research are marked **CORRECTION**.

---

## 0. The contract (fixed by owner — implemented verbatim, not reopened)

```
route(agent_transcript, responses) -> responseId
```

- Exactly one value: a `responseId` from the scenario's authored response catalog.
- No text generation, no reasoning, no agent/backend contact.
- Never returns a value outside the catalog.
- No `confidence`, no `null`, no `askForClarification`, no probability, no fallback branch.
- Unit of routing is a **response**, not a fact.
- `off_script` is a **normal catalog entry**, not a special code path. "Always a valid responseId" holds by construction.

```yaml
responses:
  company_name:
    intent: ask_company_name
    instruction: Provide the company name.
    text: "It's Bluebird Property Management."
  callback_number:
    intent: ask_callback_number
    instruction: Provide the callback phone number.
    reusable: true
    text: "You can reach me on 555-0142."
  off_script:
    system: true
    instruction: Select this only when the agent's question matches none of the other responses.
    text: "I'm sorry, could we stay focused on the property inquiry?"
```

```yaml
router:
  provider: openai          # openai | gemini
  model: ""                 # empty = per-provider default
  api_key: sk-...           # .agent-sim/ is gitignored; no api_key_env
  timeout_ms: 1500
  reasoning_effort: minimal
  temperature: 0
  prompt_version: v1
  unknown_policy: off_script
```

---

## 1. Verified repo state (provenance for every decision below)

Working directory: `C:\Users\ADMIN\Documents\Projects\livekit-agent-simulator`

| Fact | Value | Verified |
|---|---|---|
| HEAD | `d431686` ("docs: plan restoring DTMF keypad support") | ✅ |
| Parent HEAD | `c0d3e26` "test(mutation): inject A/B/C mutants in caller-contract" | ✅ |
| Working tree | `M AGENTS.md`, `?? PROBLEMS.md`, `?? wf-v3.js`. `git diff` empty. | ✅ |
| `.agent-sim/scenarios/` | **only** `smoke-hello.yaml`. No `_archive/`, never in git history. | ✅ |
| pydantic in repo | none | ✅ |
| Mutants | `live_wiring.py:58` `super().__init__(f"MUTANTA {reason} MUTANTB {detail}")`; `live_wiring.py:537-542` maps 5 of 6 `EndedBy` to `MUTANTC_*`; `dsl.py:191` `play_audio.bypasses_ai_and_validator` True→False | ✅ |
| `_SATISFIED_PATTERNS` | 9 substrings, checked **unscoped** at `orchestrator.py:262`; price/negotiate/arrange_visit branches above it *are* scoped | ✅ |
| Turn budget owner | `Orchestrator.check_max_turns` reads `contract.constraints.max_turns` (`orchestrator.py:375`) | ✅ |
| `run_spec.max_turns` | read only in `scenario.py:259` and `:721` — both inside `export_dict`. Never enforced in Python. | ✅ |
| `assert result is not None` | `live_wiring.py:522`; `driver.run` is try/**finally** (no except) | ✅ |
| `_CONTRACT_END_SIDES` | `script/models.py:168-174` — `contract_agent_end` → side `"agent"`, `contract_scenario_end`/`contract_caller_end` → `"sim"` | ✅ |
| `contract_summary` | `run_orchestrator.py:630` does **not** pass `recorder=`; `contract_summary.py:133` hardcodes `"validation": "passed"` | ✅ |
| `asserts.py` kind set | closed; broadest is `kind.startswith("transcript.")` at `:362`, and `transcript_contains` blobs include **both** agent and user finals | ✅ |
| `RECORD_FORMAT_VERSION` | `2`; `tests/test_parity_vectors.py:414` pins equality, `:416-419` pins `rejected_versions` | ✅ |
| `config_snapshot` parity | `lks-core/src/config.rs:6` — "key ORDER is a contract (dict-literal order) and never contains secrets" | ✅ |
| `[untranscribed agent speech]` | `agent_wait.py:39`, returned at `:175` (tier-3 floor); guarded at `driver.py:1203` | ✅ |
| `DEFAULT_BEHAVIOR_CATALOG` | 11 verbs: ask, confirm, deny, accept, reject, negotiate, clarify, provide, arrange_visit, interrupt, end. **No `respond`.** | ✅ |
| `AgentWait` tiers | tier 1 transcript final, tier 2 RemoteSession `chat_history` digest, tier 3 marker | ✅ |

**CORRECTION to research:** several verdicts said the mutants are "in the working tree" and remediation is `git diff`. They are **committed** at `c0d3e26`. `git revert` is also wrong because `d431686` sits on top and both commits touch nothing the router needs — use a **targeted manual revert** (see §3 Step 0).

---

## 2. Design decisions and the evidence behind them

### D1 — Insertion point: the `do:` retry loop, guard **inside** the `try`

Both input designs are internally contradictory: they cite `driver.py:683` (which is inside `_run_behavior`'s `do:` retry loop) but then describe publishing down the `say:` path. Verified code:

```
driver.py:681   for _ in range(self.max_retries + 1):
driver.py:682       attempts += 1
driver.py:683       try:
driver.py:684           candidate_attempt = _generate()
driver.py:685       except LanguageGenerationError as exc:
driver.py:686           transport_error = exc
driver.py:687           break
```

`transport_error` is the **only** thing `driver.py:735-742` reads. A guard placed *before* `:683` lets `RouterError` escape `driver.run` → trips `assert result is not None` at `live_wiring.py:522` → bare `AssertionError`, no `FailureReason`, no `EndedBy`, no `run.end_condition`. Both adversarial `correctness` lenses independently caught this. **The guard goes inside the `try`, and `break`s unconditionally.**

### D2 — Validator bypass is real but must be stated, and must not mean "no checks"

The routed text is authored ground truth, not a generation. Grading it through `ContractValidator` + `RuleBasedSemanticVerifier` would have the harness grade its own fixture, and would fail closed on `"I'm sorry, could we stay focused..."` at `_NO_MATCH_CONFIDENCE = 0.2 < 0.5` → `SEMANTIC_ACT_MISMATCH`/`LOW_CONFIDENCE` → `CALLER_BEHAVIOR_VIOLATION` — an agent bug reported as a caller bug. So the routed candidate gets a synthetic `ValidationResult(Verdict.VALID, reason="ROUTED")` and never reaches `validator.validate`. This is a permanent carve-out in a codebase whose stated invariant (`validator.py:1-19`) is "no other module may publish caller audio without a VALID verdict from here first." It **must** be commented at the cut site with that sentence, or the next reader "fixes" it.

### D3 — `evaluate_behavior` must be bypassed on the router path, and so must `BEHAVIOR_TIMEOUT`

`BehaviorEvaluator.evaluate` (`orchestrator.py:243-268`) falls through price/negotiate/arrange_visit (all scoped) to an **unscoped** `any(p in text for p in _SATISFIED_PATTERNS)` at `:262`. `"sure"`, `"sounds good"`, `"i can do"`, `"we can do"` are all natural agent acks mid-question. With the recommended one-`do:` scenario, the routed conversation dies after **one** decision and the repeat loop the feature exists to fix never runs. Two edits, both in `_run_behavior`:

1. Guard the `evaluate_behavior` call (`:919-922`) with `if self.router is None`.
2. Guard the `BEHAVIOR_TIMEOUT` fallthrough (`:946-959`) — on a routed behavior, budget exhaustion is a **clean** exit, not a caller violation. It returns normally → `end: true` → `EndedBy.SCENARIO` → `"contract_scenario_end"` → side `"sim"`.

Consequence: **the behavior verb is genuinely cosmetic on the router path**, so `caller_steps` reuses an existing verb (`provide`) and `dsl.py` does **not** need `respond` in `DEFAULT_BEHAVIOR_CATALOG`. That removes a file and a mutation-collision from the change set.

### D4 — Two verdicts only. The third verdict (`undetermined`) is REJECTED.

The two designs split on timeout policy; both half-answers are worse than the obvious one. Settling it:

- Returning the system id on a fault publishes `"I'm sorry, could we stay focused on the property inquiry?"` into a **live call** on a network blip. `asserts.py:362-368` builds `transcript_contains` blobs from both agent and user finals, so a `must_not_phrases` guard against the caller going off-script now **fails because the harness faulted**. A harness fault failing a test is the worst possible form of the inversion the design exists to kill.
- Hard-failing needs no new failure plumbing **once the guard is inside the `try`**, and lands as `FailureReason.LANGUAGE_GENERATION_ERROR` + `EndedBy.ERROR` + `run.end_condition`, which `suite.py:52-54` and `ended_by` asserts already understand.

**Therefore: exactly two verdicts, `matched` and `off_script`. Every `off_script` is a real model choice from a real enum.** Harness faults emit a **different event kind** (`contract.router_fault`) and fail the run; they never produce a verdict. Operational risk is managed by config, not by a fallback branch: `reasoning_effort: minimal`, `timeout_ms`, and one retry on transport/429/5xx.

### D5 — Two terminal conditions, and only one of them is a terminal

- **Constant router** (same `responseId` on 3 consecutive decisions) is a harness defect → raise `RouterTerminal` from `_route_candidate`; the driver catches it and returns `self._fail(FailureReason.LANGUAGE_GENERATION_ERROR, "...", EndedBy.ERROR, 0, turns)`. The run goes **red**, which is correct: a degenerate router must never be a green suite.
- **Repeated `off_script` is NOT terminal.** The owner contract says the simulator "responds as a reasonable caller would, and records the deviation so the judge can attribute it." Killing the call would be the simulator correcting the agent. It just keeps recording; the run ends green at budget with the count in `summary["caller_contract"]["router"]`.

### D6 — Never map a router condition onto `EndedBy.AGENT`

`_CONTRACT_END_SIDES["contract_agent_end"] = "agent"` (`script/models.py:173`) is consumed by `asserts.py:823-855` `_eval_ended_by_outcome`. Two shipped templates assert `ended_by: sim` (`templates/examples/people-pleaser-hangup-threat.yaml:96`, `templates/examples/hold-timeout-agent-stall.yaml:43`). Reusing `EndedBy.AGENT` for a harness fault both breaks those and lets an `ended_by: agent` assert **pass** on a broken router. With D4/D5 this is moot: the only router failure is a genuine `LANGUAGE_GENERATION_ERROR`/`ERROR`, and routed budget exhaustion is `SCENARIO`. **No new `FailureReason` member, no new `EndedBy` member, no `script/models.py` edit, no `edge14` test change.**

### D7 — The system entry is **authored, required**, and its id is reserved

Three independent inputs converge on this, and it is the single highest-leverage correctness decision:

- `AGENTS.md:33` forbids business strings in `src/`. A hardcoded `"...property inquiry?"` default spoken by a simulated caller in a banking scenario is a rule violation.
- Requiring it makes "always a valid responseId" a **parse-time** guarantee — the strongest form. The id is in the enum the model chooses from, and the author sees it in `lks export`.
- An authored `off_script:` without `system: true` currently gets its text silently replaced. And duplicate-intent checks run before injection, so an author with `intent: off_script` on another id produces two entries sharing the eval key.

So: `parse_responses` requires **exactly one** entry with `system: true`, requires `instruction` on every entry (non-system included — it is the only thing the model sees), requires `text` on every entry, rejects unknown keys naming the id, and rejects an id collision with the reserved `__off_script__`. No injection, no defaults, no export-round-trip problem. `OFF_SCRIPT_ID = "__off_script__"` (leading `_` is outside the scenario-id character class at `scenario.py:31`).

### D8 — `reusable` keeps the owner's meaning, and the enum shrinks — with the collapse handled in the arithmetic

`reusable` means "may be answered more than once." Non-reusable = once. Honored exactly. `options()` = `{rid : served < max_serves(rid)} ∪ {__off_script__}`; `max_serves` is `2 if reusable else 1`. This is the structural repeat-loop fix: a spent one-shot id is **not in the enum**, so the model physically cannot re-pick it.

**The collapse is real**: once all non-system entries are spent, `options == {__off_script__}` and every remaining turn is a forced `off_script` with no choice. That is a *catalog authoring* fact, not an agent deviation. Handled two ways:

1. **Arithmetic**: `forced: true` on the decision when `len(non_system_options) == 0`; forced decisions are excluded from **both** numerator and denominator, and counted separately as `catalog_exhausted_turns`.
2. **Parse-time authoring check**: warn (not fail) when `len(responses) < constraints.max_turns`, because the tail of the run will be forced. A warning is the right severity — the run still produces valid evidence.

**Schema caching reconciles this with OpenAI's per-schema decoder cost.** A changing schema pays a documented one-time compile; recomputing per turn would pay it every turn. So: `sig = sha1(sorted(options))`, `_schema_cache: dict[str, dict]`, and the body is rebuilt only on a new signature. Worst case is `N` compiles per run instead of `N` turns, amortized to ~0, and the repeat loop is still structurally impossible.

### D9 — `routeable(agent_text)`: a correct agent can never produce a false `off_script`

`AgentWait` returns three different shapes (`agent_wait.py:102-176`): tier-1 transcript final, tier-2 `RemoteSession.chat_history` digest, tier-3 the literal `"[untranscribed agent speech]"`. Feeding the marker or a session digest to the router and calling the result an agent deviation is the exact inversion. **Before routing, classify the input:**

- **routeable** — non-empty, not the marker, and drawn from tier 1.
- **unroutable** — marker, empty, or a tier-2 digest. On unroutable: **do not call the router, do not publish, do not emit a verdict.** Emit `contract.router_unroutable` (no `verdict` key) and let the existing `AGENT_TIMEOUT` path own the turn. Reuse the guard idiom at `driver.py:1203`.

Also: **turn 0 has `agent_text == ""`** (`driver.py:629`) because the agent wait is at the *bottom* of the loop (`:880`). Seed from `log` (which already holds the greeting from `driver.py:264`) — **router path only**, because changing `agent_latest` on the AI path would alter the existing `do:` prompt and break replay parity for already-recorded runs.

### D10 — `matched` means "the argmax was not the system id" — and the spec says so out loud

With confidence removed from the contract, a correct match and a near-miss paraphrase are **indistinguishable after the fact**. `matched` is not evidence of agent correctness, and a hallucinating agent one lexical step from an authored intent will produce `matched`. The design's own recommended scenario authors only two responses, so a correct agent asking about address or availability produces `off_script` and is counted as a deviation. Two mitigations, both in the spec:

- The system entry's `instruction` is a **selection rule**, not an action: *"Select this only when the agent's question matches none of the other responses."* Plus an explicit abstain clause in the system prompt. A model handed a list of N imperatives has a strong prior to return one of them.
- The `router_decision` event carries `agent_text` (truncated) **and `agent_text_sha`** (untruncated), so a human can audit any single `matched` and truncation can never hide a divergence.

`off_script_rate` is reported as `off_script / (matched + off_script)` with forced and unroutable excluded from both. The spec does **not** call it "the agent's deviation rate" without the control set (§9).

### D11 — `config_snapshot`: emit `router` only when configured

`lks-core/src/config.rs:6` declares key ORDER a parity contract and `tests/golden_config.rs` pins the list. Adding a 10th key from Python alone breaks it, and `"router": null` lands in `run.started` for every existing run. Conditional emission makes all 21 gated templates + 14 demo scenarios **byte-identical**, and the Rust port needs no edit (a router run is Python-only in v1; recorded as a deliberate divergence).

### D12 — `--record` / `--replay` is **forbidden** on router scenarios in v1

A routed turn never calls `recorder.record_attempt` (that is a separate call at `driver.py:700-706`), so a record holds zero attempts; replay builds `ReplayLanguageBackend` with an empty iterator and raises `LanguageGenerationError("replay divergence: ... record has 0 attempts")` on the first turn. The alternative — adding `prompt_version`/`model` to `RunRecord` — bumps `RECORD_FORMAT_VERSION` off the pinned `2` and breaks `tests/test_parity_vectors.py:414,416-419`. A `ConfigError` at wiring time is cheaper and honest. Replay support is a v3 with a fixture update.

### D13 — Parse + export land together; `caller_steps` stays required

`scenario_to_dict` (`scenario_yaml.py:118-193`) is the single source of truth for the YAML writer *and* `export_scenario` (MCP `ops.py:333-340`). Parse-without-export silently drops the catalog and the re-imported scenario runs with the router off — a materially different run with no error. Both land in step 4. `caller_steps` remains mandatory (`run_orchestrator.py:406-411`, `tests/test_contract_all_templates.py:45-53`), so no skeleton synthesis, no `run_orchestrator.py` mutation, no test relaxation. **Open question for the owner** in §10.

### D14 — Vendor neutrality

No `openai`/`google-genai` import outside `caller_contract/router_openai.py` and `router_gemini.py`. Both use raw `urllib` like `caller_contract/text_backends.py` (stdlib only, no SDK). `google-genai` is already a `pyproject.toml` dep for `evals/backends/gemini.py`; the router does **not** use it. The port is one method: `_call(options) -> tuple[str | None, int, int]`.

---

## 3. Build order (each step independently reviewable and revertable)

### Step 0 — Revert the committed mutants (blocking, before any live_wiring work)

`d431686` sits on top of `c0d3e26`, so `git revert` risks a conflict in `live_wiring.py` — the same file the router edits. Targeted manual revert, its own commit:

| File | Line | Restore |
|---|---|---|
| `caller_contract/live_wiring.py` | 58 | `super().__init__(detail)` (drop `f"MUTANTA {reason} MUTANTB "`) |
| `caller_contract/live_wiring.py` | 537-541 | `contract_caller_end`, `contract_agent_end`, `contract_timeout`, `contract_transport_error`, `contract_error` |
| `caller_contract/dsl.py` | 191 | `self.bypasses_ai_and_validator = True` |

Line 58 still raises `RuntimeError` with a garbled message — the exception *type* is intact; the destructive parts are the `MUTANTC_*` end_reason strings and the `dsl.py:191` flip, which is read at `driver.py:620` and hard-fails `play_audio` with `VALIDATION_ERROR`. **Nothing is measurable until this lands**, and every `end_reason` assertion written before it is measuring a mutant.

Then: `pytest -q` and confirm `tests/test_parity_vectors.py`, `tests/test_caller_edge_cases.py::test_edge14`, and the 21-template gate are green.

---

### Step 1 — `caller_contract/responses.py` (new, ~140 lines, zero integration)

```python
"""Authored caller responses — the catalog the router selects from.

A response is a *response*, not a fact: it may inform, refuse, confirm,
re-ask, agree, or end the call. The router never generates text and never
returns an id outside this catalog, so "always a valid responseId" is a
parse-time guarantee rather than a fallback branch.

Stdlib dataclass, matching every other model in dsl.py / scenario.py /
config.py. There is no pydantic in this package and none is being added.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Reserved id for the framework-owned system response. Leading underscore is
# outside the scenario-id character class (scenario.py:31) so it cannot
# collide with an authored id.
OFF_SCRIPT_ID = "__off_script__"

# Gemini rejects large enums with a mid-call InvalidArgument 400 and
# publishes no numeric limit of its own, so the ceiling is ours and it must
# fail as a scenario parse error naming file:line, not as a call-time 400
# attributed to the agent.
MAX_RESPONSES = 64

_ALLOWED_KEYS = frozenset({"intent", "instruction", "text", "system", "reusable"})


class ResponsesError(ValueError):
    """Malformed ``responses:`` block. The message names file, line and id."""


@dataclass(frozen=True)
class CatalogResponse:
    """One authored response.

    ``intent`` and ``instruction`` are the ONLY fields the router model ever
    sees; ``text`` is ground truth the simulator speaks and never enters the
    request in any form.
    """

    id: str
    text: str
    intent: str
    instruction: str
    system: bool = False
    reusable: bool = False

    @property
    def max_serves(self) -> int:
        """A non-reusable response leaves the choice set after one use.

        That is the whole of the repeat-loop fix: the model cannot
        re-pick a spent one-shot because the id is no longer in the enum.
        It is not a cap on how many times the entry may be served.
        """
        return 2 if self.reusable else 1

    def to_prompt_line(self) -> str:
        return f"- {self.id} ({self.intent}): {self.instruction}"


@dataclass
class ResponseCatalog:
    """Immutable entries plus per-run served state.

    ``_served`` is the only mutable field and it lives here — not in the
    driver, not in the router — so the port signature stays exactly
    ``route(agent_transcript, responses)``.
    """

    responses: dict[str, CatalogResponse]
    _served: dict[str, int] = field(default_factory=dict, repr=False)

    def __iter__(self): return iter(self.responses)
    def __len__(self): return len(self.responses)
    def __contains__(self, rid: object) -> bool: return rid in self.responses

    def get(self, rid: str) -> CatalogResponse:
        try:
            return self.responses[rid]
        except KeyError:
            raise ResponsesError(
                f"response id {rid!r} is not in the catalog ({sorted(self.responses)})"
            ) from None

    def system_id(self) -> str:
        return next(r.id for r in self.responses.values() if r.system)

    def options(self) -> frozenset[str]:
        """Ids the router may return this turn — a shrinking, never-empty set."""
        live = {
            rid for rid, r in self.responses.items()
            if self._served.get(rid, 0) < r.max_serves
        }
        return frozenset(live | {self.system_id()})

    def non_system_options(self, options: frozenset[str]) -> frozenset[str]:
        return frozenset(rid for rid in options if not self.responses[rid].system)

    def serve(self, rid: str) -> None:
        self._served[rid] = self._served.get(rid, 0) + 1

    def served_counts(self) -> dict[str, int]:
        return {rid: n for rid, n in self._served.items() if n}

    def render_for_model(self) -> str:
        return "\n".join(self.responses[rid].to_prompt_line() for rid in sorted(self.options()))

    def export(self) -> dict[str, dict[str, Any]]:
        """Round-trip shape. Served state is per-run and is dropped."""
        out: dict[str, dict[str, Any]] = {}
        for rid, r in self.responses.items():
            entry: dict[str, Any] = {"intent": r.intent, "instruction": r.instruction,
                                     "text": r.text}
            if r.system:
                entry["system"] = True
            if r.reusable:
                entry["reusable"] = True
            out[rid] = entry
        return out


def parse_responses(raw: Any, *, where: str, turn_budget: int | None = None) -> dict[str, CatalogResponse]:
    """Parse ``responses:`` — id -> {intent, instruction, text, system?, reusable?}.

    Unknown keys are a hard error naming the id, matching dsl.py:23 ("Unknown
    top-level keys are a hard parse error ... never a silent ignore"). A
    typo'd ``insruction:`` must not silently produce an entry the router can
    never match.
    """
    if not isinstance(raw, dict) or not raw:
        raise ResponsesError(f"{where}: responses: must be a non-empty mapping of id -> response")
    if len(raw) > MAX_RESPONSES:
        raise ResponsesError(
            f"{where}: responses: {len(raw)} entries exceeds MAX_RESPONSES={MAX_RESPONSES}"
        )
    out: dict[str, CatalogResponse] = {}
    for raw_id, body in raw.items():
        rid = str(raw_id).strip()
        if not rid:
            raise ResponsesError(f"{where}: response id must be non-empty")
        if rid == OFF_SCRIPT_ID:
            raise ResponsesError(
                f"{where}: {OFF_SCRIPT_ID!r} is reserved for the system response; pick another id"
            )
        if not isinstance(body, dict):
            raise ResponsesError(f"{where}: responses.{rid} must be a mapping")
        unknown = sorted(set(body) - _ALLOWED_KEYS)
        if unknown:
            raise ResponsesError(
                f"{where}: responses.{rid} unknown key(s) {unknown}; expected {sorted(_ALLOWED_KEYS)}"
            )
        missing = [k for k in ("intent", "instruction", "text") if not str(body.get(k) or "").strip()]
        if missing:
            raise ResponsesError(
                f"{where}: responses.{rid} missing required key(s) {missing} "
                f"— intent and instruction are the only things the router model sees, "
                f"and an empty instruction is an unmatchable entry"
            )
        out[rid] = CatalogResponse(
            id=rid,
            text=str(body["text"]).strip(),
            intent=str(body["intent"]).strip(),
            instruction=str(body["instruction"]).strip(),
            system=bool(body.get("system", False)),
            reusable=bool(body.get("reusable", False)),
        )
    system = [rid for rid, r in out.items() if r.system]
    if len(system) != 1:
        raise ResponsesError(
            f"{where}: responses: exactly one entry may set system: true, got {sorted(system)}. "
            f"The system entry is what makes 'always a valid responseId' hold by construction."
        )
    intents = [r.intent for r in out.values()]
    dupes = sorted({i for i in intents if intents.count(i) > 1})
    if dupes:
        raise ResponsesError(
            f"{where}: responses: duplicate intent {dupes} — intent is the eval/debug key "
            f"and must be unambiguous in the judge histogram"
        )
    if turn_budget is not None and len(out) - 1 < turn_budget:
        # WARN, not fail. The tail of the run will be forced off_script
        # because the non-system options are exhausted; that is recorded as
        # catalog_exhausted_turns, never as an agent deviation.
        print(
            f"[router] {where}: {len(out) - 1} non-system response(s) for a {turn_budget}-turn "
            f"budget — the final {turn_budget - (len(out) - 1)} turn(s) will be forced off_script",
            file=sys.stderr,
        )
    return out
```

**Tests — `tests/test_responses_parse.py`:** unknown key names the id; missing `instruction` on a non-system entry is a hard error; two `system: true` rejected; zero `system: true` rejected; `OFF_SCRIPT_ID` as an authored id rejected; duplicate intents rejected; `MAX_RESPONSES` enforced; `options()` shrinks by exactly the served non-reusable id and never drops the system id; `reusable: true` survives two serves; `export()` → `parse_responses()` round-trips byte-equal.

**Revertable:** yes — nothing imports it yet.

---

### Step 2 — `config.py` (~50 lines, additive, no read site)

Next to `JudgeConfig` (`config.py:193`, parsed at `:435`):

```python
RouterName = Literal["openai", "gemini"]
_ROUTER_VALUES = frozenset({"openai", "gemini"})
# One legal value in v1. Validated anyway so a typo fails at load rather
# than mid-call, and so the key cannot reintroduce a fallback branch the
# contract does not have. Never surfaced in config_snapshot.
_UNKNOWN_POLICIES = frozenset({"off_script"})


@dataclass
class RouterConfig:
    """Engine only. No scenario vocabulary, no business logic, no fallback."""

    provider: RouterName = "openai"
    model: str = ""                      # empty = per-provider default
    api_key: str = ""
    timeout_ms: int = 1_500
    reasoning_effort: str | None = "minimal"
    temperature: float = 0.0
    prompt_version: str = "v1"
    unknown_policy: str = "off_script"
    base_url: str | None = None

    @property
    def timeout_s(self) -> float:
        return self.timeout_ms / 1000.0
```

`SimConfig` gains `router: RouterConfig | None = None` at `config.py:193`. Parsed **after** `simulator` (`config.py:433`) so the `api_key` fallback resolves. Hard errors: provider not in `_ROUTER_VALUES`; `unknown_policy` not in `_UNKNOWN_POLICIES`; `timeout_ms` not in `200..60000`; `reasoning_effort` not in `{none,minimal,low,medium,high,xhigh,max}`; no `router.api_key` and no `simulator.api_key`.

`config_snapshot` (`config.py:562`) — **conditional**, per D11:

```python
    if cfg.router is not None:
        snap["router"] = {
            "provider": cfg.router.provider, "model": cfg.router.model,
            "api_key_set": bool(cfg.router.api_key), "timeout_ms": cfg.router.timeout_ms,
            "reasoning_effort": cfg.router.reasoning_effort,
            "temperature": cfg.router.temperature,
            "prompt_version": cfg.router.prompt_version,
            "base_url": cfg.router.base_url,
        }
```

`unknown_policy` is **omitted deliberately** — it is read by no code, and writing `off_script` into every report names it as a runtime policy, which is the special branch the contract forbids.

**Tests — `tests/test_router_config.py`:** each hard error; `api_key` falls back to `simulator.api_key`; `config_snapshot` contains no key material; `config_snapshot` is **byte-identical to today** when `cfg.router is None` (this is the D11 regression test).

---

### Step 3 — `caller_contract/router.py` + two adapters (new, ~230 lines, no driver)

```python
class RouterError(LanguageGenerationError):
    """Router call failed, timed out, was refused, or returned a value the
    provider did not guarantee was in the enum.

    Subclasses LanguageGenerationError so the driver maps it to
    FailureReason.LANGUAGE_GENERATION_ERROR / EndedBy.ERROR at driver.py:735-742.
    There is deliberately no fallback branch: returning the system entry on a
    fault would publish authored off-script text into a live call because the
    network blipped, and asserts.py:362 builds transcript_contains blobs from
    BOTH agent and user finals — so a must_not_phrases guard would fail the
    run for a harness fault. That is the exact attribution inversion this
    design exists to prevent.
    """


class RouterTerminal(RuntimeError):
    """The router is degenerate (same id 3 decisions running).

    A harness defect, not an agent one. The driver converts it into a
    LANGUAGE_GENERATION_ERROR so a broken router is a RED run, never a
    green one with off_script_rate 1.0.
    """


@dataclass(frozen=True)
class RouteDecision:
    """The ONE value the router returns, plus evidence the CALLER derives.

    ``response_id`` is the contract. Everything else is stamped from the
    catalog and the caller's own clock so nothing in this type can grow
    back into a second opinion.
    """
    response_id: str
    intent: str
    verdict: Literal["matched", "off_script"]
    forced: bool                 # options had no non-system entry left
    latency_ms: int
    attempts: int
```

Schema + prompt, both pure functions, both cached by signature (D8):

```python
def build_route_schema(options: frozenset[str], catalog: ResponseCatalog) -> dict:
    """Runtime-generated enum. Nothing hardcodes company|phone|address.

    Wrapped in an object because OpenAI forbids a bare root enum ("the root
    level object of a schema must be an object, and not use anyOf"). The
    per-value description is the instruction, so the discriminator is
    visible AT DECODE TIME, not only in the prompt body. `text` never enters
    the request in any form.
    """
    return {
        "type": "object",
        "properties": {
            "responseId": {
                "type": "string",
                "enum": sorted(options),
                "description": "; ".join(
                    f"{rid}={catalog.responses[rid].instruction}" for rid in sorted(options)
                ),
            }
        },
        "required": ["responseId"],
        "additionalProperties": False,
    }


_ROUTE_PROMPTS: dict[str, str] = {
    "v1": (
        "You route one turn of a simulated phone call. Given the agent's last "
        "utterance and a catalog of authored caller responses, return the ONE "
        "responseId the caller should speak now.\n"
        "Select exactly one. If none of the listed responses answers what the "
        "agent just asked, select the response marked as the fallback — do not "
        "stretch the closest match to cover it.\n"
        "Never invent an id and never write the words yourself. Respond with "
        "JSON only."
    ),
}
```

The choke point — one function, one invariant:

```python
def _resolve(raw: object, options: frozenset[str], *, system_id: str) -> str:
    """The single place 'always a valid responseId' is enforced.

    Membership is tested against the per-turn option snapshot the enum was
    built from, never against the catalog: a spent-but-reusable id is in the
    catalog and illegal this turn, and accepting it is the wrong-route-that-
    reads-as-PASS failure this design exists to prevent. The raw value is
    never coerced, stripped, lowercased or stringified.
    """
    if system_id not in options:
        raise RouterError(f"system id {system_id!r} missing from options {sorted(options)}")
    if type(raw) is not str or raw not in options:
        raise RouterError(f"router returned {raw!r}, not in this turn's options {sorted(options)}")
    return raw
```

Mandatory, not defence-in-depth: **Gemini silently ignores unsupported schema keywords** where OpenAI rejects the request at the gateway, so on the Gemini path this check is the only gate on a schema-builder bug.

Retry policy, shared: **one** retry on transport errors and HTTP 429/5xx; **never** on 400 (a 400 against a runtime-generated schema is a builder bug, not a flake). Do **not** copy the status-blind `except Exception: continue` at `language_adapter.py:141-143`. Do **not** port `evals/runner.py:56-140` `_repair_truncated_json` — under `json_schema` strict it would mask a genuine contract violation.

`build_router(cfg)` mirrors `callers/factory.py:26-81`: one `if/elif`, lazy imports in the branches, `ConfigError` on an unknown provider as a backstop.

**Adapters — `router_openai.py`** (`POST {base}/chat/completions`, `response_format={"type":"json_schema","json_schema":{"name":"route","strict":True,"schema":…}}`, body carries `reasoning: {"effort": …}` when configured): handles `message.refusal` (content is `null` and does **not** follow the schema), `status == "incomplete"` with `incomplete_details.reason == "max_output_tokens"`, `choices == []`.

**Adapters — `router_gemini.py`** (`POST {base}/models/{model}:generateContent?key=…`, `generationConfig.responseMimeType="application/json"` + `responseSchema=<the same dict>`): handles `candidates == []`, empty `parts`, and the `{}` case that Gemini emits when `required` is omitted (all Gemini properties are optional by default — the `required` key in `build_route_schema` is the only thing preventing it). **Do not use `text/x.enum`** even though Gemini alone supports a bare root enum: OpenAI cannot, so it forks the parse path and the record shape for zero benefit. Model default is the alias, not a version pin — `text_backends.py:155-157` records that `gemini-2.0-flash` 404s on the working key.

**19-path failure table → all 19 raise `RouterError`, none produce a verdict.** Table-driven test with one fake backend per row: (1) id in catalog but spent; (2) id absent; (3) non-str value; (4) case/whitespace variant; (5) OpenAI refusal; (6) `incomplete`/`max_output_tokens`; (7) `choices == []`; (8) `candidates == []`; (9) Gemini `{}`; (10) bare-string enum body; (11) truncated/fenced content; (12) HTTP 400; (13) HTTP 401/403; (14) HTTP 429/5xx after one retry; (15) URLError/timeout/reset; (16) HTML proxy body; (17) Jev-style full-catalog distribution whose client argmax lands on a spent id; (18) reserved-id collision at parse; (19) system id missing from options.

**Revertable:** yes — testable against a mocked `urlopen` with no scenario, room, or driver.

---

### Step 4 — scenario parse + export (4 files, ~45 lines, lands together per D13)

- `scenario.py`: `responses: dict[str, Any] = field(default_factory=dict)` after `caller_actions` (`:185`); `"Responses"` in `KNOWN_KINDS` (`:32-46`); one branch beside the `CallerSteps` branch (`:511-520`) wrapping `ResponsesError` in `ScenarioError`. Add `Scenario.uses_router() -> bool`.
- `scenario_from_dict.py`: parse `responses:` beside `caller_steps` (`:134-144`), pass into the `Scenario(...)` construction (`:168`).
- `scenario_yaml.py`: `if scenario.responses: data["responses"] = {rid: r.export()…}` beside the `unparse_steps` block (`:161-164`).
- `tests/test_scenario_yaml_edgecases.py`: add `assert (a.responses or {}) == (b.responses or {})` to `_assert_equal` (`:38-73`) so `test_template_yaml_roundtrip` (`:81`) actually covers the new key. Model the new round-trip test on `test_template_caller_steps_roundtrip` (`:462`).

No injection means no export-idempotency problem: `export()` returns exactly what was authored.

**Revertable:** yes — a scenario can then carry `responses:` with nothing reading it; every run behaves identically.

---

### Step 5 — `driver.py` + `live_wiring.py` (the integration, ~60 lines)

**New dataclass fields after `recorder` (`driver.py:199`):**

```python
    # Response router. Both are None on every existing run, so the positional
    # walk is byte-identical.
    router: ResponseRouter | None = None
    responses: ResponseCatalog | None = None
    # Agent transcript the router matched against this turn, and whether it is
    # a real tier-1 transcript final. Seeded from `log` on turn 0 on the router
    # path ONLY — changing agent_latest on the AI path would alter the existing
    # do: prompt and break replay parity for runs already recorded.
    _agent_transcript: str | None = field(default=None, repr=False)
    _agent_transcript_routeable: bool = field(default=False, repr=False)
```

**Turn-0 seeding, `driver.py:648-654`:**

```python
            agent_latest = agent_text or None
            if self.router is not None and agent_latest is None:
                for t in reversed(log):
                    if t.speaker == "agent" and t.text:
                        agent_latest = t.text
                        break
            self._agent_transcript = agent_latest
            self._agent_transcript_routeable = _is_routeable(agent_latest)
```

with

```python
_UNTRANSCRIBED_MARKER = "[untranscribed agent speech]"   # agent_wait.py:39


def _is_routeable(text: str | None) -> bool:
    """True only for a real tier-1 transcript final.

    AgentWait returns three shapes: the transcript final, a RemoteSession
    chat_history digest, and the tier-3 marker when the agent provably spoke
    but nothing transcribed. Routing on the marker or a session digest would
    record a transcript loss as an agent deviation — the exact inversion this
    feature exists to prevent. Same guard idiom as driver.py:1203.
    """
    return bool(text and text.strip() and text.strip() != _UNTRANSCRIBED_MARKER)
```

**The guard — `driver.py:681-687`, inside the `try`, with an unconditional `break`:**

```python
            for _ in range(self.max_retries + 1):
                attempts += 1
                try:
                    if self.router is not None:
                        # Routed turn. The router returns a responseId, never
                        # text; the text it selects is authored ground truth,
                        # so ContractValidator is bypassed BY CONSTRUCTION —
                        # grading the catalog would be the harness grading its
                        # own fixture. Do NOT "fix" this by routing through the
                        # validator: RuleBasedSemanticVerifier scores catalog
                        # text at _NO_MATCH_CONFIDENCE=0.2 < 0.5 and it fails
                        # closed as SEMANTIC_ACT_MISMATCH, which is an agent
                        # bug reported as a caller bug.
                        candidate_attempt = await asyncio.to_thread(
                            self._route_candidate, contract, _emit, agent_latest
                        )
                        last_verdict = ValidationResult(
                            verdict=Verdict.VALID, reason="ROUTED"
                        )
                        _attempt_verdicts.append(
                            ("VALID", "ROUTED", candidate_attempt.utterance)
                        )
                        candidates.append(candidate_attempt)
                        break   # one router call per turn: never re-enter
                    candidate_attempt = _generate()
                except LanguageGenerationError as exc:
                    transport_error = exc
                    break
                ...
```

The `break` is load-bearing. Without it the loop re-enters, `serve()` double-charges `max_serves`, a second HTTP round trip burns the 1500 ms budget twice, and the stale-drop `continue` at `:775-782` re-enters the whole guard — double-counting `decisions` and burning one-shot entries that were never spoken.

**`_route_candidate`:**

```python
    async def _route_candidate(
        self, contract: BehaviorContract, _emit: Any, agent_latest: str | None
    ) -> CandidateUtterance:
        assert self.router is not None and self.responses is not None
        if not self._agent_transcript_routeable:
            # Nothing to route against. Do not call the router, do not publish
            # catalog text chosen from a marker, do not emit a verdict. The
            # existing AGENT_TIMEOUT path below owns this turn.
            _emit("contract.router_unroutable", {
                "behavior": contract.behavior, "turn": turns,
                "reason": "empty" if not agent_latest else "untranscribed",
            })
            return CandidateUtterance(
                act=contract.behavior, target=None, slots={"routed": False},
                utterance="", identity=self.orchestrator.new_generation(),
            )
        try:
            decision = await asyncio.to_thread(
                self.router.route,
                agent_transcript=agent_latest, responses=self.responses,
            )
        except RouterTerminal as exc:
            return self._fail(FailureReason.LANGUAGE_GENERATION_ERROR,
                              f"router degenerate: {exc}", EndedBy.ERROR, 0, turns)
        response = self.responses.get(decision.response_id)
        self.responses.serve(decision.response_id)
        _emit("contract.router_decision", { ... })     # §6 spec
        return CandidateUtterance(
            act=contract.behavior, target=None,
            slots={"routed": True, "response_id": response.id, "intent": response.intent},
            utterance=response.text,
            identity=self.orchestrator.new_generation(),
        )
```

`asyncio.to_thread` is required: the port is sync (unit-testable with no event loop) and a blocking `urlopen` inside the LiveKit room's event loop stalls the observer callbacks the driver itself polls.

**Evaluator bypass, `driver.py:919-922`:**

```python
            if self.router is None:
                verdict = self.orchestrator.evaluate_behavior(contract, agent_text)
                if verdict == EvaluatorVerdict.SATISFIED:
                    return turns, agent_text
```

**Budget-exhaustion bypass, `driver.py:946-959`:**

```python
        if self.router is not None:
            # Budget exhaustion on a routed behavior is a clean end, not a
            # caller violation: the caller never misbehaved, the catalog or
            # the turn budget simply ran out. Returns normally so `end: true`
            # produces EndedBy.SCENARIO -> "contract_scenario_end" -> side
            # "sim", which is what a caller_steps-driven run already reports.
            return turns, agent_text
        _emit("contract.behavior_violation",
              {"behavior": contract.behavior, "reason": "FAILED_MAX_TURNS"})
        return self._fail(FailureReason.BEHAVIOR_TIMEOUT, ...)
```

**Byte-faithful publish.** In the publish block (`driver.py:786-830`), when `self.router is not None`, skip `self.planner.plan_speak(...)` entirely and synthesize `candidate.utterance` verbatim — the same carve-out the `say:` branch already documents at `driver.py:362-368` ("planner tokens are computed but DISCARDED"). `plan_speak` inserts hesitation/stumble tokens when the step sets `interaction`, and a routed step is not required to omit it. `identity` comes from `self.orchestrator.new_generation()` and is re-checked with `orchestrator.is_stale()` immediately before `sink.publish`, preserving invariant 2.

**`live_wiring.py` (~20 lines):**

```python
def _build_router(cfg: Any) -> Any:
    if getattr(cfg, "router", None) is None:
        return None
    from .router import build_router
    return build_router(cfg=cfg)
```

At the driver construction (`:347-352`):

```python
    if scenario.responses and cfg.router is None:
        raise ConfigError(
            f"scenario {scenario.id!r} authors `responses:` but .agent-sim/config.yaml "
            f"has no `router:` block — the router is what turns a responseId into speech"
        )
    if record_path is not None and scenario.responses:
        raise ConfigError(
            "--record is not supported for router scenarios in v1: a routed turn is not "
            "a generation, so the record would hold zero attempts and replay would "
            "diverge on the first turn"
        )
    catalog = ResponseCatalog.build(scenario.responses) if scenario.responses else None
    driver = ContractCallerDriver(
        orchestrator=orch, validator=validator, adapter=adapter,
        synthesize=_synthesize, recorder=recorder,
        router=_build_router(cfg) if catalog is not None else None,
        responses=catalog,
    )
```

Both conditions required (`catalog is not None` **and** a configured router) — one-sided wiring turns every existing scenario into a routed run against a one-entry catalog the moment a `router:` block appears in the gitignored config.

**Tests — `tests/test_router_driver.py`:** fake `ResponseRouter` + 2-entry catalog + `FakeAgentWait` driving five successive agent questions through one routed `do:`; assert (a) **five** `contract.router_decision` events, (b) published text equals catalog `text` byte-for-byte, (c) `validator.validate` called **zero** times, (d) an agent reply containing `"sure"` does **not** end the behavior, (e) an untranscribed marker produces `contract.router_unroutable` and **no** `off_script`, (f) a faulting backend yields `LANGUAGE_GENERATION_ERROR` + `EndedBy.ERROR`, (g) a constant router yields a **red** run, (h) a routed run with no config raises `ConfigError`, (i) `--record` + `responses:` raises `ConfigError`.

**Revertable:** yes, and inert if reverted — a parsed-but-unused `responses:` block changes nothing.

---

### Step 6 — evidence (2 files, ~60 lines)

`contract.router_decision` — one per decision, `source="sim.contract"`, `include_dialogue=False`:

```json
{"turn": 3, "behavior": "provide", "offered": ["__off_script__", "company_name"],
 "non_system_offered": ["company_name"], "forced": false,
 "agent_text": "Can I get a callback number?", "agent_text_sha": "…",
 "response_id": "callback_number", "intent": "ask_callback_number",
 "verdict": "matched", "served": 2, "max_serves": 2,
 "provider": "openai", "model": "", "prompt_version": "v1",
 "latency_ms": 412, "attempts": 1}
```

`contract.router_fault` — a **distinct kind**, not the same kind with the key absent. Absence-of-a-key is a convention some reader will eventually get wrong; presence-of-a-different-kind is a type error.

```json
{"turn": 3, "fault": "timeout", "error": "…", "http_status": null,
 "provider": "openai", "model": "", "prompt_version": "v1", "attempts": 2}
```

A fault **never** produces a verdict. The aggregator filters on `kind == "contract.router_decision"`, so a fault is structurally incapable of entering any routing statistic.

`contract.router_unroutable` — `{"turn", "reason": "empty"|"untranscribed"}`. No `response_id`, no `verdict`.

`contract_summary.py`: add `contract.router_decision` to the kind filter (`:82-98`), put `response_id`/`intent`/`verdict`/`forced` on each `turns_detail` row, and add:

```python
"router": {
  "provider":…, "model":…, "prompt_version":…,
  "decisions": [{turn, agent_text_sha, response_id, intent, verdict, forced}],
  "counts": {"decisions": 5, "matched": 3, "off_script": 1,
             "forced": 1, "unroutable": 0, "faults": 0},
  "off_script_rate": 0.25,          # off_script / (matched + off_script)
  "catalog_exhausted_turns": 1,
  "by_response": {"company_name": 1, "callback_number": 2, "__off_script__": 2},
  "terminal": null,
}
```

`off_script_rate` excludes `forced` and `unroutable` from **both** numerator and denominator. That is what makes it mean "the fraction of turns where the agent asked something no authored response covers" rather than "the fraction of turns where the harness produced a result." A router that always returns `__off_script__` is then visibly 100%, not 0% error.

Also change `contract_summary.py:133` from the hardcoded `"validation": "passed"` to `"routed"` when the turn carried `slots.routed`, so a routed turn is distinguishable from a validated AI turn in the report and in `web/cues.py:114-123`.

`run_orchestrator.py:630-644`: pass `recorder=recorder` (the parameter exists at `contract_summary.py:70` and no production caller supplies it) **only** inside the same `try/except` that already swallows summary failures.

`event_writer.py`: **two** `_describe` cases, beside the existing `contract.*` ones (`:519-527`). The generic fallthrough key list at `:527` is `("name","identity","topic","status","room","node_id","reason","behavior","verdict")` — the decision spec would render as a bare `verdict=matched` with no id, and the **fault** spec matches none of those keys and renders as an empty cell. The one event kind that must be impossible to miss is the one that renders blank.

**Judge channel (deferred, guarded).** Thread `router=` as an optional keyword defaulting to `None` through `evals/evidence.py:77-86` → `evals/runner.py:195-315`, mirroring the existing `build_assert_digest` seam, and merge at `run_orchestrator.py:665` **only** when the block is present. The judge receives the aggregate, never the raw events, so there is one source of truth for the rate. Do **not** put the router block under `assert_verify` — that is the hard gate that flips `status="failed"` (`run_orchestrator.py:598-602`) and is read by `suite.py:52-54`; every agent deviation would become a red CI build, the exact false signal being eliminated.

**Revertable:** yes — the events are already in `events.jsonl` without any of it.

---

### Step 7 (separate commit) — `http_json.post_json` extraction

`text_backends.py:117` hardcodes `json_object`, which carries **no schema** (keys are string-matched at `:94-111`) and structurally cannot express enum membership. Extract the transport; keep the envelopes separate. Bodies are byte-identical today and `tests/test_text_backends.py` (mocked `urlopen`) is the net. Its own commit, **after** the router ships, so the record/replay diff stays readable.

---

## 4. Rejected mustFixs

| Rejected | Why |
|---|---|
| **Third verdict `undetermined` + fall back to the system id on fault** | Publishing authored off-script text on a network blip corrupts the live transcript, and `asserts.py:362` matches **both** agent and user finals — so a `must_not_phrases` guard would fail the run *because the harness faulted*. Hard-fail needs no new plumbing once the guard sits inside the `try` (D1, D4). |
| **`attributable: false` as a runtime summary field gating the judge and termination** | Superseded: with hard-fail on fault and forced decisions excluded, the arithmetic is trustworthy without a cache-validity flag. The calibration lesson survives as §9 (a **test** gate on the prompt), not a runtime field. A boolean nothing reads is worse than no boolean. |
| **Widening the attribution cache key to `(model, prompt_version, reasoning_effort, temperature, base_url, prompt_sha)`** | Follows from rejecting the cache. The same tuple is instead asserted on the control-set **fixture** in CI. |
| **Pydantic `responses_schema.py` as a schema of record** | Zero pydantic in the package (`pyproject.toml:7-17`). A second schema definition a test must keep in sync with the dataclass is net negative for five fields, and a new optional extra for the CLI is a project-wide decision, not a router side effect. |
| **Ship a `jev` adapter in v1** | TypeSafe's documented endpoint is `POST /v1/systemone` with `{state, model, questions}` — **not** the OpenAI `/chat/completions` + `json_schema` envelope. Shipping that is shipping fiction. `router.provider: jev` raises `ConfigError` until `router_jev.py` lands against a verified spec. The port makes it an adapter swap, which is the contract's actual promise. |
| **Skeleton synthesis so `responses:`-only scenarios are legal** | `tests/test_contract_all_templates.py:45-53` asserts `caller_actions` non-empty at **parse** time; synthesis at `run_orchestrator.py:406` happens after and does not unblock it. Requiring `caller_steps` keeps `run_orchestrator.py` untouched. Owner question in §10. |
| **`legality: "in_options" \| "rejected"` as a persisted field** | It is either a success (nothing persists) or a `RouterError` (the run is red). The only value that changes arithmetic is `forced` (options had no non-system entry), which is persisted. Fewer fields, same information. |
| **`respond` added to `DEFAULT_BEHAVIOR_CATALOG`** | Unnecessary: the evaluator is bypassed on the router path (D3), so the verb is cosmetic and `provide` works. Avoids a `dsl.py` edit that would collide with the mutant revert in Step 0. |
| **`contract.router_error` with no `verdict` key, same kind as the decision** | A distinct kind is a type error; an absent key is a convention. `contract.router_fault`. |
| **A router escalation ladder** | Constrained decoding already fixes format, so the ladder has no format failure mode left to address, and it would violate the one-function contract for a benefit only our own frozen dataset could demonstrate. |
| **Adding `prompt_version`/`model` to `RunRecord` in v1** | Bumps `RECORD_FORMAT_VERSION` off the pinned `2` and breaks `tests/test_parity_vectors.py:414,416-419`. `--record` is forbidden on router scenarios instead (D12). v3, with a fixture update. |
| **`Promptfoo` / `Braintrust` / `LangSmith` tooling** | The three-way comparison is not a v1 deliverable. A 12-case JSON fixture and one pytest file get 80% of the value at zero dependency cost. |

**Accepted mustFixs, in full:** turn-0 log seeding; the guard inside the `try`; unconditional `break`; `routeable()` marker gate; evaluator bypass; `BEHAVIOR_TIMEOUT` bypass on the router path; `RouterTerminal` → red run; `forced` exclusion from the arithmetic; no `EndedBy.AGENT` reuse; conditional `config_snapshot`; parse+export together + `_assert_equal`; `Recorder` untouched + explicit `--record` ban; two `_describe` cases; `contract_summary` router key in the same try/except; `validator.validate` call-count assertion; no business strings in `src/`; reserved-id collision check; duplicate-intent check after the system entry is known; `reasoning_effort` in config; `max_serves`/collapse handling; no off_script terminal; parse-time `MAX_RESPONSES`; parse-time turn-budget warning; `agent_text_sha`; served-counts in the event.

---

## 5. Vendor neutrality

| Rule | Enforcement |
|---|---|
| No `openai` / `google-genai` import outside an adapter | `router_openai.py`, `router_gemini.py` only. Both use `urllib.request`, matching `caller_contract/text_backends.py:1-18` ("stdlib only, so no extra HTTP client dependency"). `google-genai` stays where it is (`evals/backends/gemini.py:5-6`). |
| One provider method | `ResponseRouter.route(...)` is the port; `_StructuredRouterBase` subclasses implement `_call(options) -> tuple[str \| None, int, int]` and `_headers()`. Prompt assembly, option-set computation, the choke point, verdict derivation, constant-router detection and retry policy are shared and never reimplemented per provider. |
| Core is repo-agnostic | No consumer-specific vocabulary in `src/`. `instruction` is scenario data; `AGENTS.md:33` is honoured (no default `off_script` text). |
| No silent unknown keys | `parse_responses` rejects them naming `file:line:id`, per `dsl.py:23`. |

---

## 6. Default-off and blast radius

- `SimConfig.router` defaults `None`; `Scenario.responses` defaults `{}`.
- Both new driver fields default `None`; the guard is `if self.router is not None` **and** the wiring requires a non-empty catalog **and** a configured router.
- `config_snapshot` is **conditionally** extended → `run.started` is byte-identical for every existing run.
- No existing scenario or template carries `responses:`. `.agent-sim/scenarios/` contains exactly one file, `smoke-hello.yaml`, with no `responses:` block. **CORRECTION to the brief's premise: there is no `_archive/` and there never was** — the "7 archived scenarios" do not exist. The real gated surface is 21 templates (`templates/*.yaml` + `templates/examples/*.yaml`, minus `config.yaml` and `scenario-scaffold.yaml`) enforced by `test_every_template_has_caller_steps`, plus 14 demo scenarios. None of them change in steps 1-5.
- Extend `tests/test_contract_all_templates.py` with the inverse invariant: a template authoring `responses:` must have a `router:` block in the CI config, and a template with neither must construct `driver.router is None`.

---

## 7. Rust twin (deliberate divergence, recorded)

`lks-core/src/scenario.rs` `KNOWN_KINDS` is a fixed array and `scenario_jsonl.rs` hard-rejects unknown kinds, so a JSONL scenario with a `Responses` section parses in Python and is rejected by `lksr`. `tests/golden/` is empty, so nothing catches it today. v1 is **Python-only for router scenarios**; the exclusion is stated in the plan and in `AGENTS.md` rather than silently carried. Revisit if `lksr` ever needs to execute a router run.

---

## 8. PROVEN vs UNVERIFIED

**PROVEN (verified in the tree during this merge)**
- Mutants are committed at `c0d3e26` and still present at `live_wiring.py:58,537-542` and `dsl.py:191`; `d431686` is HEAD.
- `.agent-sim/scenarios/` has one file; `_archive/` never existed.
- `_SATISFIED_PATTERNS` is checked unscoped at `orchestrator.py:262`; the three branches above it are scoped. **This alone collapses a routed `do:` to one turn.**
- `check_max_turns` reads `contract.constraints.max_turns`; `run_spec.max_turns` is `export_dict`-only in Python.
- `transport_error` is set only inside the `try` at `driver.py:685-687`; `driver.run` is try/**finally**; `live_wiring.py:522` asserts on the result.
- `_CONTRACT_END_SIDES["contract_agent_end"] == "agent"` is consumed by `asserts.py:823-855`; two shipped templates assert `ended_by: sim`.
- `contract_summary.py:133` hardcodes `"validation": "passed"`; `run_orchestrator.py:630` passes no `recorder`.
- `asserts.py` matches a closed kind set; `transcript_contains` spans both speakers.
- `RECORD_FORMAT_VERSION == 2`, pinned by `tests/test_parity_vectors.py:414,416-419`.
- `config_snapshot` key ORDER is a documented Rust parity contract (`lks-core/src/config.rs:6,926`).
- `[untranscribed agent speech]` is `agent_wait.py:39`, returned at `:175`, guarded at `driver.py:1203`.
- `DEFAULT_BEHAVIOR_CATALOG` has 11 verbs, no `respond`.
- `gemini-2.0-flash` 404s on the working key (`text_backends.py:155-157`).
- No pydantic anywhere in the package.

**UNVERIFIED — do not treat as fact; each is an assumption the smoke test must falsify**
- **`gpt-4.1-nano` deprecation / 2026-10-23 shutdown.** Research cites `developers.openai.com/api/docs/deprecations`; I did not fetch it. Mitigation regardless: `model: ""` = per-provider default, and **pin a dated snapshot, never an alias**, so a moving alias cannot silently change results under one `prompt_version`.
- **`reasoning_effort: minimal` is required for a 1500 ms budget.** The 0.77 s-vs-32.4 s figures are third-party (Artificial Analysis). Directionally important, not measured here.
- **All latency and cost figures.** No provider publishes p50/p99 for any of these tiers; two independent labs disagree ~2× on the same model. `timeout_ms: 1500` has roughly the width of the measurement error as headroom.
- **Jev's wire format.** Unverified; the adapter is deferred, not guessed.
- **Whether the enum-constraint guarantees actually hold at 4.1-nano-class models.** Structured Outputs requires `gpt-4o-mini` / `gpt-4o-2024-08-06` or later; probe once at router construction rather than assume.
- **Gemini's enum ceiling.** No published number; `MAX_RESPONSES = 64` is our choice, not Google's.
- **The working tree may already be moving** — `M AGENTS.md`, `?? PROBLEMS.md`, `?? wf-v3.js` at merge time.

---

## 9. The first smoke test

`tests/fixtures/router_control_set.json` (checked in, 12+ labeled `(agent_transcript, expected_response_id)` pairs, real-transcript-shaped, ≥4 labeled `__off_script__` so an always-off_script router caps near 33%) + `tests/test_router_control_set.py`. **Lands at step 4, before the driver wiring** — the router is measured as a pure function with no way to publish anything.

A positive accuracy number cannot distinguish a working router from a lucky one, so the file is three negative controls plus the score:

1. **SCORE** — accuracy `== 1.0` over the labeled set, `off_script` count `==` exactly the labeled-system count, and **zero** faults. Any fault on a warm real call means a path the author believed unreachable is being hit.
2. **NEGATIVE A — instruction swap** *(the most important assertion in the file)* — exchange `company_name.instruction` and `callback_number.instruction`, re-run, assert the two routes **swap**. If they do not, the router is matching the id string or enum position, and every other number in the suite is meaningless.
3. **NEGATIVE B — order shuffle** — reverse the catalog's insertion order (Python dicts preserve it, and the enum order follows it) and assert accuracy is **unchanged**. If it moves, the router is positional.
4. **NEGATIVE C — degenerate backend** — stub the backend to always return `options[0]`; assert decision 3 is `RouterTerminal` and the run is **red** with `LANGUAGE_GENERATION_ERROR`, not `EndedBy.AGENT` and not green.
5. **SHRINK** — assert `len(options)` strictly decreases across turns, a spent id is absent from turn 2's enum **and** from the request body on the wire, and the signature cache rebuilds the schema exactly once per distinct set.
6. **CHOKE POINT** — table-drive all 19 paths from Step 3; each raises `RouterError`, none returns a value, none produces a verdict.
7. **BYTE-FAITHFUL** — fake agent + fake sink: published text `==` catalog `text` byte-for-byte, `validator.validate` called **zero** times, an agent reply containing `"sure"` does **not** end the routed behavior, five questions produce five `contract.router_decision` events.
8. **ATTRIBUTION, both directions** —
   - no false accusation: an untranscribed marker and a tier-2 session digest each produce `contract.router_unroutable` and **zero** `off_script` verdicts; a faulting backend produces `contract.router_fault` with `counts.off_script == 0`;
   - no missed deviation: a forced turn (`non_system_offered == []`) is counted in `catalog_exhausted_turns` and is absent from **both** sides of `off_script_rate`'s arithmetic.
9. **CHRONOLOGY** — parse-only exports the catalog; export → re-parse → export is byte-identical; no injected entry appears.

Re-run the control set on **any** change to `model`, `prompt_version`, `reasoning_effort`, or any `instruction`. A drop blocks the merge, and the comparison is per-case diffs against the baseline, not absolute scores — 0.87 vs 0.89 on 12 cases is noise. **12 cases is enough to catch a positional router and an always-off_script router; it is not enough for a trustworthy `off_script_rate`.** If that number is ever going to gate a decision rather than inform a human, size the set to 50-200 first.

---

## 10. Open questions for the owner

1. **Is `--record` on router scenarios acceptable as forbidden in v1?** The alternative is a v3 `RunRecord` bump with a `tests/test_parity_vectors.py` fixture update. v1 forbids; that is the honest default.
2. **Should `responses:` alone be a legal scenario?** v1 requires `caller_steps` alongside it (one `do:` + `end: true`, `behavior: provide`). Making `responses:`-only legal needs skeleton synthesis in `run_orchestrator.py:406` **and** relaxing `tests/test_contract_all_templates.py:45-53` — two files, two decisions.
3. **Is `off_script_rate` allowed to inform a decision at 12 control cases?** If not, size the set now rather than after the first bad call.
4. **Should `router.model` default to a specific dated snapshot, or stay `""` = per-provider default?** `""` is safer against a deprecation the plan could not verify; a pin is safer against alias drift.
5. **Is `responses:` the right block name?** `caller_steps:` is the WHEN skeleton, `responses:` the WHAT source. `caller_responses:` is more symmetric and costs a longer key. Cheap now, expensive after templates exist.
6. **Should the router see recent turns, or strictly the last agent utterance?** The contract says `route(agent_transcript, responses)`. `build_context` already caps `recent_turns` at 6, and an agent that re-asks across two finals routes wrong against one — but the Jev research's own warning applies directly: accuracy falls as irrelevant state grows. v1 keeps the contract exact.
7. **Is `off_script` supposed to ever end the call?** v1: no, by contract ("responds as a reasonable caller would, and records the deviation"). If you want a Nth-consecutive cutoff, it needs a new `EndedBy` **and** a `_CONTRACT_END_SIDES` entry that maps to no side, or it silently breaks the two `ended_by: sim` template asserts.
---

## Bead map

The plan and the bead set drifted twice during this work — the plan said "place the branch after
the agent-turn wait", which is the wrong end of the loop, and the config block kept two dead keys
after the code had cut them. This map is the guard: **a design change is not done until the bead
and this section agree.**

| Bead | Owns | Lands in |
|---|---|---|
| `v2-1` | provider enum guarantees, ceilings, schema shape | this plan §1 |
| `v2-2` | text-backend reuse, prompt clause, `relevant_facts` | this plan §3a |
| `v2-3` | `responses.py` — the catalog | `caller_contract/responses.py` |
| `v2-4` | `router` / `text_planner` config blocks | `config.py` |
| `v2-5` | the port, degeneracy guard, retry policy | `caller_contract/router.py` |
| `v2-6` / `v2-7` | OpenAI / Gemini adapters | `router_openai.py`, `router_gemini.py` |
| `v2-8` | scenario parse + export, together | `scenario.py`, `scenario_from_dict.py`, `scenario_yaml.py` |
| `v2-9` | the driver branch **and the attach seam** | `caller_contract/driver.py`, `language_adapter.py`, `caller_contract/live_wiring.py` |
| `v2-10` | router evidence in the run summary | `run_orchestrator.py` |
| `v2-11` | attribution both directions, both planner modes | `tests/test_router_smoke.py` |
| `v2-20` | the package's own router scenario, driven in CI | `templates/examples/router-smoke.yaml`, `tests/test_router_smoke_template.py` |
| `v2-21` | the `--no-router` abort path | `cli.py`, `ops.py`, `run_orchestrator.py` |
| `v2-22` | ordering vs the DTMF track | this plan §4 |
| `v2-13` | prompt guide | `docs/router-prompts.md` |
| `v2-14` | architecture diagram, AGENTS.md, README, migration guide | `NEW_ARCHITECTURE_…md`, `docs/migration-caller-steps-to-responses.md` |
| `v2-15` | judge / eval | judge surface |
| `v2-16` | fixtures + snapshot regeneration | `tests/fixtures/`, snapshots |
| `v2-17` | CLI help | `cli.py` |
| `v2-18` | `http_json.post_json` shared by text backend and router | P2 refactor, after the router ships |
| `v2-19` | web report player renders the router key | `web/` — blocked on `v2-16` |
| `v2-26` | `cargo fmt` — **NEEDS A RUST TOOLCHAIN**, unavailable here | Rust CI |
| `v2-27` | `lks init` router config — **OWNER DECISION** | `templates/`, `ops.py` |
| `v2-28`…`v2-32` | Rust parity: decision, fail-fast, cleanup | `src/livekit_agent_simulator_rust/` |

### Where the implementation differs from the first draft

Four corrections, all verified rather than assumed:

1. **The routing branch replaces the GENERATE step only.** The first draft said "after the
   agent-turn wait and before the generate/validate loop" — those two are in the wrong relative
   order. The loop is generate-then-listen, so a branch placed after the wait sits downstream of
   publish and never gets first crack. A branch that publishes and `continue`s is worse: it skips
   the wait, and `evaluate_behavior` is what decides whether a behavior is satisfied, so every
   routed turn would end `BEHAVIOR_TIMEOUT`. **Routing changes WHAT the caller says, never WHEN it
   speaks.**
2. **The gate is `agent_text` truthiness, not a turn index.** `agent_text` is reset per *behavior*
   (`driver.py:629`), so a three-behavior scenario has three legacy openings, not one. An index
   gate only works by accident of where the variable happens to be initialised.
3. **Two config keys were cut as dead surface** (`unknown_policy`, `prompt_version`) — the plan's
   first draft still listed them. The off-script verdict is a *catalog lookup*, not a config branch.
4. **A branch that is never wired is not implemented.** `v2-9` was originally closed on "the branch
   runs correctly when called". Nothing in the run path ever assigned `driver.router`, so the branch
   was dead code in every real run while all 1293 tests passed — they injected the router by hand.
   This is the same shape as the 4/6 unreachable `EndedBy` keys found the same day. **The exit
   criterion for any driver-integration bead is now: drive `run_contract_driver_path` itself, and
   mutation-verify** (replace the attach call with `pass`; the test must fail). A unit test that
   calls the seam directly cannot see the call site disappear — that mistake was made here and
   caught only by mutation.

`text_planner` was itself a dead knob until `v2-9` gave it consumers: `enabled: false` publishes
catalog text verbatim (byte-exact mode), otherwise the block's own provider/model/api_key build a
paraphrase backend for routed lines. Cutting it instead was the other option, but `enabled: false`
is what D12's record/replay ban rests on.

### Suite baseline

`1156 → 1321` as of `097e9c8`. Re-measure before trusting any number: this tree is shared and
changes under measurement.
