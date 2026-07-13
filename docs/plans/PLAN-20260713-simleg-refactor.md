# Plan Report — SimLeg / telephony post-ship refactor (clean + extend)

**ID:** PLAN-20260713-simleg-refactor  
**Date:** 2026-07-13  
**Repo:** livekit-agent-simulator (lk-sim)  
**Status:** Waiting for OK — reply **go ahead** to implement **R0 + R1** first  

---

## 0. Summary (read this first)

| | |
|---|---|
| **You asked** | After shipping telephony (RTC / inbound / outbound), is the source easy to understand, clean, and extend? If not, plan a refactor. |
| **What is going on** | Architecture matches the plan (**Template Method + Strategy + Factory**). Implementation grew hard edges: long functions, multi-phase room discovery, dual-room Gemini feed, player cue heuristics. Score ~**6.5/10** (architecture 8, hygiene 5.5). |
| **We recommend** | **Refactor in place — keep patterns, shrink modules.** Do **not** rewrite orchestrator or invent new frameworks. Split legs, extract room resolvers, thin orchestrator, isolate cue heuristics, add unit tests for discovery/listen seams. |
| **Risk** | **Low–Med** if phased with unit tests first. **High** only if big-bang rewrite of `run_scenario_instance` + SIP legs together. |
| **Not in scope** | T6 RTP vendor; product-specific worker fixes; new Caller modes; changing scenario JSONL contract (unless additive). |

---

## 1. Goals (locked)

1. **Preserve** Template Method + Strategy + Factory + `SimLegHandle` normalization.
2. **Preserve** portable core (no consumer keys in `src/`; opaque `Dispatch.metadata`).
3. **Preserve** external behavior: WebRTC default, inbound/outbound SIP, suite gate, `--parallel`, forensic artifacts.
4. **Make change cheap:** typical bugfix ≤ 1–2 files; new mode = new leg module + factory line.
5. **No behavior change** in R0–R2 unless a test proves a bug; R3+ may fix known edge cases with tests first.
6. **Keep CI green** (`pytest -q`) after every phase; SIP E2E remains manual/local.

### Non-goals

- Replacing LiveKit Cloud hairpin with in-process RTP (T6).
- Redesigning scenario JSONL kinds.
- Perfect multi-STT player sync (only structure heuristics for maintainability).
- Micro-optimizing wall-clock of suite runs.

---

## 2. Evidence (current state)

### 2.1 What already matches the telephony plan

| Pattern | Location | Verdict |
|---|---|---|
| Template Method | `run_orchestrator.py` single pipeline | Keep |
| Strategy | `WebRtcSimLeg` / `InboundSipSimLeg` / `OutboundSipSimLeg` / `AgentDialsSimLeg` | Keep |
| Factory | `sim_leg_factory(mode)` | Keep |
| Adapter | `LiveKitAdapter.create_sip_participant` | Keep |
| Normalized handle | `SimLegHandle` | Keep; enrich slightly in R2 |

### 2.2 Complexity hotspots (approx LOC)

| Symbol | ~LOC | Issue |
|---|---|---|
| `run_scenario_instance` | 360+ | God function — all phases inline |
| `OutboundSipSimLeg.connect` | 180 | Dial + early observer + sim-leg wait + meta |
| `InboundSipSimLeg.connect` | 160 | Dial + A/B/C room resolve + join |
| `find_agent_room` | 120 | Multi-phase discovery hard to unit-test |
| `_build_transcript_cues` | 160 | Stacked heuristics (interim, active speaker, ghost STT) |
| `sim_leg.py` (file) | 586 | Four strategies in one file |
| `ops.execute_scenarios` | ~80 | OK after `--parallel`; leave unless needed |

### 2.3 Production patches that must remain correct

These are **load-bearing** — refactor must preserve behavior:

| Patch | Why |
|---|---|
| Outbound: observer joins agent-room **before** dial | LiveKit does not buffer; late join drops greeting PCM |
| Outbound: short sim-leg wait (~3s), not 15s block | Avoid delaying Gemini/record when no sim DID |
| Gemini `watch_agent_tracks_on_room(agent_room)` when SIP sim-leg missing | Caller otherwise goes silent after first utterance |
| Inbound room A: `agent_room` / template | Deterministic, parallel-safe |
| Inbound room B: `sip_call_id` correlation | Parallel-safe when room is dynamic |
| Observer records agent WAV R-channel on agent-room | Outbound WAV had R=0 when only sim-room listen |
| Player: prefer `sim.gemini` for user; active_speakers for agent windows | Ghost English STT / final-only timestamps |

