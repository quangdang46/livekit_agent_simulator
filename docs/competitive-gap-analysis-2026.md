# lks — What We're Missing vs Comparable Platforms (2026-08 research)

**Date:** 2026-08-11
**Purpose:** Fresh Exa/web research on the closest comparable platforms to check the
`WIP.md` competitive snapshot, verify what `lks` already ships, and rank the gaps
that still matter. Companion to `docs/caller-behavior-research.md` (2026-07-14).

---

## 1. Where lks sits today (verified against `src/`)

`lks` is the **open-source, local-first, forensic-first, MCP-native black-box LiveKit
voice-agent tester**. Its real differentiators vs the SaaS field:

| lks strength | Verified in `src/` |
|---|---|
| Black-box dispatch (agent_name + LiveKit creds only) | `run_orchestrator.py` |
| Real room: WebRTC + inbound SIP + outbound SIP + agent_dials | `Caller.mode`, SimLeg |
| Gemini Live simulated human (not just chat) | `callers/gemini.py` |
| Full forensic log + local report player + stereo WAV | `reports/`, `web/` |
| CLI `lks` ↔ MCP parity over one `ops.py` | `cli.py` / `mcp_server.py` |
| Portable install (no uv/pip), MIT, self-hosted | `install.sh`/`install.ps1` |
| Hard CI gates: status / assert / script / `--baseline` | `asserts.py` / `suite.py` |
| typed interruption classes (`correction/backchannel/noise/dtmf/silence/escalate`) | `script/models.py` |
| DTMF script action (`dtmf`, `w` pauses) | `script/models.py` |
| `constraint_respected` + `backchannel_agent_continued` asserts | `asserts.py` |
| `tool_order` assert | `asserts.py` |
| `silent_mode` / `caller_nudge` | `caller_nudge.py` |
| Interrupt-rate timer (None/Low/Med/High) | `interrupt_rate.py` |
| Multi-judge PassCriteria (`judges[]`, `mode`) | `evals/presets.py` |

Note: `WIP.md` still lists `dtmf`, `P1.F typed interruption classes`,
`constraint_respected`, `tool_order`, and `P1.I` as open. **They are landed.**
The roadmap doc is behind the code.

---

## 2. Who we compared against (2026 research)

| Platform | Kind | Model |
|---|---|---|
| **Hamming** | SaaS, dedicated QA + prod monitoring | 50K+ concurrent, WER/latency/noise, 50+ metrics, accents, prod replay |
| **Coval** | SaaS, simulation + evals | persona ≠ test case, structured knobs, silent mode |
| **Cekura** | SaaS, voice-native testing + monitoring | audio-level accuracy/clarity, load |
| **Bluejay** | SaaS (YC), QA for voice+chat | Digital Humans, **Customer Journeys**, prod replay, load, red-team |
| **Vapi Evals / Retell sim** | platform-native | text-level, near own stack |
| **voicetest** | **open-source** | cross-platform AgentGraph IR, LLM repair loop, `--all` CI |
| **LangWatch Scenario** | **open-source** | code-first `scenario.run()`, real-audio effects, DTMF/interrupt, adapters incl. Gemini Live + OpenAI Realtime |
| **LiveKit Agents sims (beta)** | framework-native | text-only, Cloud parallel, `on_simulation_end` final-state grade |
| **FutureAGI** | SDK | local sim against deployed LiveKit agent, per-speaker WAV + combined, strict eval mapping |

---

## 3. Capability matrix (lks vs field)

✅ shipped · ⚠️ partial / soft / manual · ❌ missing

