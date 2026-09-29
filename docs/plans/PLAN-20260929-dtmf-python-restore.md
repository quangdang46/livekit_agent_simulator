# Plan — Restore DTMF keypad support to the Python `lks` port

> Implements: a `dtmf` execution branch in `caller_contract`, a `DtmfPublisher` seam, and one shipped template. Research: this document. **Status (2026-09-29): research complete, design sealed, not implemented.**
>
> Research method: 6-agent multi-modal recon, 3 independent designs, 4 adversarial lenses each, completeness critic. 23 agents, 0 errors. Every claim below was re-verified against the working tree by the author after the research returned.

## Summary (read this first)

- **You asked:** restore DTMF keypad support to the Python `lks`, and produce a plan you could hand to an implementer.
- **What is going on:** `lks` (Python) parses `- dtmf: "1"` in a scenario's `caller_steps`, then drops it on the floor. `caller_contract/driver.py:592` has no `dtmf` branch, so the action falls through to a generic `contract.control` event and no tone ever reaches LiveKit. The Rust port `lksr` still has this capability because it was ported before the Python engine was removed. Any Windows user is stuck: `lksr` ships no Windows release asset.
- **We recommend:** add the one missing wire call, in the contract path, behind a narrow publisher seam, and fail loudly when the topology cannot deliver. Do **not** resurrect `ScriptRunner`.
- **The finding that changed the design:** the highest-scoring design published from `bridge.room`. That is the *sim* room, and in 3 of 5 caller modes the agent is in a different LiveKit room. A `SipDTMF` packet is room-scoped, so those modes would emit a green event for a tone that reached nobody. No challenge lens asked where the agent actually was; the synthesis step caught it.
- **Status:** sealed. Implementation is 9 steps, estimated small-to-medium, fully offline-testable. Blocked on the three decisions in [§9](#9-decisions-needed-from-a-human).

## 1. The defect

`caller_steps` supports a `dtmf:` key end to end except for the last step:

| Layer | File | State |
|---|---|---|
| Parse + validate | `caller_contract/dsl.py:423-427` | works |
| Classify | `caller_contract/interaction_planner.py:118` | works, but unwired (see §7) |
| **Execute** | `caller_contract/driver.py` | **missing** |
| Verify / report | `script/verify.py:268`, `script/summary.py:16-21` | already counts `sim.script.dtmf` |

The driver enumerates five `action.kind ==` branches (`say` :358, `do` :445, `wait` :462, `play_audio` :467, `interrupt` :546). `dtmf` and `silence` fall past all of them into:

```python
# driver.py:592-593
# dtmf / silence: control actions, never AI/TTS.
_emit("contract.control", {"kind": action.kind, "line": action.line_no})
```

`silence` is correct there. `dtmf` is the defect: a documented authoring verb that parses cleanly, runs, and produces a successful `DriverResult(ended_by=SCENARIO)` having done nothing.

A test pins this behaviour. `tests/test_contract_live_wiring.py:685` `test_wait_and_dtmf_never_publish` asserts `urlopen` was never called for `[{wait: 10}, {dtmf: "123"}, {end: true}]`. Its `wait` half is right; its `dtmf` half encodes the bug.

## 2. Why "document `lksr`-only" was rejected

It was on the table and it fails on three counts:

- `install.sh:10,24` ships `lks` (`BINARY_NAME="lks"`, `RUST=0`); `install.ps1:21` defaults to `lks` and offers `lksr` only behind `-Rust`; `pyproject.toml:33-34` marks `lks` as the primary console script.
- `lksr` has no Windows build at all. `install.ps1:122` throws, and `rust-release.yml:31-33` states plainly: *"lksr is Linux/macOS-only... Windows users: use Python `lks`."*
- A Windows binary can be built by hand (this repo has one at `debug/release/lksr.exe`), but it embeds CPython through pyo3 and exits `127` with `python312.dll: cannot open shared object file` unless the interpreter directory is on `PATH`. That is not an install story.

So the majority of Windows users run Python, and Python cannot press a key.

## 3. The finding that drives the design: rooms are not shared

`SimLegHandle` (`livekit/sim_leg/protocol.py:24-32`) carries **two** rooms, and `run_orchestrator.py:366` builds the caller bridge with `room=leg_handle.sim_room`.

| mode | sim_room | agent_room | shared | anchor |
|---|---|---|---|---|
| `webrtc_sim` | `dispatch.room_name` | `dispatch.room_name` | yes | `webrtc.py:40-41` |
| `outbound_human_pickup` | `agent_room_name` | `dispatch.room_name` | yes | `human_pickup.py:159` |
| `inbound_sip` | `lks-sip-{run_id}` | resolved, separate | **no** | `inbound.py:36-37,158,197-198` |
| `outbound_sim_callee` | `lks-sip-{run_id}` | `room_name_for_run` | **no** | `sim_callee.py:34-35` |
| `agent_dials` | `lks-sip-{run_id}` | `room_name_for_run` | **no** | `agent_dials.py:18-19` |

`publish_dtmf` sends a `PublishSipDtmfRequest` data packet (`livekit/rtc/participant.py:302-316`). The Python API exposes no destination identities, so it is always a room broadcast. In the three two-room modes, publishing from the sim room reaches the observer and nothing else.

The agent room is not a usable substitute. `Observer` does hold it (`observer.py:96`), and `observer` is already positional argument 3 of `run_contract_driver_path` (`live_wiring.py:302`). But the observer connects as `lks-obs-{run_id[:8]}` (`inbound.py:183`, `agent_dials.py:63`, `sim_callee.py:87`), and the live consumer filters on sender identity:

```ts
// voice-ai-agent/src/session/attach-flow-runtime.ts:729
if (participant.identity !== options.getCallerIdentity()) return;
```

`getCallerIdentity()` returns the first remote participant (`attach-room-participant.ts:39-40`), which in `inbound_sip` is deterministically the SIP participant, awaited at `inbound.py:141-150` **before** the observer joins at `:183`. Observer-room DTMF is dropped silently.

**Consequence:** there is no publisher that is both in the right room and correctly attributed, in the two-room modes. The correct response is to fail fast and say why, not to publish into the void.

## 4. Design

One new module, one branch in an always-live loop, one shipped template. No new authoring syntax, no config knob, no CLI flag.

### Step 1 — new module `caller_contract/dtmf.py`

```python
DTMF_PAUSE_MS = 120   # 'w'
DTMF_GAP_MS = 150     # after each published tone
PUBLISH_TIMEOUT_S = 5.0

DTMF_CODES = {"0": 0, ..., "9": 9, "*": 10, "#": 11}   # LiveKit's map, NOT RFC 4733


@dataclass(frozen=True)
class DtmfResult:
    digits: str
    published: int
    error: str | None


class DtmfPublisher(Protocol):
    async def publish(self, digits: str) -> DtmfResult: ...


class RoomDtmfPublisher:
    def __init__(self, participant: rtc.LocalParticipant) -> None: ...
    async def publish(self, digits: str) -> DtmfResult: ...
```

`publish` walks the string: `'w'` sleeps 120 ms and continues; a mapped char is published then followed by a 150 ms gap; an unknown char records `error=f"unknown DTMF char {ch!r}"` and stops. Any exception, including `TimeoutError`, is captured into `error` and stops the loop.

**The `asyncio.wait_for` wrapper is not optional.** The SDK overrides `Queue.wait_for` (`livekit/rtc/_utils.py:103`) and drops the timeout parameter; `publish_dtmf` awaits it with no cancel path (`participant.py:320-328`). A dead transport therefore hangs `_run_actions` indefinitely, and because `_on_hold_timeout` calls `bridge.sim_hang_up()` without cancelling the driver task, the `finally: bridge.stop()` at `run_orchestrator.py:430-431` never runs and the run never finalizes. Bounded is the difference between a failed run and a wedged process.

### Step 2 — `caller_contract/driver.py`: one branch

Replace the silent fall-through at `:592-593` with an explicit `dtmf` branch, placed with the other control branches (after `play_audio` at `:467`):

- **Gate on trigger**, reusing `_wait_trigger` (`driver.py:1239`) the same way `say` and `play_audio` do. If it does not fire, `continue`.
- `result = await self.dtmf.publish(action.dtmf_digits or "")`.
- Emit **`sim.script.dtmf`** with `{digits, published, error}`.
- If `published == 0 and result.error`, `return self._fail(FailureReason.TRANSPORT_ERROR, f"dtmf publish failed: {result.error}", EndedBy.TRANSPORT, ...)`.
- **`silence`** keeps emitting `contract.control`.
- **Anything else** returns `self._fail(FailureReason.VALIDATION_ERROR, f"unhandled caller action {action.kind!r}", EndedBy.ERROR, ...)`.

That last bullet is the durable part. The bare fall-through is exactly the shape that let a known verb become a no-op; closing it means the next addition to `_KNOWN_ACTION_KINDS` fails loudly instead of silently.

Inject the publisher as a `run()` keyword beside `assets` (`driver.py:213-215`), matching the sibling seam at `live_wiring.py:460,497`.

### Step 3 — event name: `sim.script.dtmf`

Three reasons, in order of weight:

- The live consumer filters on sender identity:

  ```ts
  // voice-ai-agent/src/session/attach-flow-runtime.ts:729
  if (participant.identity !== options.getCallerIdentity()) return;
  ```

- `run_orchestrator.py:565-568` skips `script_verify` for contract scenarios regardless of which kinds are emitted, so "the contract path must use `contract.*` vocabulary" is not actually true.
- `script/summary.py:16-21` already counts `sim.script.dtmf`, and `build_caller_behavior_summary` runs unconditionally, so `dtmf_fired` and `script_cues_fired` come alive with no new reader. A bespoke `contract.dtmf` kind would be read by nothing.

The Rust port's contract path already emits `sim.script.dtmf` (`lks-livekit/src/script.rs:534`) for the same authored step. Same scenario, same event name, both ports.

### Step 4 — `caller_contract/live_wiring.py`: construction and the room gate

`run_contract_driver_path` already receives `observer` (`:302`) and `bridge` (`:303`). Build the publisher **above** the driver constructor at `:347`, not beside the sink at `:362`, because the constructor runs first.

```python
# A tone must be published by the participant the agent perceives as the
# caller, which requires one shared room. In the SIP legs the sim and the
# agent sit in different LiveKit rooms and a data packet cannot cross.
_shared = getattr(bridge, "room", None) is not None and bridge.room.name == observer.room.name
dtmf_pub = RoomDtmfPublisher(bridge.room.local_participant) if _shared else None
```

When `_shared` is false the driver has no publisher, and the `dtmf` branch fails with `TRANSPORT_ERROR` naming the two-room topology. Fail-fast, not a silent stub: AGENTS.md forbids half-implemented backends.

Two details that will bite otherwise:

- Access `room.local_participant` **directly**. `getattr(room, "local_participant", None)` is not a safety net: the property raises a bare `Exception("cannot access local participant before connecting")` (`room.py:224`), which `getattr` does not swallow.
- Declare `room: rtc.Room` on the `CallerBridge` Protocol (`callers/base.py`) so `.room` is in-contract rather than duck-typed.

### Step 5 — `caller_contract/dsl.py`: allow `trigger` on `dtmf`

`dsl.py:415-422` rejects `trigger`/`barge_in` for `("dtmf", "wait")` with a message that no longer matches the implementation. `CallerAction` already carries `trigger` (`:166`) and `_wait_trigger` already serves `say` and `play_audio`. Lift `dtmf` out of the reject tuple; it is a two-line deletion.

This preserves the only historical behaviour that made DTMF work. `demo/dtmf-feature/.agent-sim/reports/058-press-4-…/events.jsonl` ran `trigger: agent_speaking`. Without it the tone fires at t=0, before the agent's menu prompt, and the demo reads as broken.

### Step 6 — ship a tracked, gated template

`templates/examples/dtmf-ivr-menu.yaml`, `caller_steps` only:

```yaml
caller_steps:
  - do: {behavior: greet, expect: greeting}
  - wait: {seconds: 2}
  - dtmf: "1w2w3w#"
  - wait: {seconds: 3}
  - end: true
```

`templates/` is force-included into the wheel (`pyproject.toml:51-52`) and `tests/test_contract_all_templates.py::_all_templates()` globs the examples directory, so the existing CI gate parses it.

The `wait` after the tones is load-bearing, not padding: `- dtmf:` followed immediately by `- end:` hangs up before the agent can react, so the first real run would look broken even though every tone landed.

Delete `templates/examples/ivr-pin-dtmf.jsonl`, which is the example this replaces. Do **not** sweep the other `.jsonl` files in that directory; 17 sit there and the `amd-*` three are pinned by `tests/test_amd_templates.py`. That belongs in its own commit.

### Step 7 — delete `InteractionPlanner.plan_dtmf`

`interaction_planner.py:118` has zero `src/` callers. Its only two are `tests/test_interaction_planner.py:111` and `tests/test_parity_vectors.py:375`. After step 2 it becomes a second, provably-unwired representation of a keypress sitting next to the live one.

Delete the method, both test callers, and the `dtmf` case in `tests/fixtures/parity/interaction_planner.json`, with the paired removal in `lks-core/src/caller_contract.rs:1678` and its sole caller at `:2561`.

### Step 8 — `authoring.py`: fix the inverted warning

`authoring.py:308-319` filters `script_steps` for `action == "dtmf"` and warns *"sim can send; many agents only parse spoken digits."* That surface is the inert legacy one. The live surface, `caller_steps: - dtmf:`, gets no warning at all.

Move the filter to `CallerAction.kind == "dtmf"`, or delete it. Do not leave a promise attached to a surface that cannot keep it.

### Step 9 — docs

- `docs/contract-caller-wiring.md:61` — `dtmf` is no longer "planner control only".
- `docs/guide/installation.md:619` — the `ivr-pin-dtmf` / `action: dtmf digits` line is now wrong.
- `docs/behavior-dsl.md` — add one line: a keypress appears in `events.jsonl` as `sim.script.dtmf`, and **not** as a report marker.
- In the `dtmf.py` docstring, state what the event proves: tones were **submitted to the local participant**, not that an agent received them. The server excludes the sender from fan-out, so the sim structurally cannot observe its own tone. The only in-repo delivery evidence is the agent's own reaction (`demo/dtmf-feature/agent/dtmf_agent.py:120`).
- Note that `DTMF_CODES` is LiveKit's map (`#` to 11), not RFC 4733 (`#` to 15). A real SIP caller pressing `#` yields 15, and `livekit/sip` ignores `code` on the room-to-phone leg. An agent asserting on `SipDTMF.code` rather than `.digit` behaves differently under simulation.

## 5. The named user flow

AGENTS.md's no-dead-features rule requires a concrete user for every line shipped. Here it is:

**`templates/examples/dtmf-ivr-menu.yaml` is the keeper.** It is in the wheel, parsed by the CI gate, and spot-driven by a new test asserting a `sim.script.dtmf` event carrying the expected digits. Delete the driver branch and that test goes red on the template. This is the tripwire commit 665ec8c never had.

**Consumer demand is real, and checked directly.** Flows live in the consumer's database, not in its repo, which is why a code-only search misses them. Querying the local `voice_ai_local` database:

```sql
SELECT f."agentId", a.name, count(*) AS dtmf_nodes
FROM "FlowNode" n
JOIN "AgentFlow" f ON f.id = n."flowId"
JOIN "Agent"  a ON a.id = f."agentId"
WHERE n.type = 'DTMF_INPUT'
GROUP BY 1, 2;
```

```
 agent_g9srhevzdanmpue372yfgzi7 | イーブロード                        | 1
 agent_w3ph2c35kisb1ibqythi571j | イーブロード (3-Cụm Macro-Node…)   | 1
```

One of these (`agent_w3ph2c35kisb1ibqythi571j`) has a `single_key` node accepting `["1","2","3"]`, writing to `dtmf_value`, with four `DTMF`-triggered transitions routing on `{{dtmf_value}} == "1" | "2" | "3"` plus a `dtmf_timeout` handle. This is a **regression fix**, not a speculative feature.

What is *not* a justification: archived runs `058`/`063`, which show `error: null` in `events.jsonl`. Those prove an FFI round-trip completed, and both ran in `webrtc_sim`, the one mode where the room is shared. They are not delivery evidence.

## 6. Test plan

All offline. Fakes are per-file, as in `tests/test_contract_live_wiring.py`.

### `tests/test_contract_dtmf.py` (new)

Drives the real `ContractCallerDriver` against a fake participant.

1. `RoomDtmfPublisher` maps `"1w2#"` to `[(1,"1"), (2,"2"), (11,"#")]`, and `'w'` produces no tone. Reuses the assertion from the deleted `test_script_dtmf_runtime.py:129-138`.
2. A `dtmf` step pushes **no** PCM. This is the original 764dc3a regression. It inverts `test_contract_live_wiring.py:685`, so rename that test to `test_wait_never_publish`, keep its `wait` half, and move the `dtmf` half here.
3. A fake whose `publish_dtmf` awaits forever drives the driver to `TRANSPORT_ERROR` within `PUBLISH_TIMEOUT_S`, and does not hang. Without this the timeout ships untested and the suite is green while the wedge ships.
4. A `publish_dtmf` raising `PublishDTMFError` fails the step loudly.
5. Two-room gate: with no publisher, a `dtmf` step fails with the named topology message, and `silence` still emits `contract.control`.
6. `trigger: agent_speaking` on a `dtmf` step waits for the agent. Guards the step 5 deletion.

### `tests/test_contract_all_templates.py` (modify)

Add a spot-drive of the new template through the real driver with a fake publisher, asserting a `sim.script.dtmf` event with `digits == "1w2w3w#"`.

Assert the **event**, not merely that a publisher was injected. The existing `test_every_template_has_caller_steps` (`:46-56`) only checks `caller_actions` is non-empty and never touches the driver, so deleting the branch leaves it green. That test is not the gate. This one is.

### `tests/test_behavior_dsl.py` (modify)

`parse_step({"dtmf": "1", "trigger": {...}})` no longer raises.

### Negative check

Comment out the new `dtmf` branch. Confirm `tests/test_contract_dtmf.py` and the new spot-drive go red, and that the template still parses, proving the parse-only gate is not the keeper. Revert.

### Baseline

`uv run --extra dev pytest -q` against the current suite before any edit. On Windows, if `uv sync` fails because the MCP exe is locked, use `.venv\Scripts\python.exe -m pytest -q`.

## 7. Rust parity

The wire capability already exists in Rust on the contract path, via the projection from caller steps onto script steps. This change converges them.

| Concern | Action |
|---|---|
| Event name | None. `lks-livekit/src/script.rs:534` already emits `sim.script.dtmf`. |
| `plan_dtmf` removal | Required in the same commit: `lks-core/src/caller_contract.rs:1678`, its sole caller at `:2561`, and the `dtmf` case in `tests/fixtures/parity/interaction_planner.json`. Rust enumerates top-level fixtures from a hardcoded `VALIDATOR_VECTOR_FILES` at `caller_contract.rs:2153`. |
| Charset | Deliberately untouched. `dsl.py:423-427` and `caller_dsl.rs:508-524` both accept any non-empty string. Tightening Python alone breaks the shared-fixture harness (`tests/test_dsl_parity_vectors.py:6-8`: *"must accept/reject the SAME steps"*). Unknown characters are handled at publish time into the event's `error` field. Separate ticket, two-sided. |

**Known pre-existing divergence, note and do not fix here.** Python `summary.py:16-21` counts four `sim.script.*` kinds into `script_cues_fired`; Rust `summary.rs:23-26` filters on `sim.script.cue` only. The same scenario reports `1` under `lks` and `0` under `lksr`. Emitting the shared event name makes this visible instead of hidden. Aligning them is a separate small PR. See decision §9.3.

## 8. Do not

- **Do not resurrect `script/runtime.py` or any `ScriptRunner`.** It was deleted for a reason, and `run_orchestrator.py:399-411` makes the contract path the only caller path.
- **Do not publish from `observer.room`.** It reaches the agent in all five modes and is dropped by all five, because the sender identity is `lks-obs-*`.
- **Do not publish from `bridge.room` without the room gate.** Three of five modes turn it into a green no-op.
- **Do not add `publish_dtmf` to the `CallersBridge` or `PublishSink` Protocol** to avoid growing test doubles. That trades a mechanical test edit for optional-hook dead surface, which is the exact shape commit 665ec8c deleted.
- **Do not add a `DtmfSender` Protocol module** beyond `dtmf.py`. A `Callable[[str], Awaitable[DtmfResult]]` beside the existing `emit` injection is this repo's idiom.
- **Do not add a `writer` field to the publisher.** The driver already emits the full result. `BridgePublishSink.writer` is load-bearing (`publish_sink.py:63,79`); a publisher's would not be.
- **Do not add a web marker.** `MARKER_DTMF` (`web/report_time.py:21`) is reachable only from a `sim.script.cue` spec's `interrupt_class` (`web/markers.py:167,186`), and `lks-web/src/lib.rs` has zero `dtmf` hits. Both ports render no chip today. Adding one means touching `web/src` types, `markers.py`, **and** the Rust web crate. Document instead.
- **Do not add a config knob, MCP tool, or CLI flag.** The `dtmf:` step is the entire API.
- **Do not touch `demo/`** in this commit.
- **Do not make `plan_dtmf` live** by routing the new branch through the planner. It has no timing, no pacing, and no test beyond a classification assertion.

## 9. Decisions needed from a human

**9.1 The two-room gap is made loud, not solved.** Real DTMF is a PSTN use case, and PSTN is exactly the topology this plan refuses to work in. Supporting it needs a participant in the agent room that the agent perceives as the caller, which is a server-side or SIP-leg change rather than a client one. Options: (a) accept shared-room-only for now, recommended; (b) authorise real plumbing, a dedicated caller-identity participant in the agent room; (c) ask voice-ai-agent to relax `attach-flow-runtime.ts:729` to accept any sender. Recommendation is (a) plus a bead for (b) and (c). Do not read the fail-fast as "done".

**9.2 Framing.** Consumer demand is resolved (§5), but the project may still want this framed as a regression fix against runs 058/063 rather than a new feature. Author's position: it is a regression, since the Python port had the capability and lost it.

**9.3 `script_cues_fired` cross-port divergence.** Align Rust's `summary.rs` up to the four-kind set, or align Python down to `sim.script.cue`? It is a machine-readable `summary.json` field, so the choice is visible to consumers. Pick deliberately.

## 10. Preconditions

**Revert `c0d3e26` before the verification step is meaningful.** The A/B/C mutants are live at HEAD: `live_wiring.py:58` raises `f"MUTANTA {reason} MUTANTB {detail}"` from `ContractDriverFailure.__init__`, and `live_wiring.py:539-543` maps `EndedBy` values to `MUTNTC_*` strings. Every failure-path assertion in the new test file lands on that path. Until it is reverted, a green suite cannot be interpreted.

## 11. Definition of done

1. `c0d3e26` reverted; baseline suite green and its count recorded.
2. Steps 1 through 9 implemented, each with a `file:line` reference in the commit body.
3. `tests/test_contract_dtmf.py` and the template spot-drive pass.
4. Negative check performed and reverted.
5. Full suite green; the delta equals the tests added plus the ones deliberately removed.
6. Rust change in step 7 landed, `cargo test --workspace` green.
7. Decisions §9.1 through §9.3 recorded in this document with the answers filled in.

## Appendix — rejected designs

Kept so a future reader does not re-run the same analysis.

**Design 2, bridge keypad capability gated by the action.** Scored 6.5, failed all four lenses. It makes the capability optional three times over: `publish_dtmf` required on the Protocol, then `getattr` tolerance, then `| None = None`, then a `KeypadError('bridge has no publish_dtmf capability')` branch. All four are unreachable in production because both bridges implement the method in the same change. That is a re-creation of the optional-hook dead surface. It also left the room bug unaddressed and the `publish_dtmf` await unbounded.

**Design 3, one surface, delete `script: action=dtmf` outright.** Scored 5.8, failed all four lenses. Deleting the legacy `script:` DTMF surface while Rust's contract path still projects caller steps onto it (`lks-core/src/scenario.rs:805-808` to `:446-454` to `ScriptRuntime`) widens the divergence it claims to close. It would also hard-parse-error a shipped template (`templates/examples/ivr-pin-dtmf.jsonl`) that the CI gate cannot catch, because the gate globs `*.yaml` only.
