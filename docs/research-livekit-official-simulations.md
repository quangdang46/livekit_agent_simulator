# Research: LiveKit Official Agent Simulations (docs.livekit.io/agents/start/testing/simulations/)

> Research note written 2026-08-06 in a throwaway worktree from `main`. Purpose: learn how
> LiveKit's own simulations work and what LKS can borrow. Not a plan, not a PRD.

---

## 1. What LiveKit ships (beta, Python-only)

`lk agent simulate` runs your agent against an **LLM-driven simulated user** that plays a
scenario start-to-finish, then a **judge** LLM-grades the transcript against the scenario's
`agent_expectations`. Three components:

| Component | Role |
|-----------|------|
| **Simulated user** | LLM follows scenario `instructions` (persona + goal), talks until natural end |
| **Your agent** | Real entrypoint. CLI spawns a local worker, or `--agent-name` targets a running agent |
| **Judge** | LLM grades transcript vs `agent_expectations` → pass/fail + reason |

Runs on **LiveKit Cloud**, in parallel up to the project's concurrency limit. **Text mode by
default** (no STT/TTS); audio mode "isn't available yet" but is the documented north star.

### scenarios.yaml — the unit of iteration

```yaml
name: Room booking
scenarios:
  - label: Book a king room for one night
    instructions: >
      You are Jordan Reyes (email jordan.reyes@example.com, phone 5550142).
      Book a king room for the night of 2026-06-09 ...
    agent_expectations: Room booked successfully
    tags: { feature: room_booking }
    userdata:
      room_type: king
```

