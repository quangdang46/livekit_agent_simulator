# Plan: Add `--environment` flag for multi-environment LiveKit config

## Context

Currently `lks` has a flat `livekit:` block in `config.yaml` with one set of credentials. To test against production without editing the file every time, we need `--environment` — the same pattern as `--profile` (which already exists for `simulator.profiles`), but for `livekit.environments`.

**Config shape:**
```yaml
livekit:
  # Legacy flat block (backward compat when no --environment)
  url: "wss://local.livekit.cloud"
  api_key: "..."
  api_secret: "..."
  agent_name: "worker-local"

  environments:
    local:
      default: true
      url: "wss://local.livekit.cloud"
      agent_name: "worker-local"
    production:
      url: "wss://prod.livekit.cloud"
      agent_name: "worker-prod"
```

**Selection rules** (mirror `simulator.profiles` exactly):
1. `--environment <name>` wins → that env
2. No flag + exactly one `default: true` → that env
3. No flag + no defaults → legacy flat block (backward compat)
4. 2+ defaults → error

**Inheritance:** each env inherits unspecified keys from the flat `livekit:` block (same as profiles inherit from flat `simulator:`).

---

## Files to modify

### Python

| File | Change |
|------|--------|
| `src/livekit_agent_simulator/config.py` | Add `active_environment: str \| None` to `LiveKitConfig`; add env resolution in `load_config()` (port of `apply_profile`); add `active_environment` to `config_snapshot` |
| `src/livekit_agent_simulator/cli.py` | Add `ENVIRONMENT_OPTION`; add `environment` param to `preflight`, `execute`, `execute_all_cmd`, `execute_dict_cmd`, `optimize` |
| `src/livekit_agent_simulator/ops.py` | Thread `environment` through all `load_config()` calls and all public ops that accept `profile` |
| `src/livekit_agent_simulator/mcp_server.py` | Add `environment: str \| None = None` param to `preflight`, `execute_scenario`, `execute_scenarios`, `execute_scenario_dict`, `optimize_persona` |
| `src/livekit_agent_simulator/preflight.py` | Thread `environment` to `run_preflight` → `load_config` |
| `src/livekit_agent_simulator/optimize/optimize.py` | Thread `environment` through `optimize_persona` and internal `load_config` calls |
| `src/livekit_agent_simulator/optimize/_backend.py` | Thread `environment` through `proposer_for` → `load_config` |

### Rust

| File | Change |
|------|--------|
| `src/livekit_agent_simulator_rust/crates/lks-core/src/config.rs` | Add `active_environment: Option<String>` to `LiveKitConfig`; add `apply_environment()` fn (port of Python); update `load_config()` signature to accept `environment: Option<&str>`; add `active_environment` to `config_snapshot` |
| `src/livekit_agent_simulator_rust/crates/lks-core/src/ops.rs` | Thread `environment` through `op_preflight_core` and all `load_config` calls |
| `src/livekit_agent_simulator_rust/crates/lks-livekit/src/preflight.rs` | Thread `environment` through `op_preflight` |
| `src/livekit_agent_simulator_rust/crates/lks-livekit/src/run.rs` | Add `environment: Option<String>` to `ExecuteOptions`; thread to `load_config` |
| `src/livekit_agent_simulator_rust/crates/lks-livekit/src/ops_execute.rs` | Add `environment: Option<String>` to `SuiteOptions`, `OptimizeOptions`; thread through `op_execute_scenarios`, `op_execute_scenario_dict`, `op_optimize_persona` |
| `src/livekit_agent_simulator_rust/crates/lks-mcp/src/lib.rs` | Add `pub environment: Option<String>` to `PreflightParams`, `ExecuteParams`, `ExecuteScenariosParams`, `ExecuteDictParams`, `OptimizeParams`; thread in handler functions |
| `src/livekit_agent_simulator_rust/crates/lks/src/main.rs` | Add `--environment` / `-e` arg to `Preflight`, `Execute`, `ExecuteAll`, `ExecuteDict`, `Optimize` commands; thread to `ExecuteOptions` / `SuiteOptions` / `OptimizeOptions` |
| `src/livekit_agent_simulator_rust/crates/lks-web/src/lib.rs` | Thread `environment` through `load_config` calls |

### Templates & docs

