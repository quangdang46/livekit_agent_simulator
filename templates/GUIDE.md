# livekit-agent-simulator — agent/setup guide

Standalone package: dial **any** LiveKit voice agent with a Gemini Live simulated caller.
CLI (`lk-sim`) and MCP share the **same** ops. Target-specific wiring lives only in
`<target>/.agent-sim/` — never in package source.

## 1. One-time setup (target repo)

```bash
lk-sim init --root /path/to/target
# edit .agent-sim/config.yaml:
#   livekit.url / api_key / api_secret / agent_name
#   simulator.google_api_key
#   optional: livekit.dispatch_metadata  (opaque JSON string)
#   optional: observe.record_audio: true  → reports/<run-id>/conversation.wav
```

Start the **agent worker** so it registers with the same `agent_name`.

```bash
lk-sim preflight --root /path/to/target
```

## 2. Scenarios

```bash
lk-sim scenario-init my-case --root /path/to/target
# → .agent-sim/scenarios/my-case.jsonl
# Full-line // comments document each kind. Delete unused kinds.
# Runtime ignores // lines.

lk-sim validate my-case --root /path/to/target
lk-sim execute my-case --root /path/to/target
```

Kinds (see `//` lines in scaffold): Scenario, Persona, Context?, Simulator?, Execute,
Dispatch?, Script?, Plugins?, PassCriteria?

- **Dispatch.metadata** — opaque JSON string for `RoomAgentDispatch`; core never parses keys.
- **Script** — timed cues (`agent_speaking`); `delivery: gemini_text | room_pcm`.
- **PassCriteria** — optional LLM judge (needs `judge:` in config).

## 3. Public ops (CLI ↔ MCP)

| CLI | MCP |
|-----|-----|
| `init` | `init_project` |
| `preflight` | `preflight` |
| `scenarios` | `list_scenarios` |
| `plugins` | `list_plugins` |
| `validate` | `validate_scenario` |
| `export` | `export_scenario` |
| `scenario-init` | `init_scenario` |
| `execute` | `execute_scenario` |
| `execute-all` | `execute_scenarios` |
| `execute-dict` | `execute_scenario_dict` |
| `status` | `get_run_status` |
| `log` | `get_run_log` |
| `report` | `get_run_report` |
| `compare` | `compare_runs` |
| `runs` | `list_runs` |
| `guide` | `guide` |

## 4. After a run

```bash
lk-sim report <run-id> --root /path/to/target
lk-sim log <run-id> --kind "transcript.*" --root /path/to/target
```

Reports: `.agent-sim/reports/<run-id>/`

- `events.jsonl`, `timeline.md`, `summary.json`, `meta.json`
- `conversation.wav` if `observe.record_audio: true` (L=sim, R=agent)

`run_id` format: `{scenario_id}-{YYYYMMDD-HHMMSS}-{hex4}` (UTC).

## 5. Common failures

| Symptom | Check |
|---------|--------|
| preflight livekit.api fail | URL / API key / secret |
| dispatch.agent_timeout | Worker running? `agent_name` match? |
| agent silent | `first_speaker` / Dispatch.metadata for your stack |
| no judge | `PassCriteria` + `judge:` in config |
| Gemini key warn | Any long Google/Gemini key is fine |

## 6. Rules for coding agents

- Do **not** hardcode consumer keys, language, or business logic in package `src/`.
- Customize only under target `.agent-sim/` (config, scenarios, plugins).
- Prefer `execute` / `execute_scenario` over ad-hoc scripts.
- Full research loop and boundary: see package `AGENTS.md`.