Fields: `label` (human name), `instructions` (the simulated user's prompt), `agent_expectations`
(what a pass looks like — be specific), `tags` (group/filter), `userdata` (arbitrary JSON → agent
at runtime, drives mocks + final-state grading).

Two bootstrap paths: `lk agent simulate -n 10` **generates scenarios from your source** (uploads
code to Cloud for the generator, asks first), or `--scenarios scenarios.yaml` for the checked-in,
reproducible, CI-runnable source of truth.

### Hooking into your agent

```python
server = AgentServer()
@server.rtc_session(on_simulation_end=on_simulation_end)
async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    if sim := ctx.simulation_context():        # None in production
        calendar = build_fake_calendar(sim.userdata()["available_slots"])
        mock_tools(MyAgent, build_tool_mocks(calendar), session=session)
    ...
```

- `ctx.simulation_context()` → `SimulationContext | None`, resolved from job attributes
  (`lk.simulator.dispatch`) **before the room connects**, cached.
- `mock_tools(Agent, {...}, session=session)` — LLM still sees **real tool schemas**, only execution
  is intercepted. Dynamic: mocks close over shared state, so booking through one mock changes what
  another mock returns next call. Session-scoped, replaceable at any time, `{}` clears.
- Production path untouched: `simulation_context()` returns `None` → real backends.

### Grade on final state (the important idea)

```python
async def on_simulation_end(ctx: SimulationContext) -> None:
    expected = ctx.userdata().get("expected_state")
    if not expected: return                    # grade on conversation alone
    session = ctx.job_context.primary_session
    if not booking_matches(session.userdata.db, expected):
        ctx.fail(reason="final DB state diverged from the expected booking")
```

- **Two verdicts, ANDed**: `simulator_verdict` (LLM judge of chat, read-only) + `user_verdict`
  (your veto via `ctx.fail()`). Your check **can fail a run the simulator passed but can never
  rescue one** — there is no "pass override".
- Access final agent state via `ctx.job_context.primary_session` (room, session, userdata the agent
  accumulated).
- Pin time-sensitive scenarios: absolute dates in the scenario + pin the agent clock via an env var
  (`HOTEL_TODAY` / `FRONTDESK_NOW`).

### Internals (livekit-agents/livekit/agents/simulation.py)

- `SimulationContext` carries `scenario`, `simulation_mode` (text default), `job_context`,
  `simulator_verdict`, `user_verdict`, `userdata()`, `fail(reason)` (last call wins).
- `simulator_verdict` **raises RuntimeError** if read outside `on_simulation_end` — read-only,
  recorded alongside your verdict.
- `JobContext._on_simulator_disconnected` shuts the job down when the simulator participant leaves
  (guarded by the `lk.simulator` participant attribute, so agent-added legs like SIP don't trigger it).
- Proto types re-exported as canonical: `Scenario`, `ScenarioGroup`, `SimulationRun`,
  `SimulationDispatch`, `SimulationMode`.

### Frontdesk example (the reference)

- `simulation.py` holds **all** simulation glue; `agent.py` stays production-shaped.
- `userdata` drives the whole run: `available_slots` (seed FakeCalendar), `expected_booking`
  (graded in `on_simulation_end`; `null` = must NOT book; omitted = conversation-only),
  `now` (per-scenario clock, default `SIMULATION_NOW` = `2026-06-12T09:00:00`).
- Tool mocks share one `FakeCalendar` → "Booked slot disappears from later listings" scenario works.
- `ToolError` on invalid slot so the LLM self-corrects instead of propagating a hallucination.

---

## 2. Side-by-side with LKS

| Dimension | LiveKit Simulations | LKS (`livekit-agent-simulator`) |
|-----------|--------------------|-------------------------------|
| Test model | **White-box**: agent runs inside, entrypoint branches on `simulation_context()` | **Black-box**: drives a live agent over real WebRTC/SIP, no imports, no agent edits |
| Mock determinism | Yes — `userdata` + `mock_tools` intercept execution, LLM sees real schemas | **No** — cannot touch the agent's tools; relies on `Dispatch.metadata` contract |
| Final-state grading | Yes — `on_simulation_end` reads internal DB/userdata, `ctx.fail()` veto | Partial — asserts check observable transcript/tool/room events only |
| Judge | Single LLM verdict vs `agent_expectations` | Richer: PassCriteria list, judge groups, `all/majority/any`, builtin judges, strict/soft gate |
| Interaction medium | Text by default; audio planned | **Audio-first** (Gemini Live): interruptions, barge-in, silence, VAD, multi-language |
| Scenario format | `scenarios.yaml` (instructions/expectations/tags/userdata) | JSONL kinds: Persona/Context/Simulator/Execute/Dispatch/Caller/Telephony/PassCriteria/Script/Behavior/Plugins/Assert |
| Bootstrapping | `-n 10` generates scenarios from source (Cloud) | `scenario_from_run` promotes a real run → draft JSONL |
| Parallelism | On LiveKit Cloud, concurrency-capped | Local orchestrator, `--parallel` + `--wait` |
| CI gate | Pass/fail per scenario | Suite gate + `pass@k` + baseline `compare` (latency/assert regression) |
| Caller realism | Simulated user = generic LLM turn-taker | Dedicated caller policy (Strategy + Composite SI), mid-call re-ground, Script/Behavior timing |

---

## 3. Lessons LKS can borrow

Ranked by payoff for a black-box audio simulator:

1. **Two-verdict model (LLM + final-state veto), explicitly ANDed.** LiveKit's core insight: "a
   polished conversation can still book the wrong room." LKS already has deterministic asserts as a
   hard gate — that IS the veto. The doc's `on_simulation_end` / `expected_state` pattern maps to
   LKS `Assert`/`ScriptVerify` but is broader because LiveKit can read agent-internal state. **LKS
   gap:** black-box can't see internal DB; a documented `Dispatch.metadata` contract (agent exposes
   a "final state" snapshot) is the closest equivalent. Worth a doc/plan note.

2. **Pin the clock for time-sensitive scenarios.** LiveKit pins the *agent's* clock via env var.
   LKS can't set the agent's env, but it already carries opaque `Dispatch.metadata` to the job —
   the same contract the agent can read to pin `HOTEL_TODAY`. The docs' "absolute dates go stale"
   warning applies directly to LKS scenario authors.

3. **A text/chat transport for fast, deterministic CI.** LiveKit's default text mode covers
   "LLM, tools, and logic without the STT/TTS pipeline." LKS is audio-only today. A `webrtc_text`
   / chat mode would make LKS runs faster/cheaper and cut flake — while keeping audio mode for the
   realism that is LKS's differentiator. This is the single biggest architectural idea here.

4. **Tool-mock semantics are a feature LKS fundamentally can't copy** (black-box). Respect the
   boundary: don't drift toward needing agent edits. The frontdesk "mocks share live state" trick is
   the white-box superpower — LKS's analog is scenario-level fake state injected via metadata, or
   asserts that observe the agent's real tool outputs.

5. **Scenario generation from source** — LiveKit uploads code to Cloud; LKS already has
   `scenario_from_run` (from real behavior). Complementary: a "generate scenarios from the agent's
   actual tool list / README" path could bootstrap a suite. Low priority.

6. **Text-vs-audio is a first-class field.** LiveKit carries `SimulationMode` on the dispatch and
   treats "unspecified → text" for backward compat. LKS treats mode as intrinsic. If LKS ever adds
   text mode, mirror this: per-scenario mode, default text, opt into audio.

## 4. What LKS already does better (keep)

- **Caller realism**: persona goals, freestyle-vs-script hybrid, natural multi-clause speech
  (see [[natural-conversation-approach]]), interruptions, hold/VAD fixtures.
- **Judge richness**: multi-judge groups, builtin judges, `all/majority/any`, strict gating —
  LiveKit's `JudgeGroup` (8 builtin judges: accuracy/coherence/conciseness/handoff/relevancy/safety/
  task_completion/tool_use) is the same concept; LKS's is more structured and CI-integrated.
