# Problems — observed against a duplex (GPT-Live) agent

> Written 2026-09-29 from live runs of `gpt-live-happy-path` and
> `gpt-live-queue-fifo` against a `gpt-live-1` Conversation Flow worker
> (agent `agent_fu6wozvzu41ztgo2rljs9zki`, E-Broad Communications NO DTMF).
>
> Scope: problems **in the simulator**. Transport behaviour of the agent under
> test is called out separately in §4 so the two are not conflated — a large
> share of the confusion in these runs came from attributing a model limitation
> to the harness.
>
> Unrelated to the SIP problems catalogued in `docs/PROBLEM.md`.

---

## 1. `silence` trigger fires while the agent is still speaking

### Problem

`silence` triggers gate on `AgentTurnWait.is_agent_speaking_now()` →
`Observer.agent_is_active_speaker`, which is set **only** from LiveKit's
`active_speakers_changed` event (`livekit/observer.py:242-250`). That signal
**lags the agent's real audio by ~2.4s**, so the trigger can satisfy
"agent has been silent for `delay_ms`" while the agent is mid-turn.

Measured on run 035 (happy-path, `delay_ms: 2600`):

```
15406 ms  agent transcript final lands          (agent is audibly speaking)
17360 ms  contract.trigger_fired kind=silence   ← fired anyway
17781 ms  active_speakers: [agent]              ← flag only turns up now
18296 ms  active_speakers: [agent]
19093 ms  active_speakers: [agent, lks-caller]
20390 ms  contract.say_published (caller line)  ← lands inside the agent's window
```

The caller then spoke at 20.4s while the agent was an active speaker
(17.8–19.9s) and the utterance was **never delivered to the agent as a user
turn**. `extracted_company_name` stayed `null` for the rest of the call and the
flow re-asked the same question repeatedly.

```
silence trigger ──(active_speakers lag 2.4s)──► fires mid-turn
        │
        ▼
caller speaks over agent
        │
        ▼
agent never sees the utterance ──► field stays null ──► flow loops
```

### Evidence it is a timing signal, not a caller bug

The identical scripted line, at `delay_ms: 6500` (run 036), is captured
perfectly:

```
29546 ms  user interim "It's Bluebird Property Management"
32296 ms  user final  "It's Bluebird Property Management"
32484 ms  agent final "Thank you."          ← on-flow
```

### Mitigating today

None in the harness — the delay is set per scenario. `gpt-live-happy-path` uses
`6500ms` with a comment recording why. **This is load-bearing, not a
workaround**: shortening it re-breaks the run.

### Researched: the signal that replaces `active_speakers`

**Chosen signal: the agent's own state machine, published as the participant
attribute `lk.agent.state`.** Values are exactly
`'idle' | 'initializing' | 'listening' | 'thinking' | 'speaking'`
(`AgentState` in `livekit/attribute-definitions`, the JSON Schema that generates
`client-sdk-js/src/room/attribute-typings.ts`).

Turn completion becomes `speaking` → `listening`. That is the agent's own
decision that its turn is over, not an inference from audio energy by the SFU —
which is precisely the distinction §1 is about. The current gate conflates
"loud" with "still talking"; this one does not.

#### Why not `agent_state_changed`

There is no such event on a client. `livekit.protocol.agent_pb.agent_session`
defines `AgentSessionEvent.agent_state_changed` and `AgentState`
(`AS_INITIALIZING/IDLE/LISTENING/THINKING/SPEAKING`), but that is the
**agent-worker** IPC message. The Python client SDK (`livekit` 1.1.13)
declares 35 room events in `EventTypes` (`.venv/.../livekit/rtc/room.py:54`)
and `agent_state_changed` is not one of them; `grep agent_state
.venv/.../livekit/rtc/*.py` returns nothing. A non-agent participant cannot
subscribe to it.

#### How the state actually reaches us

The worker writes it as a participant attribute, which every client receives:

```
AgentSession state machine
  → agents-js: setAttributes({ ["lk.agent.state"]: ev.newState })
       (livekit/agents/voice/room_io/room_io.ts)
  → also ATTRIBUTE_AGENT_STATE = "lk.agent.state"  (livekit/agents/types.py)
  → room participant attributes
  → client: RoomEvent "participant_attributes_changed"
       (.venv/.../livekit/rtc/room.py:54 declares it in EventTypes,
        :924-936 dispatches it, emitting (changed_attributes: dict, participant))
```

The simulator does **not** subscribe to this today. `Observer.attach()`
(`livekit/observer.py:202`) registers exactly six handlers —
`participant_connected`, `participant_disconnected`, `track_subscribed`,
`active_speakers_changed`, `disconnected`, `data_received` — and
`participant_attributes_changed` is not one of them.

The payload is already narrow: `changed_attributes` is a `dict` of only the
keys that changed (`room.py:927-930`), so the handler is a dict lookup on
`"lk.agent.state"`, not an attribute-diff walk.

Attributes travel on the reliable, ordered participant-attribute channel —
a different path from the unreliable `data_received` packets we already handle.

#### What has to be wired

1. `livekit/observer.py` — add a `@room.on("participant_attributes_changed")`
   handler; on `p.identity == self.agent_identity` and `"lk.agent.state"` in
   `changed`, set `self.agent_state` (new) and
   `self.agent_state_changed_mono` (new, `time.monotonic()`), and emit a
   `room.agent_state` event so it lands in `events.jsonl` for measurement.
2. `caller_contract/agent_wait.py` — the trigger gate. `is_agent_speaking_now()`
   (`:59-67`) and `last_speech_at_ms()` (`:69-84`) both read
   `agent_is_active_speaker`; `wait_agent_turn` reads it at `:127-128` and
   `:145`. These become `agent_state == "speaking"`.
3. `callers/gemini.py:1284`, `callers/openai.py:939` — the same one-line
   delegate in the two sim callers.
4. `caller_nudge.py:63`, `interrupt_rate.py:181,190-193,230,312` — read
   `agent_has_spoken` / `agent_is_active_speaker`; decide per site whether
   "speaking" or "has spoken" is the right question.

Keep `agent_is_active_speaker` as a **floor**, not a replacement: audio energy
is still the only signal that survives an agent that never publishes state, and
the existing three-tier wait in `wait_agent_turn` (`:118-124`) already reasons
in tiers.

#### The failure mode to design against

If the agent under test does not publish `lk.agent.state`, this signal never
fires and the gate degrades to *permanently silent* — the same class of bug as
the four unreachable `EndedBy` keys, but worse, because it fails silently
instead of raising. So: **fail loud.** If no `lk.agent.state` has been seen by
the time the first agent turn is expected, say so in the run report rather than
falling back quietly to the 6500 ms delay.

#### Lag: NOT YET MEASURED

This is the one deliverable element I could not complete. The number requires
one live run against a real duplex agent; I had no credentials, and dialling a
live voice agent is the owner's call, not mine to make unprompted. No run in
this repo has ever captured the attribute, so it cannot be recovered from
history either.

To measure, once wired, on a run that reproduces §1 (run 035 shape,
`delay_ms: 2600`), read `events.jsonl` and compare four timestamps for the same
agent turn:

