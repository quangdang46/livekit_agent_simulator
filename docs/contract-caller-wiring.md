# Contract Caller Wiring — Single-Path Design

Status: design locked, implementing in slices. Parent epic: Task #2.

Canonical flow (no shortcuts from AI/TTS to LiveKit):

```text
Scenario (caller_steps: say/do/wait/dtmf/interrupt/end)
  → Behavior Engine (dsl.parse_steps → CallerAction)
  → Orchestrator (WHEN: turn gate, timeouts, staleness, max_turns)
  → say: Interaction Planner → TTS → publish
  → do:  AI Language Adapter → Contract Validator → Interaction Planner → TTS → publish
  → Observer → TurnDetector → BehaviorEvaluator → next behavior / next turn
```

## Why text-only backend for `do:`

The live bug class (role-flip/recap) exists because generation and audio
publish are fused inside the Realtime model session: asking the model to say
X also lets it say Y. The fix is structural — `do:` generation must be a
**text-only** LLM call (`generate_content` / chat completion, no audio
session), then TTS, then PCM publish. The Realtime audio session is never
involved in caller speech again; the bridge keeps only mic/mixer plumbing.

## Slices

1. **`caller_contract/driver.py`** (this slice): pure `ContractCallerDriver`
   driving CallerAction lists through Orchestrator → Adapter → Validator →
   Planner, with abstract `PublishSink` / `AgentTurnWait` interfaces so it is
   fully unit-testable without LiveKit. Emits `contract.*` events on the
   existing EventWriter.
2. **Scenario YAML**: new top-level `caller_steps:` key parsed by
   `dsl.parse_steps` into `Scenario.caller_actions`. Legacy
   persona + script path untouched (backward compat; old scenarios keep
   working).
3. **Text backends**: `GeminiTextBackend` / `OpenAITextBackend` implementing
   `LanguageBackendProtocol.generate(context)` via text-only API calls.
   No audio session, no publish side effect.
4. **Publish sink**: `BridgePublishSink` wrapping `CallerBridge` mixer via a
   new protocol method `publish_validated_audio(pcm, gain)` (say AND do use
   the same sink — the only path to the mic). `inject_cue` / `_inject_*_text`
   / freestyle persona paths deleted once the driver owns all scenarios.
5. **run_orchestrator branch**: if `scenario.caller_actions` is non-empty,
   run `ContractCallerDriver` instead of `ScriptRunner` + freestyle bridge;
   persona generation disabled (bridge in plumbing-only mode).
6. **Deletions** (only after 1–5 green + live-tested):
   `script_speak_directive`, `_inject_gemini_text`, `_inject_openai_text`,
   `nudge_freestyle_answer`, `inject_reground`, freestyle pumps,
   `observer.merge_as_same_turn` backchannel fold, `ScriptRunner` (after
   migrating scenarios to `caller_steps:`).

## Invariants

- Every caller PCM reaching the sink carries the `GenerationIdentity` from
  `Orchestrator.current_identity()` at creation; the sink drops stale
  identities (`orch.is_stale`) — never publishes.
- `say` crosses `advance_caller_turn()` + `gate_say()`; never touches
  adapter/validator.
- `do` exhaustion (no VALID after retries) → `CALLER_BEHAVIOR_VIOLATION` +
  STOP; never speaks the rejected candidate.
- `wait`/`dtmf`/`interrupt`/`end` never go through AI/TTS (planner control
  actions only).
