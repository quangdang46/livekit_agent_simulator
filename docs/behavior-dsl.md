# Behavior DSL — authoring guide

This is the authoring surface for the new caller-architecture rewrite
(`src/livekit_agent_simulator/caller_contract/`). It covers exactly the
knobs that ship today and have test coverage — see
`NEW_ARCHITECTURE_FOR_LKS_AND_LKSR.md` at the repo root for the full
design rationale if you want the "why", not just the "how".

## `say` vs `do`

Two caller primitives, with very different guarantees:

```yaml
- say: "Hi, I'm calling about the 2022 Honda CR-V."
```

`say` is **100% deterministic**: the caller speaks exactly this text, no
AI, no validator. It still crosses the Orchestrator's turn gate like every
other action — it is never a parser-direct publish.

```yaml
- do: ask_price
```

or, explicit form:

```yaml
- do:
    behavior: negotiate
    target: price
    constraints:
      max_turns: 3
      max_budget: 30000
      forbidden_intents: ["financing", "trade_in", "vehicle_change"]
      must_not: ["invent_facts", "end_call"]
    interaction:
      pace: slow
      hesitation: occasional
```

`do` is **adaptive**: the AI Language Adapter generates a natural-language
utterance that must satisfy the `BehaviorContract` you declare here. The
generated candidate is always validated before anything reaches the
agent — see "Caller Contract Validator" below.

## Behavior catalog

The default allowlist (`DEFAULT_BEHAVIOR_CATALOG` in
`caller_contract/dsl.py`):

```
ask, confirm, deny, accept, reject, negotiate, clarify, provide,
arrange_visit, interrupt, end
```

Scenarios may extend this per-run (`parse_steps(..., known_behaviors=...)`)
— the catalog is never hardcoded business vocabulary in `src/`, per the
project's generic-core rule.

## Contract fields (`constraints:`)

| Field | Meaning |
|---|---|
| `max_turns` | Behavior fails the run if not satisfied within this many caller turns (default 3). |
| `max_budget` | Numeric ceiling a `negotiate`-style behavior's claimed slot may not exceed. |
| `max_words` / `max_duration_s` | Reject an otherwise-valid utterance that is too long. |
| `forbidden_intents` | Names of intents the AI must never drift into (e.g. `financing` while negotiating `price`). |
| `must_not` | Free-form guard list, e.g. `invent_facts`, `end_call`. |

These are **action-space** constraints (what the caller may *do*), not a
vocabulary/topic allowlist — see the terminology table below for why that
distinction matters.

## `interaction:` block (delivery, not content)

```yaml
interaction:
  pace: slow            # passed through to TTS as a pacing hint
  hesitation: occasional # inserts one token from a small fixed allowlist
  stumble: occasional    # repeats the first word as a stumble fragment
  pre_delay: 500         # ms, delay before speaking
  backchannel: true
  barge_in: true
```

The Interaction Planner (`caller_contract/interaction_planner.py`) applies
these to an **already-validated** utterance. It can never introduce new
words beyond the fixed hesitation allowlist — this is enforced by
construction, not just by convention.

## Other step kinds

`wait`, `dtmf`, `interrupt`, `end`, `silence`, `hangup` are also
recognized; see `caller_contract/dsl.py::_KNOWN_ACTION_KINDS`.

## Canonical terminology

| Old / superseded wording (do not use) | Canonical term |
|---|---|
| "AI optional" / "Language Adapter (optional AI)" | **`AI Language Adapter` — REQUIRED for `do:`** |
| "Constrained Language Adapter" | `AI Language Adapter` |
| `allowed_topics` / `forbidden_topics` as the primary contract | `Behavior Contract` with `constraints{max_*, forbidden_intents, must_not}` |
| A validator step called just "Semantic Validation" | `Caller Contract Validator` = deterministic checks + **Semantic Intent Verification** |
| AI output as a bare string | `CandidateUtterance` = `{act, target, slots, utterance}` — `act/target/slots` are the generator's **claim**, not evidence |
| "what the AI said" | `ObservedAct` — the verifier's own classification of the utterance, compared against the contract |

## Breaking-change notes (v1 rewrite)

This DSL is a new, additive authoring surface. It does not replace the
existing `Script`/`Behavior` scenario kinds or the legacy `ended_by`
assertion type (`sim`/`agent`/`detect`) — those continue to work as
before. See `caller_contract/failures.py` module docstring for the
explicit scope decision on why `ended_by` was not folded into the new
`EndedBy` enum.

## Example scenario

See `docs/examples/negotiate-car-price.yaml` for a complete example using
`say`, `do` (shorthand and explicit), `constraints`, and `interaction`. It
is validated against the parser in `tests/test_behavior_dsl_docs_example.py`.
