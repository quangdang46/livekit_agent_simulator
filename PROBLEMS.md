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

**Status: designed.** The response router that closes this gap is specified in
[`docs/plans/response-router.md`](docs/plans/response-router.md) — see §2 and §3
below. Note it lands as `responses` (a catalog of things the caller *does*),
not `facts` (values), because the routing unit is a response, not data.

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

---

## Evidence index

| Run | Scenario | Outcome |
|---|---|---|
| `034-gpt-live-queue-fifo-20260929-013239-2065` | barge, `delay_ms: 1000` | failed — `require_superseded` (supersession never occurred; nodes are static/pregen clips) |
| `035-gpt-live-happy-path-20260929-013951-ca38` | cooperative, `delay_ms: 2600` | failed — caller overlap lost a user turn; company name never extracted |
| `036-gpt-live-happy-path-20260929-014856-58a9` | cooperative, `delay_ms: 6500` | failed — script exhausted, flow still asking; transcript healthy through node 1-b |