| Capability | lks | voicetest | LangWatch | Hamming | Coval | Bluejay |
|---|---|---|---|---|---|---|
| Real audio room (not chat-only) | ✅ WebRTC/SIP | ⚠️ off by default | ✅ | ✅ | ✅ | ✅ |
| AI persona caller | ✅ Gemini Live | ✅ | ✅ | ✅ | ✅ | ✅ |
| Typed interruptions (barge class) | ✅ classes | ❌ | ✅ interrupt() | ✅ | ✅ | ✅ |
| DTMF / IVR | ✅ action+assert | ❌ | ✅ `dtmf` step | ✅ | ✅ | ✅ (Digital Human) |
| Background noise / SNR | ⚠️ mixer + cues | ❌ | ✅ effects (cafe/street…) | ✅ | ✅ | ✅ |
| **Codec/network degradation (packet loss, echo, phone_quality)** | ❌ | ❌ | ✅ effects | ⚠️ | ⚠️ | ❌ |
| Silent / dead caller | ✅ `silent_mode` | ❌ | ✅ `silence()` | ✅ | ✅ | ✅ |
| Voicemail / AMD / machine answer | ❌ | ❌ | ❌ | partial | partial | partial |
| **Warm transfer / handoff observe** | ❌ (P2) | ❌ | ⚠️ | ✅ | ✅ | ✅ |
| **Multi-call Customer Journey (identity locked across calls)** | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ Bluejay |
| **Fail → regression / replay production call** | ⚠️ `scenario-from-run` | ✅ replay prod | ✅ | ✅ auto | ✅ | ✅ |
| **Auto-repair failed prompts (LLM fix loop)** | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **Cross-platform agent import/export (AgentGraph IR)** | ❌ (LiveKit-only) | ✅ | ❌ | ❌ | ❌ | ❌ |
| Multi-judge / weighted PassCriteria | ✅ judges+mode | ✅ | ✅ judge criteria | ✅ 50+ | ✅ | ✅ |
| **Audio-native scoring (WER / clarity / jitter from audio channel)** | ⚠️ onset-only (RMS VAD → perceived TTFA / turn audio) | ⚠️ disabled | ⚠️ latency only | ✅ | ✅ | ✅ Cekura/Bluejay |
| Latency hard gates (p50/p95, TTFW, recovery) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Parallel / batch suite | ✅ `execute-all --parallel` | ✅ | ✅ | ✅ 50K | ✅ | ✅ |
| **CI/CD native hook** | ✅ CI friendly + MCP | ✅ GH Actions | ✅ `scenario.run()` in pytest | ✅ | ✅ | ✅ |
| **Production monitoring / alerting** | ❌ (local only) | ❌ | ✅ OTel | ✅ | ⚠️ | ✅ |
| **Load testing (concurrent calls)** | ❌ → `lk perf` | ❌ | ❌ | ✅ 1k–50K | partial | ✅ |
| **MCP server / coding-agent native** | ✅ (10+ tools) | ✅ Claude Code plugin | ✅ (10 tools) | ❌ | ❌ | ❌ |
| Local-first / self-hosted / open source | ✅ MIT | ✅ | ✅ | ❌ | ❌ | ❌ |
| Cross-platform (not LiveKit-only) | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Auth/owner/risk metadata on tests | ⚠️ tags | ✅ | ⚠️ | ✅ | ✅ | ✅ |

---

## 4. The gaps that actually remain

Ranked by ROI for the **lks niche** (local, open-source, forensic, MCP, black-box LiveKit).
The old WIP "DTMF / typed interruptions / constraint_respect" items are **done**; the
live gaps are below.

### Tier 1 — high ROI, fits the niche

> **Closed 2026-08-11:** G2 (handoff observe + asserts), G3 (audio degradation
> effects), and G5 (REST API) shipped as PRs #64 / #63 / #65.

| # | Gap | What it is | Which competitor | Effort |
|---|---|---|---|---|
| G1 | **Voicemail / AMD / machine-answer persona** | Preset: greeting WAV → beep → silence → optional no-chat until timeout; assert leave-msg / hang / tools. LiveKit ships AMD + `EndCallTool`; every telephony agent must survive it. (Draft templates exist: `amd-*`.) | Hamming/Coval/Bluejay partial; nobody open-source | M |
| G4 | **`scenario-from-run` extraction quality + prod-call replay** | Fail→golden exists but is a draft; add "promote a real production call transcript → regression scenario" and improve goal/constraint extraction. | Hamming/voicetest/Bluejay all auto-promote | M |

### Tier 2 — good leverage, later

| # | Gap | What it is | Which competitor | Effort |
|---|---|---|---|---|
| G6 | **Auto-repair loop for failed prompts** | After a failure, propose a fixed persona/assert diff and re-run. Distinctive open-source feature voicetest has. | voicetest | M |
| G7 | **Accent / voice matrix** | Neural accents stay SaaS, but LangWatch does accents via TTS voice selection (`elevenlabs/raj_indian_english`) — lks could expose a `voice` matrix cheaply since Gemini Live has voice variants. | LangWatch/Hamming | S |
| G8 | **`on_simulation_end`-style final-state grade** | LiveKit sims let you validate agent final DB/state, not just conversation. lks black-box cannot see CRM, but a target verify-plugin hook "state matches expected" would close it. | LiveKit sims | S |