### 2.4 Related shipped commits (context)

- `0b3fdaf` feat SimLeg telephony  
- `ea3820a` outbound greeting + cue alignment  
- `11da2fc` `--parallel`  
- `214716a` inbound A+B room resolve  

---

## 3. Target design (after refactor)

```text
run_orchestrator.py          # thin Template Method: call phase_* helpers only
livekit/
  sim_leg/
    __init__.py              # re-export factory + handle + protocol
    protocol.py              # SimLeg, SimLegHandle, SimLegContext, SimLegError
    factory.py               # sim_leg_factory
    webrtc.py                # WebRtcSimLeg
    outbound.py              # OutboundSipSimLeg
    inbound.py               # InboundSipSimLeg
    agent_dials.py           # AgentDialsSimLeg (optional keep)
    room_resolve.py          # A deterministic + B sip_call_id (pure-ish helpers)
  adapter.py                 # thin LiveKit API wrappers only (no multi-phase policy)
  observer.py                # observe + agent record (unchanged responsibility)
gemini/live_session.py       # brain; listen hooks stay small
web/
  transcript_cues.py         # orchestration only
  cue_source_priority.py     # ghost / source rank
  cue_windows.py             # active_speakers + interim → start/end
```

### SimLegHandle (R2 enrichment — still mode-agnostic)

```text
SimLegHandle
  agent_room / sim_room / names / identities   # existing
  gemini_listen: enum { agent_identity, sip_on_sim_room, agent_room_webrtc }
  record_agent_from: enum { agent_room, sim_room }  # default agent_room
  meta / rooms_to_delete / disconnect()
```

Orchestrator uses **handle fields only** — delete remaining `if leg_handle.gemini_listen_sip` branches where possible.

---

## 4. Principles

1. **Behavior-preserving first** — golden unit tests before move.
2. **One seam per PR** — prefer small commits (R0, R1a, R1b…).
3. **Policy out of Adapter** — Adapter = RPC; room *policy* lives in `room_resolve` / leg.
4. **No new public scenario kinds** unless required (avoid).
5. **Comments explain *why* production constraints**, not *what* the next line does.
6. **Delete dead paths** when tests cover replacement (no dual implementations).

---

## 5. Implementation phases

### R0 — Safety net (tests only, zero product change)

**Goal:** Lock current behavior so moves are safe.

- [ ] Unit tests: `sim_leg_factory` modes + unknown mode (extend `test_sim_leg.py` if gaps).
- [ ] Unit tests: `find_agent_room` / resolution with **mocked** `list_rooms` + `list_participants`:
  - [ ] A: exact `prefer_name_substr` / dial digits in room name + agent present.
  - [ ] B: `sip_call_id_substr` matches SIP attrs + agent present.
  - [ ] Parallel trap: WebRTC room `lk-sim-smoke-*` with agent only must **not** win when `require_sip=True` and another room has SIP+agent.
  - [ ] Timeout / empty → `AgentJoinTimeout`.
- [ ] Unit tests: outbound leg ordering (mock adapter) — `connect_participant(agent_room)` **before** `create_sip_participant` (spy call order).
- [ ] Fixture-based test: sample `events.jsonl` snippet → `_build_transcript_cues` drops English ghost near `sim.gemini` Vietnamese final; keeps script barge `uh-huh`.
- [ ] `pytest -q` green.

**Accept:** No production file behavior change required; only new/extended tests (+ tiny hooks if needed for testability, e.g. inject list API).

**Risk:** Low.

---

### R1 — Split Strategy modules (structure only)

**Goal:** One leg per file; factory thin; imports stable for callers.

- [ ] Create package `livekit/sim_leg/` (or `livekit/legs/` — pick one name, stick to it).
- [ ] Move without logic change:
  - [ ] `protocol.py` — Protocol, Handle, Context, Error
  - [ ] `factory.py` — `sim_leg_factory`
  - [ ] `webrtc.py` / `outbound.py` / `inbound.py` / `agent_dials.py`
- [ ] Compatibility shim: `livekit/sim_leg.py` re-exports public names **or** update all imports in one commit (prefer **one commit, no long-lived dual path**).
- [ ] Update imports: `run_orchestrator`, tests.
- [ ] `pytest -q` green.

