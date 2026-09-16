# Plan Report — Natural LKS caller talk (hardened)

## Summary (read this first)
- **You asked:** Harden this plan so one implementation pass durably fixes sparse / robotic LKS caller speech (portable core — not scenario sleeps, not fit-to-one-repo).
- **What is going on:** Core SI + midcall reground **hard-code brevity** (“1–2 sentences”) everywhere; traits that add detail (`chatty`) are opt-in; freestyle between Script cues is already allowed. Target packs also say `style: … short turns` and wall-to-wall Script `say` — but the durable fix is **defaults + knobs in core**, not more scenario patches.
- **We recommend:** Ship **Option A only** — `speech_conditions.verbosity` ∈ `quiet|natural|chatty` (default **`natural`**), `NaturalSpeechSection`, rewrite Role / Script / Guardrails / midcall length lines to the band, hybrid Script = milestone then freestyle. Leave hang_up gates unchanged. Optional `user_words_p50` metric. Defer cascade TTS user-sim (Option B).
- **Risk:** Medium — richer freestyle can invent early bye or drift; mitigated by unchanged hang_up SI + Script gates + `quiet` for VAD/interaction fixtures.
- **Status:** Implemented on branch `feat/natural-caller-talk` (pytest green). Live suite smoke still pending.

---

## Non-goals (do not do in this PR)

1. **No cascade rewrite** (text LLM planner + separate TTS) — Option B stays deferred.
2. **No consumer hardcoding** in `src/` — no worker agent IDs, order-demo strings, `vi-VN` as default, or target-repo imports.
3. **No stubborn scenario patches** — no extra `delay_ms`, “human reminder” prose in every JSONL, or sleeps to paper over brevity.
4. **No change to Script timing semantics** — cue arming, `agent_speaking` + `delay_ms`, PCM inject, `suppress_persona_output` around inject/hang_up wrap stay as today.
5. **No change to hang_up defaults** unless this plan explicitly documents a one-line exception (default: **unchanged**).
6. **No auto-parse of free-text `style`** (e.g. detecting the English phrase `"short turns"`) to set verbosity — that would be brittle and locale-coupled. Migration is **explicit** `verbosity` / traits only.
7. **Do not edit** `voice-ai-worker/.agent-sim/scenarios/*` in the core PR (optional follow-up authoring PR only).

---

## Invariants (must hold after implement)

| ID | Invariant |
|----|-----------|
| I1 | Scenario path control remains authoritative: Script `say` / wait / hang_up / fixtures still fire on schedule. |
| I2 | Script forced `say` is spoken **once** as a milestone; freestyle may continue until the next cue (unless silent / suppressed / fixture overlay). |
| I3 | Freestyle **goodbye / `[END_CALL]`** while Script steps remain is still forbidden in SI + midcall. |
| I4 | `GeminiCallerBridge._allow_persona_room_audio` continues to allow freestyle between cues (no re-introducing mute-all-while-Script-pending). |
| I5 | Hang_up step defaults stay: `require_agent_reply_this_turn=True`, `defer_on_open_question=True`, `open_question_idle_ms=20000` (`script/models.py`). |
| I6 | Core defaults stay portable (`en-US` / `UTC` elsewhere; verbosity enum is language-neutral guidance). |
| I7 | Persona realism knobs ≠ PassCriteria / path assertions (Coval-style separation). |
| I8 | `Context.notes` still never injected into SI. |

---

## Feature planning

### Recommended approach (sealed)
Keep Gemini Live as the mouth. Change **DefaultCallerPolicy / PromptSections** so the default human caller:

1. Uses verbosity band **`natural`** unless overridden.
2. May use occasional standard fillers (`um` / `uh` / `well`) and one situational detail when helpful (`NaturalSpeechSection` for `natural`/`chatty`).
3. Treats Script `say` as **milestones**, then continues freestyle until the next cue.
4. Exposes `Persona.speech_conditions.verbosity` + trait aliases as the only extension points for length.

### Prior art (research — Exa MCP unavailable this session; WebSearch + WebFetch fallback)

