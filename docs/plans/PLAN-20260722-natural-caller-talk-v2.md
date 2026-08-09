# Plan Report — Natural LKS caller talk v2 (post-smoke)

## Summary (read this first)
- **You asked:** Why suite `user_words_p50` barely moved after `feat/natural-caller-talk` (~9.7 vs ~10), and what a stronger portable fix looks like — deep research, not another shallow SI tweak.
- **What is going on:** v1 correctly retargeted SI length bands, but the smoke metric and the worker path-lab pack **cannot show freestyle lift**. Overall p50 is dominated by Script-forced `say` lines (~median 10 words authored), and after each line cue `silence_after_cue_ms` **mutes** Gemini freestyle for tens of seconds. Style still injects `"short turns"`. Hard suite fails are a separate path/assert problem.
- **We recommend:** **Option A (v2)** — fix the *measurement*, fix the *mute-after-line* gate that contradicts hybrid SI, neutralize style/length conflicts in core, then optional sparse-Script authoring guidance for targets. Defer cascade TTS (Option B) until A is proven on freestyle-only metrics.
- **Risk:** Medium — un-muting between line cues can invent early bye or extra chatter that flakes assert phrases; keep hang_up SI + Script gates; allow `verbosity=quiet` + intentional silence holds for VAD fixtures.
- **Status:** Implemented (core + pytest). Live re-smoke deferred — ask when ready.

---

## Research notes (LOOK)

| Source | Status |
|--------|--------|
| Exa MCP (`user-exa`) | **Unavailable** this session — used WebSearch + WebFetch fallback |
| LiveKit MCP (`user-livekit-docs`) | **Unavailable** — freestyle mute is our bridge code, not LiveKit room API |
| Prior plan | `docs/plans/PLAN-20260722-natural-caller-talk.md` (v1 implemented) |

