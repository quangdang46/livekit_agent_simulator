# Scenario plugins

Dev-written plugins extend the simulator without forking the package. There are
three registration kinds:

| Kind | When it runs | Purpose |
|------|----------------|---------|
| **Verify** | After the call, during `script_verify` | Custom checks over `events.jsonl` (referenced from `Script.verify`) |
| **before_run** | After prepare, **before** SimLeg connects | Enrich `meta`, set up external resources |
| **after_run** | After finalize, just before execute returns | Notify CI/Slack, archive reports, side effects |

Verify plugins run after the built-in log checks (`min_agent_finals_after_first_cue`, etc.).
Lifecycle hooks (`before_run` / `after_run`) fire for every execute once the plugin
module is loaded (local `.agent-sim/plugins/` or entry-point `setup`).

## 1. Write a plugin

Copy `templates/plugins/example_verify.py` to `<target>/.agent-sim/plugins/my_checks.py`:

```python
from livekit_agent_simulator.plugins import (
    AfterRunContext,
    BeforeRunContext,
    VerifyContext,
    register_after_run,
    register_before_run,
    verify_plugin,
)

@verify_plugin("adaptive_backchannel")
def adaptive_backchannel(ctx: VerifyContext) -> dict:
    ok = ctx.finals_after_first_cue("agent") >= 1
    return {"pass": ok, "checks": [{"check": "agent_continued", "pass": ok}]}

@register_before_run
def stamp_run(ctx: BeforeRunContext) -> None:
    # ctx.meta is a mutable dict (dataclass is frozen; dict contents are not)
    ctx.meta["plugin_stamp"] = "my_checks"

@register_after_run
def notify_done(ctx: AfterRunContext) -> None:
    print(f"{ctx.run_id} → {ctx.status} ({ctx.report_dir})")
```

Or ship plugins from an installable package:

```toml
# pyproject.toml (your worker or test package)
[project.entry-points."lks.plugins"]
worker_sim = "my_worker.sim_plugins:setup"
```

```python
# my_worker/sim_plugins.py
from livekit_agent_simulator.plugins import (
    AfterRunContext,
    BeforeRunContext,
    VerifyContext,
    register_after_run,
    register_before_run,
    register_verify,
)

def setup() -> None:
    register_verify("worker_flow_started", _flow_started)
    register_before_run(_before)
    register_after_run(_after)

def _flow_started(ctx: VerifyContext) -> dict:
    flows = ctx.events_of_kind("data.message", prefix=False)
    started = any(
        (e.get("spec") or {}).get("payload", {}).get("type") == "flow_started"
        for e in flows
    )
    return {"pass": started, "detail": "flow_started seen on data topic"}

def _before(ctx: BeforeRunContext) -> None:
    ctx.meta["worker_plugin"] = True

def _after(ctx: AfterRunContext) -> None:
    if ctx.status != "done":
        # e.g. post to CI / Slack — keep secrets out of this module
        pass
```

Local modules under `.agent-sim/plugins/` may also define `setup()` — the loader
calls it after import (in addition to any `@register_*` at module level).

## 2. Reference from JSONL

Load local modules (optional if plugins register via entry-points only):

```json
{"kind":"Plugins","spec":{"modules":["my_checks"]}}
```

Loading the module registers **all** verify + lifecycle hooks in that file.
Wire **verify** plugins on the Script line (lifecycle hooks need no Script reference):

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

`Script.verify.plugin_options` is also passed to lifecycle hooks as `ctx.options`
(the full options map, not only one plugin’s slice).

### Portable example: adaptive false-interrupt resume

The `adaptive_backchannel` example above is **portable** — it uses only the
`VerifyContext` API and the run’s own `events`, never a worker-specific agent ID,
data topic, or business key. The same portable pattern applies to verifying that
the agent **recovers** after a `noise`-class (false-interrupt) cue:

```python
from livekit_agent_simulator.plugins import VerifyContext, verify_plugin

@verify_plugin("adaptive_false_interrupt_resume")
def adaptive_false_interrupt_resume(ctx: VerifyContext) -> dict:
    # A noise-class cue must not count as a recovery barge. Verify the agent
    # spoke again after the false interrupt and no tool error leaked.
    noise_cue_ms = ctx.first_cue_ms()
    agent_finals = ctx.events_of_kind("transcript.agent.final")
    resumed = any(
        int(e.get("ts_mono_ms") or 0) >= noise_cue_ms
        for e in agent_finals
    ) if noise_cue_ms is not None else False
    tool_errors = len(ctx.events_of_kind("tool.error"))
    return {
        "pass": resumed and tool_errors == 0,
        "checks": [
            {"check": "agent_resumed_after_false_interrupt", "pass": resumed},
            {"check": "no_tool_error", "pass": tool_errors == 0},
        ],
    }
```

Reference it from JSONL exactly like `adaptive_backchannel`:

```json
{
  "kind": "Script",
  "spec": {
    "steps": [{"id": "noise", "trigger": "agent_speaking", "delay_ms": 500, "say": "[noise]", "delivery": "room_pcm", "asset": "builtin:noise.loud", "barge_in": true, "class": "noise"}],
    "verify": {
      "plugins": ["adaptive_false_interrupt_resume"]
    }
  }
}
```

> **Portability rule:** verify plugins are portable when they read only
> `VerifyContext` (`events`, `steps`, `scenario`, `options`) and the run’s own
> `events.jsonl`. A plugin that hardcodes a consumer agent identity, a
> project-specific `data_topic`, or a business keyword is **not** portable and
> belongs in the target’s `.agent-sim/plugins/`, not in this package.

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
lks plugins --root /path/to/target
```

MCP: `list_plugins(project_root)`.

Lists **verify** plugin names. Lifecycle hooks are registered in-process when
modules load; they are not named entries in `lks plugins`.

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

## Lifecycle hooks (`before_run` / `after_run`)

Register with `@register_before_run` / `@register_after_run` (or call
`register_before_run(fn)` / `register_after_run(fn)` from `setup()`).

| Hook | Timing in `run_orchestrator` |
|------|------------------------------|
| `before_run` | After prepare (meta built, plugins loaded), **before** SimLeg connects |
| `after_run` | After SQLite finalize / report written, **just before** execute returns |

Hooks return `None`. Exceptions propagate and fail the run (do not swallow silently
unless you intend soft side effects).

### BeforeRunContext

| Member | Purpose |
|--------|---------|
| `scenario` | Parsed scenario |
| `project_root` | Target repo root |
| `run_id` / `run_name` | Allocated run id and optional `--name` |
| `meta` | Mutable run meta dict (enrich before connect; written into `meta.json`) |
| `dispatch_metadata` | Opaque dispatch dict (or `None`) |
| `options` | `Script.verify.plugin_options` map (empty if no Script verify) |

### AfterRunContext

| Member | Purpose |
|--------|---------|
| `scenario` | Parsed scenario |
| `project_root` | Target repo root |
| `run_id` / `run_name` | Run identifiers |
| `report_dir` | Path to `.agent-sim/reports/<run-id>/` |
| `status` | Final status (`done` / `failed` / …) |
| `summary` | Summary dict (metrics, assert/script_verify, judge, …) |
| `events` | Event records for this run |
| `verdict` | Soft judge verdict dict, or `None` |
| `options` | Same `plugin_options` map as before_run |
