# Multi-repo portability

**Optional reference** — not required for sim package development. Open only when wiring
`.agent-sim/` for a specific consumer repo.

`livekit-agent-simulator` is **target-agnostic**. Each consumer owns a gitignored `.agent-sim/`
folder; the Python package never imports consumer application code.

## What is generic (sim core)

| Layer | Behavior |
|-------|----------|
| **Dispatch** | Opaque JSON string → `RoomAgentDispatch.metadata`. Sim does not parse keys. |
| **Scenario** | `Execute`, `Dispatch`, `Persona`, `PassCriteria`, `Script` — same JSONL for any agent. |
| **Transcripts** | `lk.transcription` (LiveKit standard) + configurable data payloads (`transcript_payload_types`, default `transcript_turn`). |
| **Dedupe** | Multiple sources (sim.gemini, lk.transcription, data topics) merged with source priority so turn count stays accurate. |
| **L3 session** | `lk.agent.session` passive events + final `getChatHistory` / `getSessionUsage` snapshot for LiveKit Agents SDK workers. |
| **Tools** | Automatic SDK `tool.start` / `tool.end` / `tool.error`; optional `observe.tool_event_patterns` fallback for custom agents. |
| **Report player** | `lks web` — stereo audio + transcript sync; tool bands/cards + session footer when L3 events exist. |

## Per-target setup (in `<repo>/.agent-sim/`)

1. **`config.yaml`** — LiveKit URL/key/secret, `agent_name`, `simulator.api_key` (+ `simulator.provider`).
2. **Optional `livekit.dispatch_metadata`** — default opaque JSON for all runs.
3. **Optional per-scenario `Dispatch.metadata`** — overrides config default.
4. **`observe.data_topics`** — list topics your agent publishes (empty = record all).
5. **`observe.lk_agent_session`** — defaults to `true`; disable only for non-SDK agents.
6. **`observe.tool_event_patterns`** — optional fallback when the agent publishes a custom tool wire format.

## Example: app-built dispatch metadata

Some stacks build dispatch metadata in a web app and pass opaque keys the agent process reads
from job metadata (e.g. `customAgentId`). The simulator only forwards that JSON string.

**Target-only** scenario line (not in sim package):

```json
{"kind":"Dispatch","spec":{"metadata":"{\"customAgentId\":\"agent_xxx\"}"}}
```

**Target-only** `config.yaml` snippet for richer logs:

```yaml
observe:
  lk_agent_session: true
  data_topics:
    - myapp.flow
    - myapp.transcript
  # Only needed if this agent does not expose SDK tool events:
  tool_event_patterns:
    - match: { topic: myapp.flow, type: tool_started }
      emit: tool.start
```

## Example: unknown / third-party LiveKit agent

Minimum config — no custom data topics:

```yaml
livekit:
  agent_name: "their-agent"
observe:
  lk_transcription: true
  # Set false if this agent does not implement LiveKit Agents RemoteSession.
  lk_agent_session: false
  data_topics: []
  tool_event_patterns: []
```

Scenario with `Execute.first_speaker: user` if the agent waits for caller audio.

### Caller verbosity (portable length band)

Default caller speech is **`natural`** (1–3 spoken clauses + occasional fillers). Set
explicitly when a pack needs sparse or chatty talk:

```json
{"kind":"Persona","spec":{"speech_conditions":{"verbosity":"quiet"}}}
```

| Value | Effect |
|-------|--------|
| `natural` (default if omitted) | Phone-natural length; `NaturalSpeechSection` fillers on |
| `quiet` | Short clause; no filler encouragement — use for VAD / terse fixtures |
| `chatty` | Longer turns + fillers; still goal-bound |

Trait aliases when `verbosity` is omitted: `quiet` / `silent` / `terse` → quiet;
`chatty` → chatty. Free-text `style` (e.g. “short turns”) is **not** parsed for
verbosity — migrate with an explicit field or trait. When verbosity is
`natural`/`chatty`, known style length phrases (`short turns`, `terse replies`, …)
are stripped from the SI so they cannot fight the length band.

### Scenario vs Script (natural listening packs)

| Mode | What it is | Word-length metrics |
|------|------------|---------------------|
| **Dialogue / Scenario** | Persona goals; little or no Script mouth | `user_words_*` ≈ freestyle; use `user_words_natural_*` |
| **Hybrid** | Sparse Script **milestones** + freestyle between cues | Overall `user_words_p50` mixes Script + freestyle — **prefer `user_words_natural_p50`** |
| **Wall-to-wall Script** | Almost every turn is a forced `say` | Overall p50 ≈ authored say length; natural count may stay ~0 |

**Mute / silence knobs (portable):**

| Knob | Effect |
|------|--------|
| `action: wait` + `silence_after_cue_ms` | **Intentional** caller silence (VAD / dead-air fixtures). Suppresses freestyle for the hold. |
| `action: speak` + `silence_after_cue_ms` | **Does not** long-mute freestyle after the line (inject already drains TTS). Prefer `wait` for silence holds. |
| `speech_conditions.silent_mode` | Whole-call mute |

For natural listening packs: keep Script sparse (open / key correction / hang_up), set
`verbosity` explicitly if needed, and judge soft naturalness with
`user_words_natural_p50` / `user_words_natural_count` — not overall suite p50 alone.

If the agent uses a different transcript payload type, set:

```yaml
observe:
  transcript_payload_types:
    - "live_transcript"
```

## Verification checklist

```bash
lks preflight --root /path/to/target
lks validate smoke-hello --root /path/to/target
lks execute smoke-hello --root /path/to/target
```

Expect `status: done`, optional judge verdict, sensible `turn_count`, exit code 0.
Script scenarios: check `summary.script_verify.pass` in the report (no judge required).
Adaptive A/B/C use transcript finals and `during_agent_speech` on the scripted cue — not worker SDK telemetry.