- **Suite engineering**: pass@k, baseline compare with regression gates, reports + web player,
  transport matrix (WebRTC/inbound/outbound/hairpin). LiveKit's `-n 10` + Cloud dashboard is thinner.

---

## 6. YAML scenario migration (implemented spike → core)

Borrowed the *format* lesson (LiveKit uses `scenarios.yaml` with `|` block scalars
for `instructions`), but **kept the LKS section schema** — the five-field LiveKit
scenario is thin because they are white-box (`mock_tools` + `on_simulation_end` reads
internal state); LKS stays black-box + audio-first, so Script/Assert/PassCriteria stay.

### Decision (from exa research + spike)
- **One scenario per file**, section-object YAML shape — NOT LiveKit's `name:` +
  `scenarios:` group file. LKS scenarios are ~7–13 dense sections; grouping many
  into one file creates giant nested YAML, bad diffs. LKS already has `tags` +
  `execute-all --tag` for grouping.
- **Both `.jsonl` and `.yaml` are supported** on read (`parse_scenario`/`find_scenario`
  handle both; `find_scenario` prefers `.yaml` when both exist).
- **Writers produce YAML**: `init_scenario` scaffolds `<id>.yaml`, `scenario_from_run`
  writes `<id>.yaml`, `init_project` smoke is `smoke-hello.yaml`.
- **New `lks convert <id>`** converts a legacy `.jsonl` → `.yaml` (keeps the `.jsonl`,
  idempotent, `--force` to overwrite).

### YAML gotchas (exa research — pyyaml 1.x)
- `on`/`off`/`yes`/`no` → booleans (Norway problem); leading zeros → octal; `3.10` →
  `3.1`; `22:30` → sexagesimal 1350. **Fix**: quote ambiguous scalars; `safe_load`
  still coerces, so quote + validate. LKS reuses `scenario_from_dict` so type mistakes
  surface as validation errors, but authors must quote e.g. regex `\b4[0-9]{12}...`
  and `4111`/`cvv` in `must_not_phrases`.
- **`agent_expectations` / criteria with colon-space need double quotes** (LiveKit
  writing-scenarios.md).
- Exa sources: YAML gotchas validator, PyYAML docs, yaml/pyyaml#486 (YAML 1.2 core
  schema), AgentV eval-files (YAML canonical, JSONL for streaming/case data),
  OpenStack RBAC JSON→YAML migration (keep both during transition), Gitoza/dev.tools
  (Git diffs as the review contract, deterministic formatting).

### Files changed (spike→core)
- `src/livekit_agent_simulator/scenario_yaml.py` — `load_scenario_yaml`,
  `scenario_to_yaml_text`, `dump_scenario_dict` (ordered keys, drops null/empty for
  readable output, semantic round-trip).
- `src/livekit_agent_simulator/scenario.py` — `parse_scenario` routes `.yaml` via
  loader; `list_scenarios`/`find_scenario` glob both; `find_scenario` prefers `.yaml`.
- `src/livekit_agent_simulator/ops.py` — `init_scenario` → `.yaml`, smoke → `.yaml`,
  `convert_scenario` (new), `scenario_from_run` writer → `.yaml`.
- `src/livekit_agent_simulator/cli.py` — new `lks convert` command; help text.
- `templates/` — 17 example/scaffold/smoke scenarios converted to `.yaml`
  (both formats kept for `.jsonl` legacy).

---

## 7. References

- Docs: https://docs.livekit.io/agents/start/testing/simulations/ (and `/testing/test-framework`)
- Source: livekit-agents/livekit/agents/simulation.py, job.py (`simulation_context`,
  `_on_simulator_disconnected`)
- Example: examples/frontdesk/{simulation.py, scenarios.yaml, agent.py} — the reference for wiring
  `userdata` → mocks → `on_simulation_end` final-state grading
- Requirement: LiveKit CLI ≥2.16.4, livekit-agents ≥1.6.6 (Python), a Cloud project
