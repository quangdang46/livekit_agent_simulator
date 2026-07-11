# Scenario plugins

Dev-written **verify plugins** extend `Script.verify` without forking the sim package.
Plugins run after the built-in log checks (`min_agent_finals_after_first_cue`, etc.).

## 1. Write a plugin

Copy `templates/plugins/example_verify.py` to `<target>/.agent-sim/plugins/my_checks.py`:

```python
from livekit_agent_simulator.plugins import VerifyContext, verify_plugin

@verify_plugin("adaptive_backchannel")
def adaptive_backchannel(ctx: VerifyContext) -> dict:
    ok = ctx.finals_after_first_cue("agent") >= 1
    return {"pass": ok, "checks": [{"check": "agent_continued", "pass": ok}]}
```

Or ship plugins from an installable package:

```toml
# pyproject.toml (your worker or test package)
[project.entry-points."lk_sim.plugins"]
worker_sim = "my_worker.sim_plugins:setup"
```

```python
# my_worker/sim_plugins.py
from livekit_agent_simulator.plugins import register_verify, VerifyContext

def setup() -> None:
    register_verify("worker_flow_started", _flow_started)

def _flow_started(ctx: VerifyContext) -> dict:
    flows = ctx.events_of_kind("data.message", prefix=False)
    started = any(
        (e.get("spec") or {}).get("payload", {}).get("type") == "flow_started"
        for e in flows
    )
    return {"pass": started, "detail": "flow_started seen on data topic"}
```

## 2. Reference from JSONL

Load local modules (optional if plugins register via entry-points only):

```json
{"kind":"Plugins","spec":{"modules":["my_checks"]}}
```

Wire verify plugins on the Script line:

```json
{
  "kind": "Script",
  "spec": {
    "steps": [{"id": "bc", "trigger": "agent_speaking", "delay_ms": 900, "say": "うん", "delivery": "room_pcm", "asset": "backchannel_ja.wav"}],
    "verify": {
      "require_during_agent_speech": true,
      "min_agent_finals_after_first_cue": 1,
      "plugins": ["adaptive_backchannel"],
      "plugin_options": {
        "adaptive_backchannel": {"min_agent_finals": 1}
      }
    }
  }
}
```

Shorthand for a single plugin: `"plugin": "adaptive_backchannel"` (same as `"plugins": ["adaptive_backchannel"]`).

## 3. Python API (CI / dynamic scenarios)

```python
import asyncio
from livekit_agent_simulator import ops, scenario_from_dict

async def main():
    result = await ops.execute_scenario_dict(
        "/path/to/target-agent-repo",
        {
            "id": "dynamic-backchannel",
            "persona": {"brief": "Listener", "goals": ["listen"]},
            "execute": {"max_turns": 3, "timeout_s": 120, "first_speaker": "agent"},
            "plugin_modules": ["my_checks"],
            "script": {
                "steps": [...],
                "verify": {"plugins": ["adaptive_backchannel"]},
            },
        },
    )
    assert result["summary"]["script_verify"]["pass"]

asyncio.run(main())
```

## 4. Discover plugins

```bash
lk-sim plugins --root /path/to/target
```

MCP: `list_plugins(project_root)`.

## VerifyContext helpers

| Member | Purpose |
|--------|---------|
| `events` | Full `events.jsonl` records |
| `steps` | Parsed `ScriptStep` list |
| `scenario` | Parsed `Scenario` |
| `options` | Per-plugin options from `plugin_options` |
| `first_cue_ms()` | Timestamp of first `sim.script.cue` |
| `finals_after_first_cue(role)` | Count `transcript.{role}.final` after first cue |
| `events_of_kind(kind, prefix=False)` | Filter events |

Return shape from a plugin:

```python
{"pass": True, "checks": [...], "detail": "optional"}
```

Overall `script_verify.pass` is false if **any** built-in check or plugin fails.