### Top external findings (URLs)
1. [Coval Personas](https://docs.coval.ai/concepts/personas/overview) — persona ≠ test case; TTS-safe fillers (`um` not `ummm`); silent/interrupt as settings.
2. [Coval Input types](https://docs.coval.ai/concepts/test-sets/input-types) — **Scenario** = improvise toward goal; **Script** = exact lines (no LLM wording). Path labs that wall-to-wall Script will never look “natural” on length metrics.
3. [SIGDIAL 2024 — scripted vs spontaneous feedback](https://aclanthology.org/2024.sigdial-1.38/) — LLM dialogue ≈ scripted; spontaneous speech has more communicative feedback / different length distribution. Measuring scripted turns as “naturalness” is the wrong axis.
4. [Synthetic Users, Real Differences (arXiv)](https://arxiv.org/pdf/2605.02624) — evaluate simulators on **user message length distributions** separately from task success; surface-form length is its own realism dimension.
5. [Gemini Live best practices](https://ai.google.dev/gemini-api/docs/live-api/best-practices) — “keep responses short” is an **agent** guideline; must not be the caller ceiling. SI order persona → rules → guardrails still correct.
6. [τ-bench voice / speech-complexity](https://github.com/sierra-research/tau2-bench/blob/main/src/tau2/voice/README.md) — realism knobs are environment/persona complexity, not stuffing path asserts into the mouth prompt; cascade TTS is a larger architecture (our Option B).
7. [Hamming voice eval metrics](https://hamming.ai/resources/voice-agent-evaluation-metrics-guide) — keep latency/task metrics separate from experience/behavior evidence.

---

## Bug investigation (why p50 did not lift)

### Verified root cause
**Overall `user_words_p50` is not a freestyle-naturalness metric on Script-heavy suites.** It is (approximately) the word-count distribution of Script `say` + STT duplicates, with freestyle largely gated off after cues.

### Hypotheses ranked

| # | Hypothesis | Verdict | Evidence |
|---|------------|---------|----------|
| H1 | Soft metric includes scripted `say`; authored say length ≈ observed p50 | **verified — primary** | `metrics._user_word_counts` counts all deduped `transcript.user.final` (`metrics.py`). Active worker scenarios: Script `say` word median **10.5** (mean 10.5). Suite window `2026-07-22T02:46–03:00` sqlite: **mean p50 = 9.75** (n=20). |
| H2 | After line cues, freestyle is muted by `silence_after_cue_ms` | **verified — primary (runtime)** | `script/runtime.py` after `inject_cue`: `if hold_silence_ms > 0: suppress_persona_output(hold_silence_ms)`. `e2e-flow-happy`: **172s** of post-cue suppress across steps. Wait actions also call `begin_scripted_user_silence`. SI says “continue freestyle until next cue” but audio path blocks it. |
| H3 | Style string still says `short turns` and fights natural band | **verified — secondary** | `StyleTraitsSection` injects `Speaking style: {style}` verbatim. All active e2e/sa personas: `"style":"warm, everyday caller; short turns"`. Built SI for `e2e-flow-happy`: verbosity=`natural` **and** `short turns` present. |
| H4 | Natural band “1–3 clauses” ≈ old “1–2 sentences” for checklist answers | **verified — secondary** | `length_guidance("natural")` still a short-phone band; fillers optional; does not force situational detail. Cannot move median when most finals are fixed `say`. |
| H5 | SA 12–15 proves freestyle lift | **mostly discarded** | SA Script `say` medians often **12–14.5** (`sa-edge-http-only`, `sa-happy-leave`, …). Observed SA p50 cluster 9–15 tracks **longer authored lines**, not proven freestyle. |
| H6 | Freestyle mute-while-Script-*pending* reintroduced | **discarded** | `_allow_persona_room_audio` still returns True when not suppressed/silent (`live_session.py`). Problem is **timed suppress after cue**, not pending-Script mute-all. |
| H7 | Hard suite fails caused sparse speech | **discarded as primary** | Failures in recent runs are path/assert/worker (lookup fail, missing specialist transfer, brittle phrases, incomplete transcripts). Separate from naturalness. |

### Report numbers (citations)
- Suite soft avg ≈ **9.75** words p50 (sqlite runs `2026-07-22T02:46–03:00`, n=20 with metric) — matches user “~9.7”.
- Split: e2e mean p50 **8.83**; sa mean **10.14**.
- Example `055-e2e-flow-hangup-end-…`: overall p50=7; classify vs scenario `say`: **11/12** finals script-matched, **1** natural (STT garble).
- Example `058-e2e-flow-happy-…`: only 4 finals, all ≈ script open/order lines (p50=8).

### Actual SI after v1 (built from worker scenario)
For `e2e-flow-happy` with current core:
- `resolved_verbosity()` → **`natural`**
- Includes `Speak in 1–3 spoken clauses…` + `## NATURAL SPEECH` + milestone-then-freestyle lines
- **Also** includes `Speaking style: warm, everyday caller; short turns`
- Does **not** include old sole `"1-2 sentences"` Role ceiling

v1 SI change is real; it cannot move overall p50 on this pack.

### Suite hard fails (context only)
Treat as **orthogonal** to naturalness. Likely mix of: worker/tool path, brittle `transcript_contains` phrases, incomplete runs, judge noise. Do **not** “fix” by making the caller chatty enough to trip asserts. Track in worker/assert tickets separately.

---

## Feature planning (v2)

### Recommended approach — Option A
Ship three portable core fixes + one metric contract + authoring guidance (target-side optional):

1. **Measure freestyle separately** — soft metric that proves naturalness.
2. **Stop contradicting hybrid SI** — do not long-mute persona after *line* cues by default.
3. **Resolve length conflicts** — style/`short turns` must not fight `verbosity=natural`.
4. **Strengthen natural band only where freestyle can speak** — examples + “answer the question then one detail”, still goal-bound.
5. **Authoring note (portable docs)** — wall-to-wall Script = Coval Script mode (deterministic mouth); natural listening packs use sparse milestones or dialogue Scenario.

### Option B (deferred)
Cascade user-sim (text planner + TTS) à la τ-Voice. Larger surface; revisit only if Option A freestyle metrics + listening still fail.

### Option A′ (target-only, not core)
Thin worker scenarios: remove `short turns`, cut Script to milestones, shorten `silence_after_cue_ms` on speak steps. **Useful follow-up** but does not fix wrong metric or mute-after-line for every consumer.

### Integration points (verified)
| Area | Path | v2 change |
|------|------|-----------|
| Metrics | `metrics.py` | Add `user_words_natural_*` (exclude script_cue / mostly-script say); keep overall `user_words_*` for continuity |
| Speech origin | `web/speech_origin.py` (or event tags at write time) | Prefer origin already on cues; for metrics, classify finals vs Script says / `sim.script.cue` proximity |
| Silence after line | `script/runtime.py` + `script/models.py` | Split semantics: wait/intentional silence still suppresses; **line** cue default = short post-inject drain only (portable knob) |
| Style conflict | `prompt_sections.py` `StyleTraitsSection` | When verbosity ≠ quiet, strip or rewrite contradictory style tokens (`short turns`, `terse replies`, …) **or** append override: “verbosity band wins over style length hints” |
| Natural band | `length_guidance` / `NaturalSpeechSection` | Stronger natural examples; still no monologue; chatty unchanged |
| Tests | `tests/test_metrics.py`, `test_caller_policy.py`, new runtime mute tests | Hard CI on metric split + mute semantics + style override |
| Docs | portability / scenario authoring | Coval-style Scenario vs Script; when to use `silence_after_cue_ms` |

### Non-goals (same as v1 + new)
- No worker hardcoding in `src/`.
- No cascade rewrite in this PR.
- No change to hang_up defaults (`require_agent_reply_this_turn`, etc.) unless a one-line exception is documented.
- Do not auto-parse free-text style into verbosity (keep explicit enum); only neutralize **known length-conflict phrases** when a verbosity band is active.
- Do not make overall suite `user_words_p50` a merge gate.

### Open questions — closed for v2 implement
| Question | Decision |
|----------|----------|
| Keep overall `user_words_p50`? | **Yes** — rename docs to “all user finals (incl. Script)”; add natural split as the soft naturalness signal. |
| Mute after line cues? | **Default off for long holds** — `silence_after_cue_ms` on `action=speak` becomes short drain (or ignored for suppress); intentional mute stays on `action=wait` / explicit `hold_caller_silent` (name TBD, one clear knob). |
| Edit worker JSONL in core PR? | **No** — docs + optional follow-up authoring PR. |
| Default verbosity? | Stay **`natural`**. |

---

## Evidence (code + reports)

1. **Metric includes Script:** `metrics.py` `_user_word_counts` — all non-empty deduped user finals; no `speech_origin` filter.
2. **Mute after speak:** `script/runtime.py` ~L355–356 `suppress_persona_output(hold_silence_ms)` after line inject.
3. **Mute on wait:** same file ~L202–210 `begin_scripted_user_silence`.
4. **Allow freestyle when not suppressed:** `live_session.py` `_allow_persona_room_audio` ~L450–469.
5. **Style inject:** `prompt_sections.py` `StyleTraitsSection` ~L165–170.
6. **Worker authoring:** `.agent-sim/scenarios/e2e-flow-happy.jsonl` — dense Script + `short turns` + large `silence_after_cue_ms`.
7. **Suite soft numbers:** sqlite mean p50 **9.75** (window above); script say median **10.5**.

---

## Acceptance metrics (what would prove improvement)

| Metric | Definition | Target after v2 smoke |
|--------|------------|------------------------|
| `user_words_p50` (overall) | unchanged | May stay ~8–12 on Script-heavy packs — **not** the success signal |
| `user_words_natural_p50` | whitespace tokens on user finals **not** classified as script_cue / mostly-script-say | On hybrid packs with agent questions between cues: **≳ 12–20** when verbosity=`natural` and mute-after-line fixed |
| `user_words_natural_count` | count of natural finals | Should be **> 0** on hybrid e2e when agent asks between milestones |
| `user_words_script_p50` | optional: script-classified only | ≈ authored say length (sanity) |
| Listening | human spot-check | Freestyle answers audible between milestones without inventing early bye |
| Pytest | SI + mute + metric split | Hard fail CI |

**Do not** soft-fail CI on live suite word counts (STT/flake).

---

## Option A vs B (decision)

| | Option A (v2) | Option B |
|--|---------------|----------|
| What | Metric split + mute semantics + style conflict + stronger natural freestyle band | Cascade text LLM + TTS user mouth |
| Proves listening lift? | Yes, if freestyle can speak | Yes, different architecture |
| Portable? | Yes | Yes, but large |
| Risk to asserts | Medium (more freestyle) | Higher (dual voice identity) |
| Choose now | **Yes** | Defer |

---

## Steps (simple checklist)

1. [x] Add `user_words_natural_*` (+ optional `user_words_script_*`) in `compute_voice_metrics`; unit tests with mixed script/natural finals.
2. [x] Fix post-line mute: `silence_after_cue_ms` on speak must not mean multi-second freestyle blackout by default; keep wait/intentional silence mute. Document one portable knob.
3. [x] Style/length conflict: when verbosity is `natural`/`chatty`, neutralize known short-length style phrases or append explicit “verbosity wins” line; pytest.
4. [x] Strengthen `NaturalSpeechSection` / natural `length_guidance` with 1–2 concrete phone examples (still cap chatty; forbid bye while Script pending).
5. [x] Docs: Scenario vs Script (Coval map); when overall p50 is meaningless; authoring for natural listening packs.
6. [x] pytest green; smoke: compare `user_words_natural_p50` before/after on same worker root — overall p50 may be flat. *(pytest done; live smoke pending)*
7. [x] Stop — no worker JSONL edits in core PR unless owner opens a separate authoring ticket.

---

## Files to touch

| File | Change |
|------|--------|
| `src/livekit_agent_simulator/metrics.py` | natural/script word stats |
| `src/livekit_agent_simulator/script/runtime.py` (+ models if new field) | mute semantics after line vs wait |
| `src/livekit_agent_simulator/caller/prompt_sections.py` | style conflict + stronger natural band |
| `tests/test_metrics.py`, `tests/test_caller_policy.py`, (+ runtime mute test) | CI gates |
| `docs/portability.md` or scenario authoring note | metric + Script sparsity guidance |

**Not in this PR:** `voice-ai-worker/.agent-sim/scenarios/*` (optional follow-up).

---

## Risk matrix

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Freestyle invents early bye | Med | High | Keep I3 SI + midcall; hang_up gates unchanged |
| Extra chatter flakes assert phrases | Med | Med | Path asserts stay agent-side; natural band still goal-bound; `quiet` for brittle packs |
| Changing mute breaks VAD silence fixtures | Med | High | Only change speak-after suppress; wait/intentional silence unchanged |
| Style strip too aggressive | Low | Low | Allowlist of conflict tokens; verbosity field still authoritative |
| Chatty callers hurt path labs | Low | Med | Default natural not chatty; document quiet for fixtures |

---

## Relation to v1 plan

v1 fixed **SI length defaults** and added overall `user_words_*`. That was necessary but **insufficient**: the smoke suite could not observe freestyle, and runtime mute + style conflict undid hybrid intent. v2 fixes **observability + gating + conflict**, then re-smokes on **natural-only** metrics.

---

**Status: Implemented (core + docs). Live `lks execute-all` re-smoke deferred — reply to request when ready.**

---

## Files to touch

| File | Change |
|------|--------|
| `src/livekit_agent_simulator/metrics.py` | natural/script word stats |
| `src/livekit_agent_simulator/script/runtime.py` (+ models if new field) | mute semantics after line vs wait |
| `src/livekit_agent_simulator/caller/prompt_sections.py` | style conflict + stronger natural band |
| `tests/test_metrics.py`, `tests/test_caller_policy.py`, (+ runtime mute test) | CI gates |
| `docs/portability.md` or scenario authoring note | metric + Script sparsity guidance |

**Not in this PR:** `voice-ai-worker/.agent-sim/scenarios/*` (optional follow-up).

---

## Risk matrix

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Freestyle invents early bye | Med | High | Keep I3 SI + midcall; hang_up gates unchanged |
| Extra chatter flakes assert phrases | Med | Med | Path asserts stay agent-side; natural band still goal-bound; `quiet` for brittle packs |
| Changing mute breaks VAD silence fixtures | Med | High | Only change speak-after suppress; wait/intentional silence unchanged |
| Style strip too aggressive | Low | Low | Allowlist of conflict tokens; verbosity field still authoritative |
| Chatty callers hurt path labs | Low | Med | Default natural not chatty; document quiet for fixtures |

---

## Relation to v1 plan

v1 fixed **SI length defaults** and added overall `user_words_*`. That was necessary but **insufficient**: the smoke suite could not observe freestyle, and runtime mute + style conflict undid hybrid intent. v2 fixes **observability + gating + conflict**, then re-smokes on **natural-only** metrics.

---

**Status: Waiting for your OK — reply go ahead to implement**
