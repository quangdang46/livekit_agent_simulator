# Plan Report

## Summary (read this first)
- **You asked:** Root cause why VAD scenario runs still sound “wrong” despite gate/script improvements.
- **What is going on:** The sim caller is a **Gemini Live freestyle persona**. Script only *injects* cues and (recently) gates *hang-up*. Between cues, Gemini still answers the agent and invents dialogue. That fights the VAD probe scripts. Soft-barge failed separately on a Gemini Live socket drop (1006).
- **We recommend:** Treat Script as hard SoT for mid-call speech: mute freestyle PCM for the whole pending-Script lifetime (allow only Script inject + Script hang-up farewell), and tighten Persona goals for probe scenarios so they don’t require full signup.
- **Risk:** Medium (behavior change for all Script scenarios; need opt-out for freestyle-heavy Persona tests)
- **Status:** Implemented — freestyle muted while Script pending; prompts/Personas tightened

## Bug investigation
- **Verified root cause (primary):** Freestyle speech is **not** hard-blocked while Script steps remain. `bind_script_pending` only defers freestyle **goodbye / [END_CALL]**; normal answers still play to the room. Prompt even invites answering after agent questions.
- **Verified root cause (secondary):** Guardrails/Goals tell Gemini to finish **all** Persona goals (signup) before ending — conflicts with “fee probe → Script bye” scenarios.
- **Verified (soft-barge):** Run died on `APIError: 1006` Gemini Live abnormal closure during `state_need` inject — harness/assert failure, not AAD.
- **Backchannel “Aj…”:** Asset resolves correctly to EN `backchannel_uhhuh_en.wav`. Garbled user text is STT mis-hear of stretched uh-huh / encoding, not wrong locale WAV.
- **Sub-agents used:** skipped — single confirmed seam with event + code citations
- **Citations checked:**
  - `live_session.py` — suppress is time-windowed; pending only affects farewell
  - `prompt_sections.py` — ScriptTiming allows answering questions; Goals/Guardrails require all goals
  - report `vad-rt-barge-pcm-…d915` — freestyle “waiting for my ride” after barge; Script bye later
  - report `vad-rt-soft-barge-…8064` — `sim.error` 1006 then `run.end_condition sim_end_call`

## Evidence
1. **Our code — freestyle mute scope:** `src/livekit_agent_simulator/gemini/live_session.py` — `_persona_output_suppressed()` is TTL after cue/PCM; `_script_steps_pending()` used for farewell/[END_CALL] only, not for mute of normal `model_turn` PCM.
2. **Our code — prompt invites answers:** `caller/prompt_sections.py` `ScriptTimingSection` — “Stay quiet … **unless you are answering a direct question**”; `GuardrailsSection` / `GoalsSection` — don’t end until **ALL goals** done.
3. **Our code — Script soT intended but incomplete:** `inject_cue` comment: room_pcm vocal = SoT + short suppress; `gemini_text` is not literal TTS guarantee.
4. **Events:** barge-pcm freestyle mid-dialog; soft-barge `sim.error` `APIError: 1006`; backchannel cue asset path = EN uh-huh WAV via `lk-sim cues --resolve`.
5. **GitHub prior art:** N/A for this product seam (Script vs Gemini Live caller dual control).

## Steps (simple checklist)
1. [x] Mute freestyle room PCM whenever `has_pending_steps()` (except Script inject / hang-up farewell).
2. [x] Prompt: when Script present, “answer only if no Script arm; otherwise stay silent” + VAD Persona goals = probe intents not full signup.
3. [ ] Soft-barge (deferred): retry / harden Gemini reconnect on 1006 (separate ticket).
4. [ ] Re-run `vad-rt` and spot-check the four report URLs.

## Files to touch
- `src/livekit_agent_simulator/gemini/live_session.py`
- `src/livekit_agent_simulator/caller/prompt_sections.py` (+ tests)
- optional: VAD scenarios Persona goals in `voice-ai-worker/.agent-sim/scenarios/vad-rt-*.jsonl`

## If you want more detail
### Causal chain
```
Agent asks name/fee clarification
  → Gemini freestyle answers (mute off; silence_after expired)
  → Transcript looks like messy human+bot improv
  → Script bye finally fires after defer budget
  → Gate/script may PASS while conversation is still “wrong”
```

### Soft-barge
Transport failure closes Gemini mid-inject → almost empty transcript → assert/script fail. Not VAD sensitivity.
