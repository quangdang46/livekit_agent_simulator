# Script audio cues (room_pcm)

**24 kHz, mono, 16-bit PCM WAV** files mixed into the sim caller mic (`delivery: room_pcm`).
The agent STT hears this audio (parallel with Gemini speech via `ParallelMicMixer`).

## Built-in vs custom (multi-repo)

| Layer | Where | How to reference |
|-------|--------|------------------|
| **Built-in** | package `templates/cues/` | `builtin:noise.loud`, `@noise.ambient`, or filename |
| **Target override** | `<repo>/.agent-sim/cues/` | same filename **wins** over package |
| **Alias** | `config.yaml` → `cues.aliases` | short name, e.g. `office` |
| **Extra dirs** | `cues.dirs` | more search paths under project root |
| **Next to scenario** | same folder as `.jsonl` | relative path |

```bash
lk-sim cues --root /path/to/target
lk-sim cues --root /path/to/target --resolve builtin:noise.loud
```

## Built-in short IDs

| ID | File |
|----|------|
| `noise.ambient` | `ambient_noise_bed.wav` |
| `noise.loud` | `loud_noise_burst.wav` |
| `noise.blip` / `noise.interrupt` | `loud_interrupt_blip.wav` |
| `backchannel` | `backchannel_ja.wav` |
| `interrupt` | `real_interrupt_ja.wav` |
| `ambiguous` | `ambiguous_ja.wav` |

## Scenario examples

```json
{"id":"n1","trigger":"time","delay_ms":5000,"delivery":"room_pcm","asset":"builtin:noise.loud","say":"[noise]"}
```

```json
{"id":"n2","trigger":"agent_speaking","delay_ms":400,"delivery":"room_pcm","asset":"cafe.wav","say":"[cafe]"}
```

Put `cafe.wav` in `.agent-sim/cues/` for that target repo only.

```yaml
# .agent-sim/config.yaml
cues:
  aliases:
    street: street_vn.wav
  dirs:
    - media/noise
```

```json
{"asset":"street","delivery":"room_pcm","say":"[street]","trigger":"time","delay_ms":3000,"id":"s1"}
```