**Accept:** Same public symbols usable; file sizes legs each ≪ 250 LOC target (may still be long until R2).

**Risk:** Low (move-only).

---

### R2 — Thin Template Method + handle-driven listen/record

**Goal:** Orchestrator readable; less mode knowledge outside legs.

- [ ] Extract from `run_scenario_instance`:
  - [ ] `_phase_prepare(...)`
  - [ ] `_phase_connect_leg(...)`  # factory + connect + meta
  - [ ] `_phase_attach_observe_brain(...)`
  - [ ] `_phase_converse(...)`
  - [ ] `_phase_verify_judge(...)`
  - [ ] `_phase_finalize_cleanup(...)`
- [ ] Target: `run_scenario_instance` body ≤ ~80–120 LOC of calls + try/finally.
- [ ] Enrich `SimLegHandle` with explicit listen/record instructions (enums or small dataclass).
- [ ] Legs set handle fields; orchestrator only calls `bridge.watch_*` / observer based on handle.
- [ ] Preserve: early `recorder.mark_start()`, dual-room Gemini feed, multi-room delete.
- [ ] Unit/smoke: existing tests + R0 order tests still green.

**Accept:** New contributor can read orchestrator top-to-bottom without scrolling SIP dial details.

**Risk:** Med — careful with try/finally and cleanup paths.

---

### R3 — Room resolution = explicit A then B (delete fuzzy default)

**Goal:** Parallel-safe inbound without “first agent room” footgun.

- [ ] Split adapter policy into `room_resolve.py` (or methods with clear names):
  - [ ] `resolve_room_deterministic(name) -> (room, agent_id)` — wait agent only.
  - [ ] `resolve_room_by_sip_call_id(sip_call_id, exclude) -> (room, agent_id)`.
  - [ ] Optional: `resolve_room_by_dial_substr(dial_in, require_sip=True)` as weak C.
- [ ] `InboundSipSimLeg`:
  1. If `agent_room` / rendered template → **A only**.
  2. Else if `sip_call_id` from dial → **B**.
  3. Else fail with **actionable** error: set `Telephony.agent_room` or template; do **not** silently pick first agent room under ambiguous multi-room state.
- [ ] Document in `docs/telephony.md`: Direct/Callee rules + template placeholders `{run_id}`, `{dial_in}`, `{number}`.
- [ ] Tests from R0 become the contract; add fail-fast test when multiple agent rooms and no sip_call_id/template.

**Accept:** `--parallel` smoke+inbound cannot attach inbound observe to smoke room; error message tells how to configure A if B cannot match.

**Risk:** Med — may surface misconfigured targets that previously “worked by luck”.

---

### R4 — Player cues: structure over smart pile

**Goal:** Maintainable cue pipeline; same or better sync.

- [ ] Split `transcript_cues.py`:
  - [ ] `cue_source_priority.py` — rank + ghost filter (sim.gemini vs lk.transcription).
  - [ ] `cue_windows.py` — active_speakers merge + interim growth starts.
  - [ ] `transcript_cues.py` — wire steps only.
- [ ] Keep public `build_cues_payload` API stable.
- [ ] Add fixture tests from real anonymized events (outbound ghost English; inbound multi-source).
- [ ] Optional: document known limitation (final-only STT without interim/active_speakers).

**Accept:** `tests/test_cues.py` green; ghost English still dropped; agent windows still use active_speakers when present.

**Risk:** Low–Med (player-only).

---

### R5 — Hygiene / docs (optional same PR train)

- [ ] Mark telephony plan T0–T4 checkboxes done or link to this refactor plan.
- [ ] `docs/telephony.md`: short “Architecture” + “Inbound room resolution A/B”.
- [ ] `AGENTS.md` or GUIDE: “add a SimLeg” 5-line recipe.
- [ ] Grep dead comments / unreachable dual paths; delete.
- [ ] Consider max complexity soft rule in PR checklist (no new 200+ LOC functions without split).

**Risk:** Low.

---

## 6. Out of order / do not do

| Anti-pattern | Why |
|---|---|
| Big-bang rewrite orchestrator + all legs + cues | High regression, hard review |
| New plugin framework for legs | Overkill; Strategy is enough |
| Keep `find_agent_room` “first agent” as silent default under parallel | Reintroduces race |
| Put room policy back into raw Adapter list loops without names | Undoes R3 |
| Change scenario JSONL required fields for refactor alone | Breaks targets |
| “Optimize” by sharing one LiveKit room across parallel scenarios | Cross-talk hell |