| File | Change |
|------|--------|
| `templates/config.yaml` | Add commented `environments:` block with local/production examples |
| `.agent-sim/config.yaml` | Add commented `environments:` block |
| `demo/base-agent/.agent-sim/config.yaml` | Add commented `environments:` block |
| `demo/dtmf-feature/.agent-sim/config.yaml` | Add commented `environments:` block |
| `docs/guide/installation.md` | Add `livekit.environments` section after profiles section; add `--environment` examples |
| `templates/GUIDE.md` | Add `livekit.environments` docs parallel to profiles docs |

### Tests

| File | Change |
|------|--------|
| `src/livekit_agent_simulator_rust/crates/lks-core/tests/profile_defaults.rs` | Add environment selection tests mirroring the profile tests |

---

## Implementation approach

### 1. `config.py` — `load_config()` environment resolution

Add to `LiveKitConfig`:
```python
active_environment: str | None = None
```

In `load_config()`, after parsing `lk_raw`, add env resolution logic (same pattern as `apply_profile` for simulator):
- Check `lk_raw.get("environments")` for a dict
- Resolve selected env name (explicit `environment` param > `default: true` > flat block)
- Merge selected env over flat `lk_raw` (env inherits unspecified keys from flat)
- Set `active_environment` on the resulting `LiveKitConfig`

### 2. `cli.py` — `ENVIRONMENT_OPTION`

```python
ENVIRONMENT_OPTION = typer.Option(
    None, "--environment", "-e",
    help="Named livekit.environments.<name> (url, api_key, agent_name). "
    "Omit to use the legacy flat `livekit:` block.",
)
```

Add `environment: Optional[str] = ENVIRONMENT_OPTION` to: `preflight`, `execute`, `execute_all_cmd`, `execute_dict_cmd`, `optimize`.

### 3. `ops.py` — Thread `environment`

Every function that calls `load_config()` or `preflight()` and already accepts `profile` gets `environment: str | None = None`. Key functions:
- `preflight()`, `_run_scenario_dict()`, `_resolve_caller_policy()`, `_run_scenario()`
- `execute_scenario_dict()`, `execute_scenario()`, `execute_scenarios()`, `optimize_persona()`
- Internal `load_config(project_root)` calls that don't use profile → add `environment=environment`

### 4. `mcp_server.py` — Add `environment` param

Add `environment: str | None = None` to tool signatures: `preflight`, `execute_scenario`, `execute_scenarios`, `execute_scenario_dict`, `optimize_persona`.

### 5. Rust `config.rs` — `apply_environment()`

Add `apply_environment()` function mirroring `apply_profile()`:
- Input: `lk_raw: Map`, `environment: Option<&str>`, `config_path`
- Output: `(Map, Option<String>)` — merged config + selected env name
- Same selection rules: explicit > default > flat; 2+ defaults → error

Update `load_config()` signature: `fn load_config(project_root: PathBuf, profile: Option<&str>, environment: Option<&str>)`.

### 6. Rust CLI `main.rs`

Add `#[arg(long, short = 'e')] environment: Option<String>` to each command variant that has `profile`.

### 7. Rust MCP `lib.rs`

Add `pub environment: Option<String>` to each params struct that has `profile`.

### 8. Documentation

**`templates/config.yaml`** — Add after the `livekit:` flat block:
```yaml
  # environments:
  #   local:
  #     default: true
  #     url: "wss://local.livekit.cloud"
  #     agent_name: "worker-local"
  #   production:
  #     url: "wss://prod.livekit.cloud"
  #     agent_name: "worker-prod"
```

**`docs/guide/installation.md`** — After the `simulator.profiles` section, add a parallel section for `livekit.environments`:
```markdown
**Switch LiveKit target without editing the file — `livekit.environments` + `--environment <name>`:**
...
```

**`templates/GUIDE.md`** — After the profiles section in §1 Config, add environments docs.

---

## Verification

1. **Python config test**: Write a config with `livekit.environments` and verify `load_config(path, environment="production")` returns the production URL/agent_name
2. **CLI smoke**: `lks preflight --environment production --no-connectivity` should show the production agent_name in checks
3. **Backward compat**: `lks preflight` with no `--environment` and no `environments:` in config should work unchanged
4. **Rust build**: `cargo build -p lks` compiles with the new `environment` field
5. **Rust tests**: Run `cargo test -p lks-core` — existing profile tests pass + new environment tests pass
6. **Doc review**: `lks guide` output includes environments section