| Signal | Timestamp to read |
|---|---|
| ground truth | `room.agent_state` → `speaking` (agent's own decision) |
| candidate | `room.active_speakers` first including the agent |
| transcript | agent transcript final |
| trigger | `contract.trigger_fired kind=silence` |

Lag = `active_speakers` − `agent_state`. The hypothesis to falsify is that the
current ~2.4s shrinks to ≈0, **and** that `speaking` → `listening` lands *after*
the last audio frame rather than before it — if `listening` arrives early the
caller would barge in at the tail instead, trading one bug for another. Measure
both edges. The docs' own caution applies in reverse here: `waiting` on a
genuine turn end is the correct fix, but only if the signal is honest about
when the audio actually stops.

**Status: implemented (`turn-alignment.2`).** `lk.agent.state` is now tracked in
`livekit/observer.py` and preferred by `ObserverAgentWait.is_agent_speaking_now`,
which is the single point feeding every trigger loop. The loop itself, its
`TRIGGER_GAP_TOLERANCE_S` continuity, and all three trigger kinds are untouched
— only the *signal* changed, which is what the bead asked for. The delay knob
remains (AGENTS.md: removing it would be a dead-feature regression).

Two things worth knowing:

- **The fallback is recorded, not silent.** An agent that publishes nothing still
  falls back to energy — correct, and the historical behaviour — but
  `run_contract_driver_path` emits `contract.agent_state_unavailable` when that
  happened. A run that degraded must not read as a run that worked; that is the
  same silent-failure shape that let the router ship un-attached and let
  `confidence` sit permanently null.
- **The falsifiable claim above is still open.** This change makes the harness
  *able* to use an honest signal; whether `listening` actually lands after the
  last audio frame needs a live duplex run, and the symmetric failure (barging
  in at the tail) is exactly as bad as the one being fixed. Measured by
  `voice-ai-agent-a1`, not asserted here.

---

## 2. No decision layer — the AI sits at phrasing only, there is no router

### Problem

The simulator's only AI component is the **text backend**, and it is explicit
about its scope (`caller_contract/text_backends.py`):

> "You do not decide what to do next, when to speak, or when to end the call —
> you only phrase the CURRENT behavior naturally in first person."

So the decision of *what* to say is made entirely by the authored
`caller_steps`, which the driver walks **once, in fixed order**
(`run_orchestrator.py:399-410`: "caller_contract single path (the ONLY caller
path)"). There is no component that reads the agent's last utterance and picks
the next behaviour.

```
persona ──► [AI: phrase the line] ──► publish
                ▲
                └── "behaviour N" came from the fixed list, not from context
```

The result is a **script/flow mismatch** whenever the agent asks something the
script did not anticipate.

### Observed (run 035, same call)

```
46891 ms  agent: "...provide your callback phone number..."
67688 ms  caller: "You can reach me on five five five zero one four two"   ← already said it
83671 ms  agent: "...the number of floors and total units..."
77296 ms  caller: "It's 14 Rosewood Lane"                                 ← off-script
→ caller exhausted its 6 steps, flow still had questions → TimeoutError
```

The caller was answering a question the agent had already moved past, because
the script is positional, not reactive.

### Related

`docs/research-livekit-official-simulations.md` already covers LiveKit's own
scenario format. The relevant piece for this problem is their `FACTS` block —
facts authored up front, **withheld until asked, one per turn** — which is
exactly the missing decision layer.

**Status: implemented.** The response router that closes this gap is specified in
[`docs/plans/response-router.md`](docs/plans/response-router.md) — see §2 and §3
below. Note it lands as `responses` (a catalog of things the caller *does*),
not `facts` (values), because the routing unit is a response, not data.

**Beads that implement it:** `response-router-v2-3` (catalog), `.5`/`.6`/`.7`
(port + providers), `.8` (parse/export), `.9` (driver + **the attach seam**),
`.10` (evidence), `.11` (smoke), `.20` (package dogfoods it in CI). Migration
guide and target-repo hand-off:
[`docs/migration-caller-steps-to-responses.md`](docs/migration-caller-steps-to-responses.md).

---

## 3. Script exhaustion is unrecoverable

### Problem

`caller_steps` is a fixed-length list. When the agent asks more questions than
the script covers, the run ends with `TimeoutError: timed out` and the LLM
judge returns `verdict: error, score: null` — no partial credit, and the
transcript is often still perfectly healthy.

This makes the harness brittle for any flow whose question count is not known in
advance: the natural fix (add more steps) makes the script drift further from
whatever the agent actually asked, feeding problem 2.

**Status: implemented** by the same beads as §2 — the router answers one
question per turn against an authored catalog, so the caller is no longer
walking a fixed-length list. `max_turns` still bounds the behavior, but budget
exhaustion on a routed turn is a clean exit, not a caller violation.

---

## 4. Not simulator problems — do not file these here

Recorded so they stop being re-investigated.

| Observation | Attribution |
|---|---|
| Caller speech overlapping agent speech is never delivered to the agent as a user turn | **GPT-Live.** *"GPT-Live listens while it speaks, and it decides when to stop"* — no documented guarantee that an overlap becomes a turn. The harness *causes* the overlap (problem 1); it does not drop the audio. |
| The model spontaneously opens turns during caller silence (`idle_timeout_ms`) | **GPT-Live, by design.** There is **no client-side knob** — `turn_detection` / `create_response` / `interrupt_response` appear **nowhere** in the plugin's `gpt_live_*` files. `SessionConfig` exposes only `model`, `instructions`, `input`, `audio`, `delegation`. These knobs belong to **OpenAI Realtime** and do not transfer. |
| The agent's line differs word-for-word from the authored flow text | **`appendCommentary` paraphrases.** 2 of 3 observed "off-track" lines are absent from the authored `flowSnapshot`. Expected per the port's own contract. Note this *violates* a flow whose system prompt demands verbatim speech — a real constraint for the agent side, not a harness bug. |
| Dropped user turn while agent speaks | **Not `discardAudioIfUninterruptible`.** That defaults to falsy, is set only on the OpenAI Realtime policy path, and requires a `SpeechHandle` that GPT-Live model speech does not produce. A VAD **is** correctly passed (`build-openai-live-stack.ts:109`). |

---

## 5. Open questions

- Does `active_speakers_changed` lag on GPT-Live specifically, or is this
  generic to any duplex transport? Not measured against Realtime/Gemini.
- Is there a cheaper turn-completion signal than `active_speakers`? LiveKit
  exposes `agent_state_changed` (`speaking → listening|idle`) as agent-side
  state — the simulator currently does not consume it. This looks like the
  principled fix for problem 1 and is **not yet evaluated**.
- What is the right seam for a decision layer (problem 2) that keeps the
  anti-hallucination property the current design deliberately bought? Candidate:
  route among authored `FACTS` only, never generate new behaviours.

## 6. Response-router authoring — the `do:` target allowlist is undocumented

> Found 2026-09-29 while converting the `voice-ai-agent` GPT-Live scenarios to
> the response-router v2 design. This is the successor to problem 2: the decision
> layer now exists, and this is what it costs to author against it.

### Problem

A scenario that authors `responses:` must also keep a `caller_steps` turn 0, and
that turn must be a `do:` **behavior** — a scripted `say:` hands iteration 2
straight to `end:` and the router never runs (see
`caller_contract/driver.py:704`: the router is gated on `agent_text`, which is
empty on the first iteration). That `do:` is then validated by the semantic
verifier, which rejects it with `CALLER_BEHAVIOR_VIOLATION: LOW_CONFIDENCE` if
the generated utterance does not read as the declared `behavior` toward the
declared `target`.

`DEFAULT_BEHAVIOR_CATALOG` (`caller_contract/dsl.py:37`) enumerates the eleven
valid behaviors. It enumerates **no targets** — yet a target is half of what the
verifier grades against.

### Why the existing warning is not enough

`templates/examples/router-smoke.yaml` warns:

> The behaviour verb is deliberately one the semantic verifier already accepts
> (negotiate/price), **not one invented for this smoke**. … turn 0 is legacy and
> does go through validation, so it needs a verb with a known-good utterance.

The package names one known-good pair and nothing else. A reader has three
options, all bad: copy `negotiate`/`price` and ship a turn 0 unrelated to the
rest of the flow; invent a target and discover by running; or grep the templates
for one that happens to fit. Known-good targets recoverable by grep are
`price`, `hours`, `charge` — all **commercial** intents, so a scenario about
reporting a building problem has no usable pair and the obvious response is the
expensive one.

### Cost

Each attempt is a real call — an OpenAI Realtime caller, a live GPT-Live agent,
~70s, plus first-audio latency. The failure surfaces only *after* the call, as
`status: failed / hard_reasons: ["status:failed"]`. `lks validate` reports
`✓ valid`, `warnings: none`, `authoring tier: blocking`. The actual error is one
layer down and is not printed by the CLI summary table:

```
ContractDriverFailure: CALLER_BEHAVIOR_VIOLATION: LOW_CONFIDENCE
```

### Suggested fixes

1. ~~**Print the cause of a failed run in the CLI output.**~~ **DONE (`32e4f42`).**
   Two defects, not one: `render_execute`'s iterations table had **no** `error`
   column, and `render_execute_all` had one that `ops.execute_scenario` never
   populated (the run result had no `error` key). Both fixed —
   `run_scenario_instance` now returns `error` via `diagnose_failure()`, which
   scans the reporter events for any spec carrying an error. Matched on "any
   spec with an error" rather than on event kind, because the paths use
   different kinds (`run.error`, `sim.error`, `sim.leg_error`,
   `dispatch.agent_timeout`) and enumerating them is a list to forget to update.

   This fix was found independently while working on something else, and it
   immediately paid for itself: it turned the `_run_scenario()` `TypeError`
   below from a multi-turn bisect into a one-second read.

2. **Validate `do:` targets at PARSE time.** **PARTLY DONE (`32e4f42`+).**
   Structure is now checked — a target that is not a non-empty string is a
   parse error naming `do.target`, free instead of a 70s paid call.

   **The allowlist half is rejected, deliberately.** The original suggestion was
   to enumerate known-good targets beside `DEFAULT_BEHAVIOR_CATALOG`, but
   `dsl.py:37-40` states the catalog "must never be hardcoded business
   vocabulary (AGENTS.md generic-core rule)". `price` / `order_status` / `fees`
   *are* business vocabulary. The behaviors (`ask`, `confirm`, `deny`) are
   legitimately generic conversational primitives; targets are not. Enumerating
   them would also bake this package's first commercial scenario into every
   consumer's core.

   **The limit of the check, stated plainly:** it does NOT tell you whether a
   target is one the semantic verifier recognises. That vocabulary is
   scenario-specific, so it belongs in docs and examples — not in a catalog in
   `src/`.

3. **Still open — a non-commercial worked example.** The structural check
   catches `target: 123`; it cannot catch `target: <a real word the verifier
   happens not to know>`, which is the actual reported failure. `router-smoke.yaml`
   uses `negotiate`/`price` and says so, but a scenario about reporting a
   building problem still has no starting point. Grep-recoverable known-good
   targets are `price`, `hours`, `charge` — **all commercial**, which is the
   whole complaint.

4. **Still open — surface the verdict vocabulary at validate time.** `lks validate`
   reports `✓ valid`, `warnings: none`, `authoring tier: blocking` for a scenario
   that will fail on the call. The parse gate cannot know this without a
   target allowlist, so the honest options are a warning (not an error) listing
   the *shape* of the risk, or documentation.

### Reproduction

```yaml
responses:
  off_script: { intent: off_script, instruction: "…", text: "…", system: true }
caller_steps:
  - do:
      behavior: ask
      target: <invented target>
      constraints: { max_turns: 2 }
  - end: true
```

`lks validate` → `✓ valid`, `warnings: none`. `lks execute` →
`CALLER_BEHAVIOR_VIOLATION: LOW_CONFIDENCE`, after the call has run.

## 7. `lks execute` fails 100% — `no_router` is passed but not accepted

> Found 2026-09-29. **Committed at HEAD, not a work-in-progress.** Blocks every
> `lks execute` in the repo, silently.

### Problem

```
TypeError: _run_scenario() got an unexpected keyword argument 'no_router'
```

`ops.py:531` calls `_run_scenario(..., no_router=no_router)`, but the signature
of `async def _run_scenario(...)` ends at `replay_path: Any = None` — there is
no `no_router` parameter. Introduced by `bda3cb6 feat(cli): --no-router abort
path (response-router v2-21)`, which added the parameter to `execute_scenario`
and to the call site but not to the callee's signature.

Confirmed present in `git show HEAD:src/livekit_agent_simulator/ops.py`.

### Why it is worse than it looks

The `TypeError` is raised inside `ops.py`'s `except Exception` arm, so it is
swallowed into:

```python
{"executed": True, "run_id": None, "status": "failed", "error": "TypeError: ..."}
```

No room is created, no report directory, no job reaches the agent, no row lands
in `run_events`. From the outside it is indistinguishable from a dead
infrastructure. The only symptom is `status: failed` /
`hard_reasons: ["status:failed"]` — which is why this cost a long bisect before
the cause was found.

### Fix

Add `no_router: bool = False` to `_run_scenario`'s signature and forward it to
`run_scenario_instance` if that does not already accept it. A regression test
should call `execute_scenario(no_router=True)` and assert it does not raise.

### Note

The in-progress `diagnose_failure` + `error` column in `cli_render.py` turns
this from a bisect into a one-second read, which is what surfaced it. Reported
to the owning session separately.

## 8. The router routes the agent's PREAMBLE, and `off_script` does not absorb it

> Found 2026-09-29, run `009-…-b1ae` — the first run to pass the gate since the
> `no_router` fix (problem 7). The gate is green and the conversation is still wrong.

### Problem

Every `contract.router_decision` is one step behind, because turn 1 classifies the
agent's **preamble**, not a question:

| turn | `agent_text` | `response_id` | correct? |
|---|---|---|---|
| 1 | "Thank you for considering our service. This automated system will take down your building details…" | `vague_company` | no |
| 2 | "To begin, could I please get your company name and the full name of the person in charge?" | `contact_name` | no — should be `vague_company` |
| 3 | "Thank you" | `wrap_up` | yes |

`off_script` — reserved, `system: true`, described in `router-smoke.yaml` as
"an agent that goes off-script is RECORDED rather than papered over with a
fallback branch" — never fired. The preamble has no matching entry and was
assigned to the first response instead.

Observable consequence on the agent side: the caller answers out of order, so the
conversation ends with the agent asking *"could you tell me your company name
again?"* — it never received one.

### Two things worth checking

- **`confidence: null` on every decision**, including the ones that are right.
  If `null` means "the adapter did not return a confidence" rather than
  "confident", the router has no signal for when it is unsure, and a catalog
  with no match assigns into a response rather than falling to `off_script`.
- **Is this authoring or a router defect?** A catalog that omits the preamble is
  a legitimate authoring gap, and adding an entry is the caller's job. But
  `off_script` exists precisely for the case where nothing matches, and it did
  not engage. Which of the two it should be determines whether the fix is a
  YAML entry or a router change.

### Answered (2026-09-29, read from source)

**`confidence` is structurally impossible, not merely absent.** `build_route_schema`
(`router.py:120-130`) declares only `responseId` and sets `additionalProperties: False`,
so the provider is *forbidden* from returning a confidence. `parse_route_body:204`
therefore reads `raw.get("confidence")` and always gets `None`. The
`RouteDecision.confidence` docstring (`router.py:75-77`) promises telemetry "so a
low-confidence case can be found when a prompt needs work" — that sentence can
never come true by construction. It is a dead knob: a field, a docstring, no
consumer. The only mechanical backstop is `DegeneracyGuard`, which needs three
consecutive identical ids; alternating ids never trigger it.

**`off_script` is a label, not a fallback.** `offerable_ids()` (`responses.py:239-246`)
states the system response is *"always offerable"*, so on turn 1 the `off_script`
entry was in the options and the router chose `vague_company` over it — the
prompt only says "choose the system entry ONLY when nothing else fits", and the
model did not comply. `driver.py:730` then derives `off_script = bool(spec.system)`
from the *label*, not from whether anything matched. The harness therefore
**cannot distinguish**:

- "the router knows nothing matched" → a true `off_script`, and
- "the router is confidently wrong" → still reported as `matched`.

Turn 1 is exactly the second case: `matched`, `confidence: null`, for a preamble
that matches no entry.

**This is the mirror of the failure the design already guards against.**
`run_orchestrator.py:133` worries about the agent going off-script and the
harness recording a *false `off_script`*. The design protects that direction;
what actually happens is a **false `matched`**, and nothing guards it. A
one-sided guard: whichever direction is unobserved is the one that fails.

**Authoring still needs the entry, and it is a band-aid.** A catalog cannot
enumerate everything-that-does-not-match, so the next scenario meets the same
class. Treat "false `matched`" as its own design gap rather than something the
YAML can fix.

### Proposed fix (consolidated across review)

`NO_MATCH = "__none__"` added to the `enum` in `build_route_schema`, so "nothing
matches" becomes expressible. Preferred over `type: ["string","null"]`: a null
does not read as a decision and is not greppable, a sentinel is both.

**The branch must be three-way, not two.** This is the part that is easy to
miss and that would undo the point of the change:

```python
if decision == NO_MATCH:
    off_script = True
elif decision in response_ids:
    off_script = False
else:
    raise UnknownResponseId(decision)   # a hallucinated id must NOT become off_script
```

If the driver maps "anything not in `response_ids`" to `off_script`, the sentinel
**masks model hallucinations** — collapsing "the model declined" and "the model
invented an id" into the same label. That is the opposite of what this fix is
for.

**Do not remove `confidence` in the same PR.** The sentinel makes `off_script` a
real decision and leaves `confidence` as a dead criterion; both are true, but two
concerns in one change is one hard review. Sentinel first, `confidence` as its
own bead — remove the field and its docstring if nobody claims it.

### Test conditions

1. Current behaviour cannot distinguish false-`matched` from a real match
   (turn 1 of run 011).
2. `confidence` is always `None` regardless of what the body returns.
3. `confidence` is a dead knob — it has a docstring promising telemetry, a real
   consumer (`_summarize_router` writes `decisions[]`), and a test asserting it
   exists, so it looks alive while it never has a value. Note that removing the
   field will turn that test red, which is correct.
4. **A genuinely matching turn still returns its own id, not `__none__`.** Without
   this, the sentinel is only shown not to break everything, not shown to
   discriminate.
5. **The sentinel survives into the router log / events JSONL**, so the next
   false `matched` is greppable instead of reconstructed by hand.
6. **`off_script` is still a valid `response_id`** for any scenario that defines
   one — the sentinel must not swallow it.

### Evidence

`contract.router_decision` events in `reports/009-gpt-live-retry-while-speaking-20260929-094728-b1ae/events.jsonl`.

#### RETRACTION (2026-09-30) — the catalog is NOT the problem; the harness reads the wrong turn

**Everything above this line is wrong about the cause.** I concluded the router was
"guessing and labelling it matched" from reading transcripts, without measuring.
Measured, it is the opposite: **the router picks the CORRECT response, for the
NEXT turn's question.**

Verified by POSTing directly to the judge/router endpoint with *this repo's real*
catalog, `_SYSTEM` and `_user_prompt`: turn 5 — "provide your callback phone
number" → `callback_number`. Catalog, prompt, schema and endpoint all correct.

Re-reading the run-013 table with that in mind:

| turn | agent asked | observed | actually correct for |
|---|---|---|---|
| 2 | "are you still there?" | `wrap_up` | `off_script` |
| 4 | "anything else today?" | `callback_number` | turn 5's question |
| 5 | "callback phone number?" | `contact_name` | the contact-name ask |
| untranscribed | *(empty)* | `preamble_ack` | `off_script` |

Every "miss" is the right answer one turn later. The mechanism is
`agent_wait.py::wait_agent_turn`, which returns
`observer.last_agent_final_text` — a **single overwritten slot, not a queue**
(no `deque`/`queue` anywhere). With duplex, `still_speaking` lags ~2.4s
(problem 1, and what the turn-alignment beads are fixing), so turn N's final
arrives while the agent is still flagged speaking, turn N+1's final overwrites
it, and the function returns turn N+1's text.

**Two consequences:**

1. `probabilities`/logprobs would make this **worse** — adding abstain to a
   router that already chose correctly turns a right turn into `off_script`.
   Withdrawn from the plan.
2. One-turn-behind is a **consequence of problem 1's signal lag**, not a router
   contract bug. Fixing the signal fixes this as a side effect; the single-slot
   → queue change makes correctness independent of it in the meantime.

Run `027-gpt-live-happy-path-20260930-031157-8809` is the artifact I misread, and
should be used as the replay input for the regression test.


#### Run 013 — confirms it, and answers "what actually decides it"

`reports/013-gpt-live-retry-while-speaking-20260929-104409-e643/events.jsonl`,
scenario `gpt-live-retry-while-speaking`, catalog with `preamble_ack` added.

| turn | `agent_text` | `response_id` | correct? |
|---|---|---|---|
| 1 | "To begin, could I please get your company name and the full name of the person in charge?" | `vague_company` | yes |
| 2 | "Next, are you still there?" | `wrap_up` | no |
| 3 | "[untranscribed agent speech]" | `preamble_ack` | no — matched untranscribed noise |
| 4 | "Great, glad to hear that. Would you like to go over anything else or have me pass along an…" | `callback_number` | no |
| 5 | "Thank you. Next, could you please provide your callback phone number, including the area c…" | `contact_name` | **no — a genuine match was bypassed** |
| 6 | "CouldOkay," | `off_script` | yes (garbled input) |

Turn 1 is now correct: with the preamble no longer routed, the first
question reaches `vague_company` as intended. From turn 2 the router is
**one step behind again**, and turn 5 is the sharpest evidence in the
whole set — the agent asks *for a callback phone number by name*,
`callback_number` is in the catalog with exactly that instruction, and the
router picks `contact_name`. That is test condition 4: a genuinely
matching entry the router still ignored. **The catalog is not the
control surface.**

Turn 3 also shows a second failure mode: `[untranscribed agent speech]`
is routed to a real entry rather than falling through, so an
untranscribable turn fabricates a confident answer instead of declining.

---

## Evidence index

| Run | Scenario | Outcome |
|---|---|---|
| `034-gpt-live-queue-fifo-20260929-013239-2065` | barge, `delay_ms: 1000` | failed — `require_superseded` (supersession never occurred; nodes are static/pregen clips) |
| `035-gpt-live-happy-path-20260929-013951-ca38` | cooperative, `delay_ms: 2600` | failed — caller overlap lost a user turn; company name never extracted |
| `036-gpt-live-happy-path-20260929-014856-58a9` | cooperative, `delay_ms: 6500` | failed — script exhausted, flow still asking; transcript healthy through node 1-b |
| `004-gpt-live-retry-while-speaking-20260929-091122-ef64` | `responses:` + `caller_steps: [say, end]` | failed — call ended after 7s, `contract_scenario_end`; router never got a turn (problem 6) |
| `006-gpt-live-retry-while-speaking-20260929-092157-8466` | `responses:` + `do: ask / <invented target>` | failed — `CALLER_BEHAVIOR_VIOLATION: LOW_CONFIDENCE` (problem 6) |
| `008-gpt-live-retry-while-speaking-20260929-094309-9dc4` | `do: ask / hours`, `max_turns: 2` | failed — `BEHAVIOR_TIMEOUT`; behavior budget exhausted mid-call |
| `009-gpt-live-retry-while-speaking-20260929-094728-b1ae` | `do: ask / hours`, `max_turns: 8` | **gate green** (`ok: ✓`) — but the router routed the preamble and every answer was off by one (problem 8) |
