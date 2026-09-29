# The two prompts

A routed call uses two different language-model calls, and confusing them is
the easiest way to make the router useless. They have different jobs, different
inputs, and — critically — **one of them must never see the authored answer**.

| | Router prompt | Text-backend prompt |
|---|---|---|
| Job | pick *which* authored response answers the agent | phrase that response as natural speech |
| Sees | `intent` + `instruction` per option | `canonical_text` — the authored line |
| Must NOT see | the authored `text` | — |
| Output | one `responseId` | one JSON object with the utterance |
| Model | `router:` block | `simulator:` (free path) or `text_planner:` (routed) |

Both are pinned by snapshot tests in `tests/test_router_prompts.py`. Editing
either prompt changes production behaviour invisibly, so the snapshots are the
tripwire: a diff there must be justified in the commit that caused it.

## Router prompt

`router_openai.py` / `router_gemini.py` — `_SYSTEM` plus `_user_prompt()`, which
renders:

```json
{
  "agent_said": "<the agent's line>",
  "responses": [
    {"responseId": "...", "intent": "...", "instruction": "..."}
  ]
}
```

**Enum order is part of the contract.** The options appear in
`catalog.offerable_ids()` order, which is the order the model sees. Reordering
changes the prompt, so it changes the snapshot — that is the point.

**The system entry's instruction is phrased as a selection rule, not an
action.** A model handed N imperatives has a strong prior to return one of
them, so "Keep the call on track" is safer than "Redirect the caller".

### Why the router never sees `text`

If the authored answer reaches the router, it can pattern-match the *answer*
instead of reasoning about the *question* — and every route becomes
"whatever most resembles this string", which is not a router. This is why
`ResponseSpec` documents `text` as ground truth the backend publishes and
never shows to the router.

`test_canonical_text_never_reaches_the_router_prompt` is the cheap guard
against that: it asserts no catalog `text` appears anywhere in the rendered
router prompt. It was mutation-checked — reintroducing `"text": spec.text`
into the router's option list fails it.

## Text-backend prompt

`text_backends.py` — `_SYSTEM_PROMPT` plus `_build_user_prompt()`, which is
`json.dumps(context)`. On a routed turn the context carries `canonical_text`,
and `_SYSTEM_PROMPT` has one clause about it:

> If the context carries `canonical_text`, that is the line to speak: phrase it
> naturally, but do not add any fact, name, number, or offer that is not
> already in it, and do not drop any part of it.

**One clause, in the existing constant.** There is deliberately no second
system prompt for the routed path: two constants would be a dual path, and the
two would drift the moment either was edited.

The clause forbids both drift directions, because both defeat authoring:

- **adding** — inventing a detail the author never wrote makes the response
  catalog a starting point rather than a specification
- **dropping** — silently omitting part of the authored line is the same
  failure wearing a disguise

### `relevant_facts` is not a delivery channel

The routed context keeps `relevant_facts` (as the persona-background channel
it has always been, empty in practice). **Never put catalog text in it.**
`relevant_facts` reads to the model as a fact the persona already knows, so an
authored answer placed there gets treated as background to elaborate around
rather than a line to speak — the exact failure `canonical_text` exists to
prevent. `canonical_text` is the only carrier of the authored line.

## Two modes

`text_planner.enabled: false` publishes `spec.text` **verbatim** — no
paraphrase, no second model call, byte-identical every run. That is what makes
a router scenario reproducible, and it is why record/replay is refused for
routed runs with the planner on: a non-deterministic wording cannot be
replayed against a recording.

`enabled: true` sends the routed line through the text backend to be phrased
naturally. Better speech, worse reproducibility. Choose per scenario, not per
run.

That makes routed runs with the planner on non-deterministic, which is the
design reason record/replay is not offered for them — a recording cannot be
replayed against wording the model re-derives each time.

**This is a design rule, not a code guard today.** Nothing in `record_replay.py`
or `live_wiring.py` refuses a routed run with `--record`; the only enforcement
there is `record_path`/`replay_path` mutual exclusion. If you need that refusal
to be real rather than documented, it wants a test and a raise — the gap is
recorded on bead `livekit-agent-simulator-response-router-v2-13-ltq`.

## Terminology

The router-facing vocabulary is **responses** and **intents**. "caller step",
"next step", and "behavior" belong to the legacy free-generation path and
should not appear in router-facing text. The text backend legitimately says
`current_behavior` — that is its own contract, and the terminology test scopes
itself to the router prompt accordingly.

## Changing a prompt

1. Edit it.
2. Run `uv run pytest tests/test_router_prompts.py`.
3. A snapshot diff is expected — read it and confirm it is what you meant.
   Enum reorder, a reworded instruction, and a dropped clause all show up
   here; a silent change is the failure this file exists to prevent.
4. Say in the commit message why the prompt had to change.