| Source | What we reuse | What we avoid |
|--------|---------------|---------------|
| **Coval Personas** ([docs](https://docs.coval.ai/concepts/personas/overview)) | Persona ≠ test case; TTS-safe fillers (`um` not `ummm`); interruption / silent as **settings**; Script vs Scenario input types | Mixing path goals into speaking-style SI |
| **Coval test sets** | Script = exact lines; Scenario = improvise toward goal — maps to our hybrid overlay | Treating every e2e as wall-to-wall Script for “naturalness” |
| **τ-Voice / Sierra** ([arxiv](https://arxiv.org/abs/2603.13686), [tau2-bench](https://github.com/sierra-research/tau2-bench)) | User prompts with disfluencies; **separate** interrupt vs backchannel policies; fillers ≠ path control | Tick-based full-duplex rewrite in this PR |
| **EVA (ServiceNow)** ([eva](https://github.com/ServiceNow/eva)) | Goal + persona on user sim; experience metrics ≠ task completion; user behavioral fidelity validators | Measuring agent conciseness as a stand-in for caller naturalness |
| **Hamming** ([personas template](https://hamming.ai/resources/voice-agent-test-personas-support-calls-template)) | `communication_style` separate from goal/assertions; talk-ratio / turn metrics as evidence | Soft “sound human” without measurable gates |
| **Gemini Live best practices** ([Google](https://ai.google.dev/gemini-api/docs/live-api/best-practices)) | SI order persona → rules → guardrails; one-time vs conversational loops; mid-session text = user turn | Copying **agent** “keep responses short / progressive disclosure” as a hard caller ceiling |

**Key insight (sealed decision):** Industry stacks separate **who/how the caller speaks** from **what the test asserts**. Our bug is defaults that over-constrain *how*; path control already works. Fix defaults once; do not compensate in JSONL.

### Integration points (verified paths)
- `caller/prompt_sections.py` — Role / ScriptTiming / Guardrails brevity strings; add `NaturalSpeechSection`; wire via `build_default_sections()`
- `caller/default_policy.py` — midcall `script_no_early_bye` / bootstrap length wording
- `caller/policy.py` — `CallerPolicyContext.verbosity()` helper (resolve speech_conditions + traits)
- `persona_traits.py` — aliases only (`terse`/`quiet`/`silent`/`fast_speaker` → quiet band influence; `chatty` → chatty); do not invent consumer traits
- `gemini/live_session.py` — SI consumption only; **no** freestyle mute regression (`_allow_persona_room_audio`)
- `metrics.py` + `logging/event_writer.py` — optional `user_words_*` under `metrics`
- `tests/test_caller_policy.py` (+ new verbosity cases) — **hard CI gate**
- docs: short authoring note in existing scenario/portability docs if present

### Sub-agents used
Hardening pass: parent research + code re-verify (no parallel swarm required for plan-only harden).

### Option B (deferred)
Cascade user-sim (text LLM + TTS) with τ-Voice-style tick interrupt/backchannel. Larger architecture; dual voice identity risk. Revisit only if Option A fails smoke listening after one implement + suite pass.

### Open questions — **closed for this PR**
| Question | Decision |
|----------|----------|
| Default verbosity? | **`natural`** — that is the product fix. |
| Worker e2e/sa stay Script-primary? | **Yes** — path lab suites may keep Script; set `verbosity=quiet` only where VAD/terse fixtures need it (target authoring follow-up). |
| Fail CI on live `user_words_p50`? | **No** — stochastic. Emit metric; unit-test SI bands fail CI. Smoke is post-implement listening. |

---

## Evidence

### Reports (voice-ai-worker `.agent-sim/reports/`, 2026-07-22) — verified earlier + summary shape
1. Deduped `user.final` across suite: **~median 9 words/turn**, many 1–3 word fragments — checklist speech, not mute bug.
2. Example `016-e2e-flow-happy-…/summary.json`: has `user_chars` / `talk_ratio` but **no** caller word-count percentiles yet — justifies adding `user_words_p50`.
3. Scenario authoring: all active e2e/sa personas use `"style":"warm, everyday caller; short turns"` + dense Script `say` — amplifies core brevity; **not** fixed by more delays.

### Our code (re-verified 2026-07-22)

| Claim | Citation | Status |
|-------|----------|--------|
| Global brevity | `RoleSection.render` — `"Keep every utterance short … (1-2 sentences)."` (`prompt_sections.py` ~L63) | verified |
| Between-cue brevity | `ScriptTimingSection` — `"answer in 1–2 natural phone sentences"` (~L261); `GuardrailsSection` same (~L309–310) | verified |
| Midcall repeats brevity | `DefaultCallerPolicy.midcall_cues` `script_no_early_bye` — `"1–2 natural sentences"` (`default_policy.py` ~L71–73) | verified |
| `chatty` opt-in only | `TRAIT_LIBRARY["chatty"]` (`persona_traits.py` ~L50–52); no `verbosity` key in schema | verified |
| Freestyle allowed between cues | `_allow_persona_room_audio` docstring + `return True` path (`live_session.py` ~L450–470) | verified |
| Suppress around inject/hang_up | `suppress_persona_output`; hang_up wrap in `script/runtime.py` ~L97–100 | verified |
| Hang_up defaults | `ScriptStep.require_agent_reply_this_turn=True`, `defer_on_open_question=True`, `open_question_idle_ms=20000` (`script/models.py` ~L90–94) | verified |
| Tests assert old brevity | `test_script_timing_forbids_early_bye` expects `"1–2 natural"`; midcall test expects `"1–2"` (`tests/test_caller_policy.py`) | verified — **must be updated** with verbosity-aware asserts |
| Metrics lack word p50 | `compute_voice_metrics` — TTFW/turn_taking/talk_ratio/user_chars; no word percentiles (`metrics.py`) | verified |

### Research notes
- Gemini Live: put length/filler rules in **SI before connect**; mid-session text interrupts (already why we avoid Script bootstrap). “Short responses” in Google examples targets **agents**, not human callers — we must not copy that ceiling.
- τ-Voice user prompts include fillers **and** can still be terse when the task needs it — banded verbosity matches that.
- EVA / Hamming: naturalness is evidence + experience metrics; path pass/fail stays separate — our PassCriteria / script_verify stay authoritative.

---

## Acceptance criteria (measurable)

### A. System instruction bands
| Verbosity | How resolved | SI must include | SI must not include |
|-----------|--------------|-----------------|---------------------|
| **`natural`** (default) | Missing/`null` verbosity and no quiet/chatty alias traits | Phone-natural band: **1–3 spoken clauses**; may add **one** reason/detail; `NaturalSpeechSection` (fillers with standard spellings) | Sole global rule `"1-2 sentences"` / `"1–2 sentences"` / `"1–2 natural phone sentences"` as the only length ceiling |
| **`quiet`** | `speech_conditions.verbosity=quiet` **or** traits intersecting `{quiet, silent, terse}` (and `fast_speaker` keeps short length — map to quiet length band) | 1 short clause / today’s sparse behavior | Filler encouragement from `NaturalSpeechSection` (omit section or empty) |
| **`chatty`** | `verbosity=chatty` **or** trait `chatty` (chatty wins over quiet if both set — document: **verbosity field wins**; if only traits, `chatty` beats `quiet`) | Up to ~4 clauses + one tangential detail; still goal-bound; `NaturalSpeechSection` on | Unbounded monologue permission |

**Resolution helper (exact):** `CallerPolicyContext.resolved_verbosity() -> Literal["quiet","natural","chatty"]`:
1. If `speech_conditions.verbosity` in `{quiet,natural,chatty}` (case-insensitive) → use it.
2. Else if traits contain `chatty` → `chatty`.
3. Else if traits contain any of `quiet|silent|terse` → `quiet`.
4. Else → `natural`.
5. Unknown verbosity string → treat as `natural` + optional debug log (no crash).

### B. Script milestone behavior
- Forced line overlay: SI still says speak SIMULATOR CUE line **once**.
- After cue: continue naturally per verbosity until next cue (hybrid framing).
- Fixture overlays (barge/noise/DTMF/silence/room_pcm): unchanged — do not invent fixtures.

### C. Hang_up / freestyle end
- With Script: midcall + ScriptTiming + Guardrails still forbid freestyle bye / `[END_CALL]`.
- Defaults on `ScriptStep` hang_up fields **byte-identical** unless a documented exception.
- Dialogue mode (no Script): still one short goodbye when goals done (wording may follow verbosity but not chatty farewell loops).

### D. Portability
- No new required env vars.
- No worker-specific strings in `src/`.
- Templates/docs mention `verbosity` with neutral examples.

### E. Migration for existing “short turns” scenarios
| Existing authoring | After this PR |
|--------------------|---------------|
| No `verbosity`, style contains “short turns” | Gets **`natural`** (richer). Style text remains advisory fluff to the model; **does not** force quiet. |
| Need old sparse CI path | Set `"speech_conditions": {"verbosity": "quiet"}` or trait `"terse"` / `"quiet"`. |
| Want extra talk | `"verbosity": "chatty"` or trait `"chatty"`. |

Document this in a short authoring note. **Do not** silently rewrite target JSONL in the core PR.

---

## Regression gates

### Hard fail in CI (`uv run pytest -q`)
1. **Default SI:** `build_persona_system_instruction(...)` without verbosity → resolved `natural`; assert **absence** of sole `"1-2 sentences"` Role line; assert presence of natural-band + filler guidance.
2. **`verbosity=quiet`:** short-clause guidance present; `NaturalSpeechSection` absent.
3. **`verbosity=chatty`:** longer-band + fillers present.
4. **Trait aliases:** `traits=["quiet"]` → quiet SI; `traits=["chatty"]` → chatty SI; explicit `verbosity` overrides traits.
5. **Script invariants:** Script SI still forbids freestyle bye; still mentions SIMULATOR CUE / overlay; midcall `script_no_early_bye` still present and **does not** reintroduce hard `"1–2"` as the only answer length (uses verbosity-aware wording).
6. **Update** existing tests that currently `assert "1–2"` in ScriptTiming / midcall (`test_caller_policy.py` L84, L163) so they assert **quiet** band or new natural wording — otherwise CI locks the bug in.

### Soft / observational (not pytest-blocking)
| Metric | Where | Guidance after smoke |
|--------|-------|----------------------|
| `user_words_p50` | `summary.json` → `metrics` | Dialogue / natural Script-hybrid listening runs: expect **p50 ≳ 12–20** words on freestyle-heavy turns (not a hard gate). |
| `user_words_p10` | same | Should rise vs ~2-word fragments when verbosity=`natural`. |
| `user_words_mean` | same | Informational. |
| Existing `talk_ratio` / `user_chars` | keep | Unchanged definitions. |

**Definition (portable):** From deduped `transcript.user.final` texts in the event stream, split on whitespace, count tokens with `len(text.split())`, compute nearest-rank percentiles (same helper as `metrics._percentile`). Exclude empty. Name: **`user_words_p50`** (plus p10/mean/count).

**CI must fail on:** unit/pytest SI contract failures only.  
**CI must not fail on:** live suite word counts (flaky / STT / Script-forced lines skew).

---

## API / config surface

```yaml
# Persona.spec (scenario JSONL)
speech_conditions:
  verbosity: natural   # quiet | natural | chatty  (default if omitted: natural)
  # existing keys unchanged: silent_mode, interruption_rate, voice_gain, noise, …
traits: []             # aliases: terse|quiet|silent → quiet band; chatty → chatty
# style: free text — NOT parsed for verbosity
```

| Knob | Default | Scope |
|------|---------|--------|
| `speech_conditions.verbosity` | `natural` (implicit) | Length + NaturalSpeech on/off |
| traits `quiet`/`silent`/`terse` | n/a | Alias → quiet if verbosity omitted |
| traits `chatty` | n/a | Alias → chatty if verbosity omitted |
| Script `say` / hang_up / overlays | unchanged | Path control |

No new top-level Scenario kinds. No dual/legacy flag names (`brevity`, `talkativeness`, etc.).

---

## Risk matrix + rollback

| Risk | Likelihood | Impact | Mitigation | Rollback |
|------|------------|--------|------------|----------|
| Freestyle invents early bye | Med | High (suite fail) | Keep I3 SI + midcall; hang_up gates I5 | Revert prompt_sections + default_policy only |
| VAD / interaction suites get chatty | Med | Med | Document `verbosity=quiet` for those packs; templates | Target sets quiet; core default stays natural |
| Longer turns → timeouts | Low | Med | Cap chatty; Execute timeouts unchanged | quiet override |
| Tests still assert `"1–2"` → false green | High if missed | High | Checklist E6 above | N/A — must update tests in same PR |
| STT merges freestyle+cue → odd transcripts | Low | Low | Existing `speech_origin` classifier; do not add sleeps | — |
| Fillers hurt non-English locales | Low | Low | Language-neutral “occasional hesitation sounds”; locale still from `RESPOND IN {lang}` | quiet band |

**Rollback procedure:** `git revert` the single implement commit(s); no DB/migration. Metrics addition is backward compatible (extra keys).

---

## Implementation steps (ordered, smallest diff)

1. [x] Add `CallerPolicyContext.resolved_verbosity()` (+ tiny unit tests for resolution order).
2. [x] Add `NaturalSpeechSection` gated on `natural`/`chatty`; register in `build_default_sections()` after StyleTraits / before or after SpeechConditions (keep Google order: persona → rules → … → guardrails).
3. [x] Retarget `RoleSection`, `ScriptTimingSection`, `GuardrailsSection`, dialogue first-speaker “one short turn”, and `DefaultCallerPolicy.midcall_cues` length strings to call a shared helper `length_guidance(verbosity) -> str`.
4. [x] Hybrid Script framing: one line clarifying milestone-then-continue (no timing changes).
5. [x] Optional: `user_words_*` in `compute_voice_metrics` from user finals.
6. [x] Rewrite/extend `tests/test_caller_policy.py` per Regression gates (replace brittle `"1–2"` asserts).
7. [x] Short docs note (portability / scenario authoring): verbosity + migration table.
8. [x] **Stop** — do not touch worker scenarios in this PR.
9. [x] Verify: `uv run pytest -q` (Windows fallback: `.venv\Scripts\python.exe -m pytest -q`).
10. [ ] Post-implement smoke (owner / follow-up turn): worker + dashboard up, then  
    `lks execute-all --parallel 3 --root C:\Users\ADMIN\Documents\Projects\voice-ai-worker`  
    — listen for natural freestyle; check `user_words_p50` in a few summaries; path PassCriteria still pass.

---

## Files to touch

| File | Change |
|------|--------|
| `src/livekit_agent_simulator/caller/policy.py` | `resolved_verbosity()` |
| `src/livekit_agent_simulator/caller/prompt_sections.py` | bands + `NaturalSpeechSection` + hybrid Script lines |
| `src/livekit_agent_simulator/caller/default_policy.py` | midcall length wording |
| `src/livekit_agent_simulator/persona_traits.py` | optional: document aliases only (resolution lives in policy) |
| `src/livekit_agent_simulator/metrics.py` | optional `user_words_*` |
| `tests/test_caller_policy.py` | verbosity matrix + update old asserts |
| docs (short) | authoring / CHANGELOG if repo habit |

**Not in this PR:** `voice-ai-worker/.agent-sim/scenarios/*`

---

## Verify command

```bash
# From livekit-agent-simulator root (required before reporting implement done):
uv sync --extra dev
uv run pytest -q

# Windows if uv sync locks MCP exe:
.venv\Scripts\python.exe -m pytest -q
```

**Post-implement smoke (not part of plan approval; after code lands):**

```bash
lks execute-all --parallel 3 --root C:\Users\ADMIN\Documents\Projects\voice-ai-worker
```

Inspect reports under that root’s `.agent-sim/reports/` for natural turn length + branching. Word metrics are observational; pytest is the merge gate.

---

## Causal chain (verified)

```
RoleSection “1–2 sentences”
  + ScriptTiming/Guardrails “1–2 …” between cues
  + midcall script_no_early_bye “1–2 …”
  + opt-in-only chatty traits
  + target style “short turns” + dense Script say
    → Gemini emits checklist-length turns (~9 words median)
    → Listener: “caller talks too little / not human”
```

Freestyle mute-while-Script-pending is **not** the current cause (`_allow_persona_room_audio` allows freestyle).

### Relation to PLAN-20260716-human-like-scenario-caller
That work split dialogue vs interaction and stopped speak-inducing Script bootstrap. This plan is the **remaining naturalness gap**: default length + disfluency + milestone framing — durable in core defaults, not scenario sleeps.

---

**Status: Implemented (core + pytest). Post-smoke: overall `user_words_p50` did not lift (~9.7) — see upgraded plan `docs/plans/PLAN-20260722-natural-caller-talk-v2.md` (awaiting go ahead).**