---

## 7. Files likely touched

| Phase | Files |
|---|---|
| R0 | `tests/test_room_resolve.py` (new), extend `test_sim_leg.py`, `test_cues.py` |
| R1 | `livekit/sim_leg/**` (new), delete/slim old `sim_leg.py`, import fixes |
| R2 | `run_orchestrator.py`, protocol handle, legs, `live_session.py` watch helpers |
| R3 | `adapter.py`, `room_resolve.py`, `inbound.py`, `docs/telephony.md` |
| R4 | `web/transcript_cues.py` + splits, `tests/test_cues.py` |
| R5 | plans/GUIDE/AGENTS |

---

## 8. Test strategy

| Tier | What |
|---|---|
| **Every phase** | `pytest -q` full suite |
| **R0–R3** | Mock LiveKit list/create/sip — no trunk |
| **After R2/R3** | Manual once: `execute-all --parallel 2 smoke-hello inbound-caller-sim outbound-callee-sim` on a target with worker (optional owner) |
| **R4** | Fixture events only + existing cue tests |

Do not block merge on PSTN cost; optional E2E is confidence, not gate.

---

## 9. Risk matrix

| Risk | Level | Mitigation |
|---|---|---|
| Regress outbound greeting capture | Med | R0 call-order test; manual outbound WAV R early energy |
| Regress inbound parallel room attach | Med | R0 multi-room mock; R3 fail-fast |
| Import cycle after package split | Low | protocol has no imports from legs |
| Cue visual regress | Low | golden fixtures before split |
| Scope creep into T6 / new modes | Med | Explicit non-goals |

---

## 10. Success metrics

- [ ] No production function > ~120 LOC without a named sub-helper (soft).
- [ ] `sim_leg` package: each leg file readable in one screenful of connect steps (ideal).
- [ ] Orchestrator phase list matches comments 1:1.
- [ ] Adding a 5th mode documented in ≤10 lines in GUIDE/AGENTS.
- [ ] `pytest -q` ≥ current count, no skips of existing telephony unit tests.
- [ ] Subjective: “fix room discovery” does not require reading Gemini mixer code.

---

## 11. Suggested PR sequence

1. **PR-R0** tests only  
2. **PR-R1** file split  
3. **PR-R2** orchestrator thin + handle listen fields  
4. **PR-R3** room resolve A/B + docs  
5. **PR-R4** cue module split  
6. **PR-R5** plan/docs hygiene  

Each PR: green CI, description links this plan section.

---

## 12. Open questions (owner)

| # | Question | Default if no answer |
|---|---|---|
| 1 | Package dir name `livekit/sim_leg/` vs `livekit/legs/`? | **`sim_leg/`** (matches type name) |
| 2 | R3: fail hard when ambiguous multi-room and no A/B signal? | **Yes** (prefer loud failure over wrong room) |
| 3 | Shim `livekit/sim_leg.py` for one release? | **No** — single import update commit |
| 4 | Manual parallel E2E required before merge R3? | **No** — mock tests sufficient; E2E nice-to-have |

---

## 13. Next step

Reply **go ahead** to implement **R0 + R1** (tests + split modules, zero intentional behavior change).  
Then R2 → R3 in follow-ups.

---

## Appendix A — Current vs target mental model

**Now (works, hard to change):**

```text
run_scenario_instance [huge]
  → sim_leg_factory → Outbound/Inbound.connect [huge each]
  → special-case listen/record in orchestrator
  → find_agent_room multi-phase
  → transcript_cues mega-function
```

**Target (same patterns, smaller pieces):**

```text
run_scenario_instance
  → phase_connect_leg → factory → leg.connect [short steps]
  → phase_attach from SimLegHandle only
  → room_resolve.A / room_resolve.B
  → cues: priority → windows → build
```

## Appendix B — References

- Architecture plan: `docs/plans/PLAN-20260713-telephony-simleg.md`
- User-facing telephony: `docs/telephony.md`
- LiveKit no-buffer media: late subscriber misses PCM (known platform constraint)
- Parallel suite race: inbound attached to `lk-sim-smoke-*` (fixed direction via A+B; R3 hardens)
