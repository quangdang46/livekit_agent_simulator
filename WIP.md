# WIP — gaps toward replacing a developer talking to the agent

Goal: **`lk-sim` replaces manual “open mic and chat with the voice agent”** for day-to-day QA — full-stack LiveKit room, black-box agent, forensic report + web replay.

Not the goal (different products): LiveKit **text-only** unit tests inside agent code; 50k concurrent load platforms; full production observability SaaS.

Last research pass: industry (LiveKit testing docs, Hamming, Coval, Vapi Simulations, FutureAGI, Cekura) + current package surface (`lk-sim` / MCP ops).

---

## Already in good shape

| Capability | Notes |
|---|---|
| Black-box room + dispatch | Opaque metadata; any LiveKit agent |
| Gemini Live sim caller | Persona / Context / Execute |
| Scripted cues | `agent_speaking`, `gemini_text` / `room_pcm` |
| Forensic log | `events.jsonl`, timeline, summary, SQLite |
| Optional LLM judge | `PassCriteria` |
| Local stereo audio | `conversation.wav` (L=sim, R=agent) |
| Web replay | `lk-sim web` — play audio + highlight transcript |
| Batch + compare | `execute-all`, `compare` |
| Scaffold / guide | `scenario-init` (`//` comments), `guide`, `install.sh` |
| CLI ↔ MCP parity | Single `ops` surface |

Roughly: **happy path + some scripted edges + listen-back** ≈ most of what a developer does in one manual call.

---

## P0 — “Talk more like a real person”

Highest impact if the bar is *replace a developer on the call*.

| Gap | Status | Notes |
|---|---|---|
| **Diverse personas** | **Partial** | `Persona.traits[]`, `Persona.language` override; full accent/noise matrix still open |
| **Interruption / barge-in** | **Done (v1)** | Script `barge_in` / `agent_speaking` + `min_interruptions` verify |
| **Silence / “user gone”** | **Done (v1)** | Script `trigger: silence` + `action: wait` + `min_agent_finals_after_silence` |
| **Outcome-based pass** | **Done (v1)** | `Assert.outcomes` (`transcript_contains`, `llm_bool` → judge) |
| **Hard tool assertions** | **Done (v1)** | `Assert.tools` name / min_count / `args_contains` |

Also: Script `trigger: time`, `action: speak|wait`; transcript phrase asserts.

---

## P1 — Daily QA / regression loop

| Gap | Why it matters | Candidate work |
|---|---|---|
| **Promote failure → golden scenario** | Capture bad prod/manual call as permanent test | “Export run → scenario” from transcript (+ optional audio cues) |
| **Suite report + CI gate** | Ship only if suite green | Suite summary matrix, baseline, non-zero exit / JUnit-friendly output (`execute-all` exists; reporting thin) |
| **Voice metrics aggregates** | Dev “feels” latency / awkward turns | TTFW, turn-taking, barge-in rate from events → summary / web |
| **Flake control** | LLM nondeterminism | N iterations, pass@k, optional seed policy |
| **Text-fast mode** | Cheap iteration before full voice | Optional chat/text sim path, then voice full (Vapi-style) |

---

## P2 — Scale / production-adjacent (not required to replace *one* developer)

| Gap | Notes |
|---|---|
| Concurrent / load rooms | Hamming / `lk` load territory |
| SIP / telephony path | Real phone numbers; lk-sim is room/web today |
| Prod import / shadow replay | Enterprise eval platforms |
| Auto-gen scenarios from prompt | Nice-to-have, not core “talk to agent” |
| Multi-party / multi-agent handoff | LiveKit multi-agent workflows |

---

## Developer manual work vs lk-sim

| Manual developer | lk-sim today |
|---|---|
| Open mic, greet agent | ✅ execute + persona |
| One happy-path flow | ✅ scenario |
| Barge-in, “uh-huh”, silence | ⚠️ Script partial — need behavior library |
| Switch language / voice | ⚠️ config — no scenario matrix |
| Check tool calls | ⚠️ observe patterns — need hard asserts |
| Re-listen to call | ✅ wav + `lk-sim web` |
| Compare before/after change | ⚠️ `compare` — no golden baseline |
| Run 20 cases before ship | ⚠️ `execute-all` — thin suite report / flake |
| Call real SIP number | ❌ |
| Load 100 concurrent | ❌ |

---

## Suggested implementation order

1. **Behavioral richness** — silence, barge-in, noise/persona matrix  
2. **Hard assertions** — tools + structured outcomes (+ soft judge)  
3. **Regression loop** — fail → scenario; suite + N-iter + CI  
4. **Metrics** — latency/turn aggregates (web already does playback)  
5. **Later** — text-fast mode; SIP optional  

Keep portable: no consumer-specific keys in `src/`; extension via scenario / config / plugins (see `AGENTS.md`).

---

## Explicit non-goals (for now)

- Replacing LiveKit **in-process** text unit tests  
- Full Hamming/Coval-class production monitoring SaaS  
- 50k concurrent simulation  
- Fitting one monorepo’s business domain into core  

---

## Status

| Area | Status |
|---|---|
| Core sim + report + web replay | Done (usable) |
| P0 behavior + structured pass | **v1 shipped** (traits, silence/barge Script, Assert tools/transcript/outcomes) |
| P0 remaining | Persona noise/accent library; richer interrupt recovery asserts |
| P1 regression / CI suite | Open |
| P2 load / SIP / prod import | Deferred |

Update this file when gaps close or priorities change.