### Tier 3 — explicitly not-the-niche (keep out)

| # | Gap | Why skip |
|---|---|---|
| G9 | Production monitoring / alerting / OTel | Hamming/Cekura/Bluejay SaaS; lks is local. Optional OTel export is the only sane slice (WIP P2.F). |
| G10 | Load testing 1k–50K concurrent | Hamming/Cekura/Bluejay; lks explicitly defers to `lk perf`. |
| G11 | Cross-platform agent import/export (voicetest AgentGraph IR) | Requires re-platforming; lks is LiveKit-native by design. Keep LiveKit as the moat. |
| G12 | Multi-call Customer Journeys (locked identity) | Bluejay-only; would need cross-run identity state lks doesn't model. Defer unless a target demands it. |

---

## 5. What the market says about the niche

- **"Text-only testing validates the agent is correct; audio testing validates it's usable."**
  (Hamming LiveKit guide). This is lks's whole reason to exist and still true in 2026.
- **LiveKit's own simulations are text-only today** and explicitly point to third-party
  tools (Hamming, Cekura, Coval, Bluejay) for the audio layer — lks is in that list's
  spirit, minus the SaaS.
- **voicetest is the closest open-source sibling** and the most feature-complete OSS
  competitor: cross-platform IR, LLM repair loop, `--all` CI. Its LiveKit audio-eval is
  *disabled by default* though — lks is more audio-native on LiveKit.
- **LangWatch Scenario is the open-source one to watch for audio realism**: real-audio
  effect pipeline (noise/prosody/quality), `dtmf`/`interrupt`/`silence` script steps,
  Gemini Live + OpenAI Realtime adapters, latency metrics, OTel. Its adapters list
  **LiveKit/Vapi as follow-up**, so there is a window for lks.
- **The "unit-test wrapped in a UI" critique** (Vapi/Retell scripted evals) is exactly
  what lks avoids by being black-box + typed interruptions + hard latency gates.

## 6. Positioning / one-line

> **lks** = the open-source Hamming for LiveKit: real-room black-box audio tests with a
> Gemini human, typed interruptions, DTMF, hard CI latency gates, local forensics, and
> MCP for coding agents — without the SaaS.

The highest-leverage closes are **G1 (voicemail/AMD)**, **G2 (warm transfer)**, and
**G3 (audio degradation effects)** — each is cheap and directly answers "does the agent
hold up like a real call, not a unit test."

---

## 7. Sources (2026-08 research)

- Voice Agent Index — "Best Voice Agent Testing Tools" / "Hamming vs Vapi Evals" (2026-06)
- Vapi blog — "Your Voice Agents Need Tests" / Evals (2025-12)
- Hamming blog — "Voice Agent Testing Platforms 2025 comparison", "Testing LiveKit Voice Agents", "How to Test Voice Agents Built with LiveKit"
- Hamming — "Testing LiveKit Voice Agents: Unit, Scenario, Load & Production" (2026-01)
- LiveKit docs — Testing and evaluation / Agent simulations / Test framework (text-only, `on_simulation_end`, JudgeGroup)
- voicetest.dev + voicetestdev/voicetest GitHub — AgentGraph IR, `voicetest run --agent … --tests … --all`, repair loop, GH Actions, Claude Code plugin
- LangWatch — Scenario GitHub + docs: voice adapters, effects pipeline (`background_noise`, `packet_loss`, `echo`, `phone_quality`, `speaking_fast`…), script steps (`dtmf`, `interrupt`, `silence`, `audio`), capability matrix, Gemini Live/OpenAI Realtime/Twilio/Pipecat/ElevenLabs
- Bland docs — Evals (LLM judge agents, workbench setups, post-call evals)
- Cekura — "Open Source Voice Agent Testing Tools" (VoiceTest vs LangWatch vs Promptfoo vs DeepEval vs Langfuse)
- Bluejay — getbluejay.ai + docs: Digital Humans, Simulations, **Customer Journeys**, telephony inbound/outbound, prod replay, load
- FutureAGI — Simulate Using SDK (live agent, per-speaker WAV + combined, strict eval mapping)
- Internal: `WIP.md` (stale — see §1), `docs/caller-behavior-research.md`
