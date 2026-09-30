# Migrating a scenario: `caller_steps` → `responses`

`caller_steps` and `responses` **coexist**. A scenario may carry both; this
is decision D13, and it is the migration's safety net — the seven archived
scenarios keep byte-identical behaviour because nothing about them changes.

| The scenario has | What happens |
|---|---|
| `caller_steps` only | Legacy path. Unchanged. (Default, and still what every existing scenario does.) |
| `responses:` only | Routed path. The Decision Router picks one `responseId` per turn. |
| **both** | **The ROUTER runs.** `caller_steps` still supplies the opening action and stays mandatory, but it does not win precedence. |
| `responses:` + `router:` in config | Routed path. |
| `responses:` with **no** `router:` in config | **`ConfigError` at run time**, naming the scenario and both keys. |

That last row is the one to understand. It is unreachable through
`load_config` (parse-time validation catches it) but a directly-constructed
`Scenario` can reach the driver, and a silent fallback there would mean a
scenario that authors a catalog behaves exactly like the legacy one — which
is precisely the bug that let the router ship un-attached.

## The decision table, mechanically

```
scenario.responses is not None  AND  cfg.router is not None   ->  ROUTED
scenario.responses is not None  AND  cfg.router is None       ->  ConfigError
scenario.responses is None      AND  cfg.router is not None   ->  LEGACY (silent)
scenario.responses is None      AND  cfg.router is None       ->  LEGACY (silent)
```

`cfg.router` present with no `responses:` is **not** an error. That is the
opt-in model: an archived scenario runs unchanged even when the operator
configures a router globally, and it must stay silent or the regression net
breaks.

## What to write

```yaml
router:
  provider: openai          # openai | gemini  (jev parses, then fails loud —
  model: gpt-4.1-nano      #   there is no adapter for it in this package)
  api_key: sk-...           # config.yaml is gitignored; no api_key_env
  timeout_ms: 1500          #   indirection — a key left `false` is a second
  temperature: 0            #   source of truth, a flag in history is not

text_planner:
  enabled: true             # false => publish catalog text verbatim, which is
  provider: openai          #   the byte-exact regression mode
  model: gpt-4.1-nano
  api_key: sk-...
```

An empty `model:` selects the adapter's own default **alias**, not a pinned
version — a pinned `gemini-2.0-flash` 404s on the working key.

```yaml
responses:
  company_name:
    intent: ask_company_name                                    # stable eval key
    instruction: Select this when the agent asks which company the caller
      represents.                                               # the ROUTER matches
    text: It's Bluebird Property Management.                    # ground truth; the
                                                                 # router never sees it
    reusable: true          # NOT a TTL, not a count — and the default is
                            # FALSE, which is the part that bites. Omit it
                            # and this entry is SPENT after one use: it leaves
                            # the router's option set entirely. Ask for a name
                            # twice and the router cannot answer with the name
                            # again — it picks something else, and that reads
                            # as "the router chose wrong" when the catalog was
                            # simply empty. Set it on anything the agent could
                            # plausibly ask about twice.

  off_script:
    intent: off_script
    instruction: Select this only when the agent's question matches none of
      the other responses.
    text: I'm sorry, could we stay focused on the property inquiry?
    system: true            # REQUIRED — see below
```

### Three rules that will bite you

1. **The `system: true` entry is required, and authored.** Its id is
   reserved (a leading underscore, outside the scenario-id character class)
   so it cannot collide with an authored id. Without it, "the router always
   returns a valid id" is a hope rather than a structural property — and a
   fallback line baked into the package would be spoken by a simulated
   caller in a scenario that has nothing to do with it.

2. **Write `instruction` as a SELECTION RULE, not an action.** The router is
   handed N imperatives; a model given N imperatives has a strong prior to
   return one of them. `off_script` is the one most at risk of winning by
   default. The `Select this only when…` framing exists to counter that.

3. **`text` is never sent to the router.** If it were, the model would
   pattern-match on the *answer* instead of the *question*, and the router
   would quietly become a guesser.

4. **⚠️ A broad entry silently disables `off_script` — and with it, the whole
   attribution mechanism.** This is the one that costs the most to debug,
   because nothing errors and the router is behaving perfectly.

   An entry whose instruction is a soft catch-all — "select this when the
   agent signals the call is wrapping up, **or otherwise**" — competes with
   the `system: true` entry for exactly the inputs the system entry exists
   for. The model picks the broad entry, the caller answers, the agent
   acknowledges, and the loop repeats. Measured on the target repo, 2026-09-30:
   an agent saying *"You're welcome."* is not asking anything, so
   `off_script` is the correct verdict — but a catch-all entry framed around
   "signals the call is wrapping up" captured it, three times in a row, and
   `DegeneracyGuard` ended the run.

   Every symptom pointed at the router. It was not the router: it selected the
   same id for the same input three times, deterministically, per its own
   instruction. **`off_script` never fired, so nothing was ever attributed to
   the agent** — which is the entire reason the system entry is required.

   The check: for every entry, name an agent utterance that must select it, and
   one that must NOT. If you cannot name the second, the entry is a catch-all
   wearing a specific name. Note that acknowledgements ("You're welcome.",
   "Thank you.") are a class of agent turn the catalog vocabulary does not
   cover at all — they are not questions, and they are not out-of-scope
   deflections, so a catalog with no entry for them will always leak them
   somewhere.

## What changes at runtime

Routing replaces the **generation** step only. Everything after it is
identical, and this is load-bearing:

- the agent-turn **wait** still runs — a routed turn still has to satisfy
  its own behavior, and that requires an agent reply to evaluate against.
  Removing the wait is not "one extra listen"; it breaks the engine
  contract, and every routed turn dies at `BEHAVIOR_TIMEOUT`.
- `evaluate_behavior` still runs, on the agent's reply.
- the **Contract Validator is skipped** (D2). See §29.2 of
  `NEW_ARCHITECTURE_FOR_LKS_AND_LKSR.md` — this carves out an *absolute*
  invariant, deliberately, and §27.7 explains why. Do not "restore" it.

## Reproducibility

| | `text_planner.enabled: true` | `false` |
|---|---|---|
| Wording | persona paraphrase | authored verbatim |
| Byte-reproducible | ❌ | ✅ |
| LLM calls per turn | 2 (router + planner) | 1 |

`--record` / `--replay` is **forbidden** on router scenarios in v1: it
cannot capture an LLM decision, and with the planner on there is a second
non-deterministic call per turn.

## If the router starts misrouting

```bash
lks execute <scenario> --no-router
```

Forces that run onto the `caller_steps` path. It edits nothing and persists
nothing, and it emits `contract.router_aborted` so a run that did *not*
exercise the router can never be mistaken for one that did and passed.
`text_planner.enabled: false` is **not** equivalent — the router would still
pick ids, still record verdicts, and still bypass the validator.

---

# TARGET-REPO CHECKLIST

The scenarios this package cannot see live in **`voice-ai-agent`**
(`.agent-sim/scenarios/`), outside this package, and AGENTS.md's Boundary
rule puts them out of scope here. Nothing in CI enforces them. They must be
re-run **by hand**, and this is the hand-off most likely to be forgotten
because nothing will fail if it is skipped.

> **This is a checklist, not a suggestion.** The failure it guards against
> is invisible: an un-routed scenario emits no decision, takes the legacy
> path, and reports as a clean run.

### Re-run, and confirm unchanged

⚠️ **Two different criteria, and mixing them reports the wrong thing.** The
target repo has **no `.jsonl` fixtures** — `find .agent-sim/scenarios -name
'*.jsonl'` returns nothing; the only `.jsonl` files under `.agent-sim/` are
`reports/*/events.jsonl`, which are run *output*, not expectations. So a
byte-compare against "the `.jsonl` next to it" is not executable. That row was
wrong when this table was first written and was corrected after the peer
session globbed the directory.

Also: the seven `_archive/scenario-N.yaml` files are **not GPT-Live flow
scenarios**. Their ids are marker/cue cases (`s1_global_custom_spoken`,
`s2_no_custom_markers_at_all`, …) — no `dispatch`, no `responses:`, not in the
caller-contract domain. Checking them under "the GPT-Live flow did not change"
is a category error.

| Scenario | Criterion | What "unchanged" looks like |
|---|---|---|
| `gpt-live-happy-path` | behaviour | Caller answers the question the agent actually asked. No "already told you" repetition. |
| `gpt-live-queue-fifo` | behaviour | Flow advances node by node. `flow_superseded_turn_completion_dropped` appears when the flow overtakes GPT-Live speech. |
| `gpt-live-retry-while-speaking` | behaviour | Flow retries while the model is speaking and does not wedge. Check there is **no** `flow_superseded_completion_expired`. |
| `gpt-live-clip-to-composed` | behaviour | A pregen clip hands off to a composed node without the model re-reading it. |
| `gpt-live-ending` | behaviour | The call terminates through the ENDING node, not a timeout. |
| the 7 `_archive/scenario-N.yaml` | out of scope | Marker/cue scenarios, not caller-contract. If you want them checked, the real pairs are `templates/examples/*.yaml` ↔ `*.jsonl` **in this package**, which CI can glob. |

**Byte-identical is the right criterion only for the 7 archived scenarios AND
only as a within-this-package `templates/` comparison.** A scenario you have
deliberately converted to `responses:` is *supposed* to produce different
output — that is the point of the conversion. Comparing it byte-wise against
its `caller_steps` predecessor and calling the difference a defect reports the
migration backwards.

### Then, per scenario you intend to migrate

- [ ] Author `responses:` with a `system: true` entry.
- [ ] Confirm `lks validate` passes — it is a parse-time gate and names
      `file:line` on a malformed catalog.
- [ ] `lks validate` also fails if `responses:` is present with no `router:`
      in config. That is intended; add the block.
- [ ] First run with `text_planner.enabled: false`. The wording is then
      authored, so what the caller says is exactly what you wrote, and a
      wrong line is unambiguously a routing problem rather than a paraphrase
      problem.
- [ ] Read `summary["caller_contract"]["router"]` in the report:
      - `off_script > 0` means the **agent** went somewhere no authored
        response covers. Fix the catalog, not the caller.
      - `faults > 0` or `unroutable > 0` means a **harness** fault. It is
        counted outside the decision list on purpose, so a harness fault can
        never be misread as an agent deviation.
- [ ] Only then flip `text_planner.enabled: true`.

### Reading a router run in the report

```
matched / off_script   verdict counts, deliberately NOT merged: a single
                       total cannot tell you which way attribution failed
faults / unroutable    harness faults, outside the decision list
decisions[]            per decision: response_id, off_script, confidence,
                       backend, latency_ms, agent_text_sha
```

`agent_text` is truncated to 200 chars; `agent_text_sha` is a hash of the
**full** line. Without the hash, truncation can hide a divergence during
audit and you will never see it.
