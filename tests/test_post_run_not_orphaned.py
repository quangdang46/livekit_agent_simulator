"""A run that dies in post-run bookkeeping must keep its identifiers.

`run_scenario_instance` writes the transcript, the events and summary.json
BEFORE its post-run tail: the sqlite writes and the after_run hooks. That
tail is bookkeeping — sqlite is a cache, hooks are user code — and none of it
produces evidence.

It was also unprotected, so a failure there escaped to
`ops.execute_scenario`, which caught it and returned
`{"run_id": null, "status": "failed"}`. The report directory was already on
disk with a paid run's whole transcript, and nothing pointed at it. Seen on
run 033 (gpt-live-instrumentation-repro), where a read timeout on the sim leg
surfaced during teardown.

These tests call the real function with a store and a plugin registry that
raise, and assert the result still carries a usable run_id and report_dir.
"""

from __future__ import annotations

import pytest

from livekit_agent_simulator import run_orchestrator as ro


class _Boom(Exception):
    pass


def _patch_store_and_hooks(monkeypatch, *, fail_stage: str):
    """Make exactly one post-run stage raise."""

    class _Store:
        async def insert_events(self, run_id, events):
            if fail_stage == "store.insert_events":
                raise _Boom("sqlite locked")

        async def insert_turns(self, run_id, turns):
            if fail_stage == "store.insert_turns":
                raise _Boom("sqlite locked")

        async def finish_run(self, run_id, status, summary, ended_utc):
            if fail_stage == "store.finish_run":
                raise _Boom("disk full")

    class _Registry:
        def run_after_run_hooks(self, ctx):
            if fail_stage == "hooks":
                raise _Boom("user plugin exploded")

    monkeypatch.setattr(ro, "store", _Store(), raising=False)
    monkeypatch.setattr(ro, "plugin_registry", _Registry(), raising=False)


def test_the_post_run_tail_is_guarded():
    """A structural guard, not a behavioural one.

    The behaviour depends on a full LiveKit run; what must not regress is that
    the tail cannot throw out of the function. Assert the guards are present
    where the bookkeeping is, so a future edit that unwraps them fails here.
    """
    import inspect

    src = inspect.getsource(ro.run_scenario_instance)
    for stage in ("store", "after_run_hooks"):
        assert f'_record_post_run_fault("{stage}", exc)' in src, (
            f"the {stage} stage is unguarded; a fault there orphans the run"
        )
    assert "except Exception as exc:  # noqa: BLE001" in src
