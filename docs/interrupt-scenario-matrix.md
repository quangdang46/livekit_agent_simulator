# Interrupt scenario matrix (lks)

Portable guidance for authoring barge-in / backchannel / noise scenarios.
Consumer-specific agent ids and Dispatch belong in the **target** `.agent-sim/scenarios/`.

## Research sources

| Source | Takeaway |
|--------|----------|
| [Hamming interruption runbook](https://hamming.ai/resources/voice-agent-interruption-handling-runbook) | Barge-in is a taxonomy (correction / backchannel / noise / DTMF / silence / escalate), not a bool |
| Speko cough study (2026) | Short cough often absorbed; need sharper/louder false-interrupt fixtures |
| Package `docs/caller-behavior-research.md` | Map taxonomy → Script `class` + verify plugins |
| Package `templates/cues/README.md` | Built-in `voice.*` / `noise.*` assets |

## Minimum suite (any realtime voice agent)

| # | Class | Script pattern | Expect |
|---|-------|----------------|--------|
| 1 | correction early | `agent_speaking` + `barge_in` + short delay | yield + recover |
| 2 | correction mid/late | longer `delay_ms` | same |
| 3 | backchannel WAV | `builtin:voice.backchannel`, `barge_in: false` | continue (soft) |
| 4 | backchannel text | short ack phrase, `barge_in: false` | continue (soft) |
| 5 | short noise | `builtin:noise.interrupt` or `ambiguous` | no hard derail |
| 6 | ambient | `builtin:noise.ambient` | no hard derail |
| 7 | soft correction | `gain` ~0.4 on barge say | yield or miss (measure) |
| 8 | escalation | barge asking for human | stop + acknowledge |
| 9 | PCM speech barge | `builtin:voice.barge_short` / locale asset | yield |
| 10 | double barge | two correction cues | recover both |

## Built-in cues to prefer

| Asset | Use |
|-------|-----|
| `builtin:voice.backchannel` | EN uh-huh (lengthened) |
| `builtin:voice.barge_short` | EN “Wait a second…” |
| `builtin:voice.barge_vi` | Short VI barge |
| `builtin:noise.ambient` | Soft bed |
| `builtin:noise.loud` / `noise.interrupt` | Burst / cut-in blip |

## Scaffold examples

Neutral JSONL under `templates/examples/`:

- `interrupt-correction.jsonl`
- `interrupt-backchannel.jsonl`
- `interrupt-noise-resume.jsonl`

Copy into a target repo, replace Dispatch metadata / language / Assert phrases.

## Out of scope for core package

- Hardcoding a consumer `customAgentId`
- Treating OpenAI vs Gemini interrupt knobs in Python (opaque Dispatch only)
