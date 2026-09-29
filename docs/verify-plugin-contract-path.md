# Bug — verify plugins never run on the `caller_steps` (contract) path

**Severity:** medium (silent false-pass — the worst kind for a test harness)
**Component:** `livekit_agent_simulator` plugin system
**Status:** open, unfixed
**Found:** 2026-09-29, while wiring GPT-Live Conversation Flow E2E scenarios
**Affects:** every scenario that uses `caller_steps:` — which is *all* of them on the current code path

---

## 1. Summary

`script_verify` — and therefore every registered verify plugin — is **structurally unreachable**
for `caller_steps` scenarios. The orchestrator explicitly marks it `skipped` before it can run.

A verify plugin that is correctly named, correctly configured, and correctly loaded still
**never executes**, and the scenario reports `ok: true` with an empty hard-reason list. A
deliberately unsatisfiable assertion passes silently.

---

## 2. Root cause

`run_orchestrator.py:571-577` hardcodes the skip:

```python
script_verify = {
    "skipped": True,
    "reason": (
        "caller_steps drives the contract path; script.verify checks the "
        "legacy sim.script.cue vocabulary that this path never emits"
    ),
    "steps_not_applicable": [s.id for s in scenario.script_steps],
    ...
}
```

`evaluate_script_log` (`script/verify.py:10`) is the **only** caller of registered verify plugins
(`script/verify.py:199-207`). It is imported at `run_orchestrator.py:42` and called only at
`run_orchestrator.py:582` — inside the `script_steps` branch, which the skip above bypasses.

The skip is defensible for the *step-matching* half of `evaluate_script_log`: it matches
`step_id`s against `sim.script.cue` events, and the contract path emits `contract.*` instead, so
step matching genuinely does not apply. But the **plugin half has no such coupling** — a verify
plugin consumes the run's `events`, not step ids. It is being skipped for a reason that does not
apply to it.

Compounding this, the legacy `script_steps` caller path was removed
(`run_orchestrator.py:399-410` — *"caller_contract single path (the ONLY caller path)"*), so
there is currently **no** scenario shape that reaches verify plugins at all.

---

## 3. Evidence

A verify plugin with one impossible assertion:

```yaml
plugin_modules:
  - gpt_live_queue_verify
script_verify:
  plugins:
    - gpt_live_queue
  plugin_options:
    gpt_live_queue:
      expected_node_order:
        - '1-a'
        - '1-IMPOSSIBLE'      # no code path reaches this node
```

Result:

```
ok       ✓
status   done
meta.json → plugins_loaded.verify_plugins: ["gpt_live_queue"]   # loaded fine
meta.json → run_spec.script_verify:       undefined             # never reached runtime
```

The plugin loads and registers. Its output appears in no artifact. The assertion passes.

---

## 4. What *does* work on the contract path

`assert.outcomes` (native, e.g. `transcript_contains`) **is** evaluated and **does** fail the run.
Observed failures on runs 038/040/041 traced to `outcome:` checks in `summary.assert_verify`.

So the harness has a working gate — it just cannot express structured, event-based assertions,
because it is limited to transcript text matching.

---

## 5. Proposed fix

Make the contract path invoke verify plugins, while leaving the legacy step-matching skipped.

In `run_orchestrator.py`, the block that builds `script_verify = {"skipped": True, ...}` for the
contract path should instead:

1. Keep `steps_not_applicable` / the step-matching half skipped (that part is genuinely
   inapplicable).
2. Resolve and run any `scenario.script_verify.plugins` through the same call site the
   `script_steps` branch uses — i.e. call `evaluate_script_log` (or extract its plugin loop into a
   helper, e.g. `run_verify_plugins(verify, scenario, project_root, events)`) and merge only the
   plugin-originated checks into the contract path's result.
3. Feed those checks into `hard_reasons` on failure, so a failed plugin fails the run exactly as
   a failed `assert.outcomes` does today.

Suggested shape — the plugin half of `script/verify.py:194-240` is already self-contained:

```python
def run_verify_plugins(verify, *, scenario, project_root, events, steps) -> list[dict]:
    """Run registered verify plugins. Independent of the legacy step-matching
    half, so it applies on the contract path too."""
    ...  # body lifted from script/verify.py:194-240
```

Then in the contract branch:

```python
script_verify = {
    "skipped": True,                      # step-matching only
    "reason": "caller_steps drives the contract path; step matching does not apply",
    "steps_not_applicable": [...],
    "plugin_checks": run_verify_plugins(
        scenario.script_verify, scenario=scenario,
        project_root=project_root, events=events, steps=[],
    ),
}
```

### Risks

- Touches orchestrator core; every `caller_steps` scenario is affected. Existing scenarios have no
  `script_verify.plugins`, so `run_verify_plugins` returns `[]` for them and behaviour is unchanged.
- `plugin_options` shape must be preserved: `verify.plugin_options.get(plugin_name, {})`
  (`script/verify.py:229`) expects a **flat map keyed by plugin name**, not a nested block.
- `VerifyContext` requires both `scenario` and `project_root` to be non-`None`
  (`script/verify.py:216-225`); the contract path has both.

---

## 6. Two naming traps (cost real time; worth documenting)

Both are currently silent — the scenario parser tolerates unknown keys, so a misnamed field
produces a run that quietly does less than intended.

| Trap | Wrong | Right |
|---|---|---|
| Spec block key | `verify:` | `script_verify:` (`scenario.py:175` → `ScriptVerifySpec` in `script/models.py:258`) |
| Plugin list location | inside `script_verify` | top-level `plugin_modules:` (`scenario_from_dict.py:119`) |

The loader **does not scan** `.agent-sim/plugins/`. It takes explicit names and opens exactly
`<name>.py` (`plugins/loader.py:68-73`), so the module name must match the filename —
`gpt_live_queue_verify` ⇒ `.agent-sim/plugins/gpt_live_queue_verify.py`.

Note that `ops.py:305-312` *does* warn on unregistered plugin names, but only when
`script_verify` or `plugin_modules` is present — a scenario that misspells the block key entirely
gets no warning at all.

---

## 7. Acceptance criteria

1. A verify plugin with a deliberately unsatisfiable assertion causes the run to **fail**, with
   the plugin's reason in `hard_reasons`.
2. A verify plugin with satisfiable assertions still passes and does not alter existing runs.
3. Scenarios without `plugin_modules` behave exactly as before.
4. `lks validate` warns when `script_verify.plugins` names an unregistered plugin — already
   implemented at `ops.py:308-310`; confirm it fires once the block key is correct.
5. Plugin check output is persisted in the run artifacts (currently it appears nowhere, which is
   what made this bug hard to see).

---

## 8. Workaround for callers (today)

Until this is fixed, structured event-based assertions are not expressible in a scenario. Options:

- **Assert natively** with `assert.outcomes` — works, but limited to transcript text, which is
  unstable on a paraphrasing transport (e.g. OpenAI GPT-Live reword the same question every call).
- **Read the run artifacts directly** — `reports/<run>/events.jsonl` carries structured flow
  events (`flow_node_active`, `flow_transition`,
  `flow_superseded_turn_completion_dropped`, …) and is stable. An external summariser over
  `events.jsonl` works today, at the cost of not being part of the pass/fail gate.
