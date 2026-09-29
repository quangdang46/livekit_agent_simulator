"""Verify plugins must actually run on the contract (caller_steps) path.

Regression for the silent false-pass: run_orchestrator skipped script_verify
wholesale on the contract path, so a verify plugin loaded, registered, and
never executed - an impossible assertion still reported ok:true.

The load-bearing assertion here is NEGATIVE in spirit: an unsatisfiable plugin
must make the run FAIL. A test that only checks the plugin ran would pass
against the old code too, because the old code also "ran" it - silently, in the
no-op sense.
"""

from __future__ import annotations

import types

import pytest

from livekit_agent_simulator.plugins.api import VerifyContext
from livekit_agent_simulator.plugins.registry import register_verify
from livekit_agent_simulator.script.verify import run_verify_plugins


def _checks_from(plugins, *, options=None, events=None, scenario=None):
    from livekit_agent_simulator.script.models import ScriptVerifySpec

    spec = ScriptVerifySpec(plugins=list(plugins), plugin_options=dict(options or {}))
    return run_verify_plugins(
        spec,
        # ensure_plugins_loaded reads scenario.plugin_modules
        scenario=scenario if scenario is not None else types.SimpleNamespace(plugin_modules=[]),
        project_root=".",
        events=events or [],
        steps=(),
    )


def test_an_unsatisfiable_plugin_reports_a_failing_check():
    register_verify("t_always_fails", lambda ctx: {"pass": False, "checks": [{"check": "impossible", "pass": False, "reason": "never reached"}]})
    checks = _checks_from(["t_always_fails"])
    failed = [c for c in checks if c.get("pass") is False]
    assert failed, "an unsatisfiable plugin must produce a failing check"
    # This is what run_orchestrator keys on to fail the run.
    assert any(c.get("check") == "impossible" for c in failed)


def test_a_satisfiable_plugin_reports_a_passing_check():
    register_verify("t_always_passes", lambda ctx: {"pass": True, "checks": [{"check": "ok", "pass": True}]})
    checks = _checks_from(["t_always_passes"])
    assert not [c for c in checks if c.get("pass") is False]


def test_an_unregistered_plugin_is_reported_not_skipped():
    checks = _checks_from(["t_definitely_not_registered"])
    assert any("not registered" in str(c.get("reason")) for c in checks)


def test_no_plugins_returns_nothing():
    assert _checks_from([]) == []


def test_plugin_options_are_read_from_a_flat_map_keyed_by_plugin_name():
    # The scenario shape is a FLAT map keyed by plugin name, not a nested
    # block. Getting this wrong makes a plugin see no options and quietly
    # assert less than intended.
    seen: dict = {}

    def _capture(ctx):
        seen.update(ctx.options or {})
        return {"pass": True, "checks": []}

    register_verify("t_captures_options", _capture)
    _checks_from(
        ["t_captures_options"],
        options={"t_captures_options": {"expected": ["1-a"]}},
        scenario=types.SimpleNamespace(plugin_modules=[]),
    )
    assert seen == {"expected": ["1-a"]}, f"options did not reach the plugin: {seen}"


def test_a_plugin_that_raises_becomes_a_failing_check_not_a_crash():
    def _boom(ctx):
        raise ValueError("plugin exploded")

    register_verify("t_raises", _boom)
    checks = _checks_from(["t_raises"])
    assert any("plugin exploded" in str(c.get("reason")) for c in checks)


def test_the_contract_branch_calls_the_plugin_loop():
    # Guards the actual wiring, not just the helper: if someone restores the
    # wholesale skip, this fails even though run_verify_plugins still works.
    import inspect

    from livekit_agent_simulator import run_orchestrator

    src = inspect.getsource(run_orchestrator)
    assert "run_verify_plugins(" in src, "the contract branch must call run_verify_plugins"
    # the old unconditional skip is gone
    assert "script.verify checks the" not in src, "the blanket skip reason is back"
    assert "plugin_checks" in src, "plugin checks must be persisted in the result"
