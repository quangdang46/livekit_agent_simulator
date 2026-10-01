"""A failed run must say WHY, not just that it did.

Regression for a silent-failure class: `cli_render` had an `error` column and a
`hard_reasons` list, but the run result carried no error at all, so a hard
failure printed

    status: failed    hard_reasons: ["status:failed"]

and the actual cause lived one layer down in `reports/<run-id>/events.jsonl`.
Once: `CALLER_BEHAVIOR_VIOLATION: LOW_CONFIDENCE` cost a bisect and a ~70s paid
call to diagnose.

These tests drive `diagnose_failure` over the REAL event shapes the failure
paths emit, and the real `EventWriter`, so a change to the event contract fails
here rather than silently reverting the column to a dash.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from livekit_agent_simulator.logging.event_writer import EventWriter
from livekit_agent_simulator.run_orchestrator import diagnose_failure


def _writer(tmp_path: Path) -> EventWriter:
    return EventWriter("001-x", tmp_path / "report", timezone_name="UTC")


def test_the_top_level_handler_failure_path_is_reported(tmp_path: Path) -> None:
    """`ContractDriverFailure` propagates to the outer handler, which emits
    `run.error` — not `sim.error`. Matching on the wrong kind would have missed
    exactly the failure this was written for."""
    w = _writer(tmp_path)
    w.emit(
        "run.error",
        spec={"error": "ContractDriverFailure: CALLER_BEHAVIOR_VIOLATION: LOW_CONFIDENCE", "mode": "outbound_human_pickup"},
        include_dialogue=False,
    )
    got = diagnose_failure("failed", w.events)
    assert got is not None
    assert "CALLER_BEHAVIOR_VIOLATION" in got
    assert "LOW_CONFIDENCE" in got
    assert "outbound_human_pickup" in got, "the mode names where it happened"


def test_the_audio_finalize_path_is_reported(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    w.emit(
        "sim.error",
        spec={"where": "audio_finalize", "error": "OSError: device unavailable"},
        include_dialogue=False,
    )
    got = diagnose_failure("failed", w.events)
    assert got is not None
    assert "OSError" in got
    assert "audio_finalize" in got


def test_the_connect_paths_are_reported(tmp_path: Path) -> None:
    for kind in ("sim.leg_error", "dispatch.agent_timeout"):
        w = _writer(tmp_path)
        w.emit(kind, spec={"error": "no route to room", "mode": "inbound_sip"}, include_dialogue=False)
        got = diagnose_failure("failed", w.events)
        assert got is not None, f"{kind} carries an error and must be reported"
        assert "no route to room" in got


def test_the_most_recent_error_wins(tmp_path: Path) -> None:
    # A late failure is the one that ended the run; an earlier one is history.
    w = _writer(tmp_path)
    w.emit("sim.error", spec={"where": "audio_finalize", "error": "first"}, include_dialogue=False)
    w.emit("run.error", spec={"error": "second", "mode": "outbound_sim_callee"}, include_dialogue=False)
    got = diagnose_failure("failed", w.events)
    assert got is not None and "second" in got


def test_success_reports_nothing_even_with_events_present(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    w.emit("run.error", spec={"error": "recovered later"}, include_dialogue=False)
    assert diagnose_failure("done", w.events) is None


def test_a_failure_with_nothing_diagnosable_is_none_not_a_guess(tmp_path: Path) -> None:
    # Never invent a reason. The CLI renders a dash; that is honest.
    w = _writer(tmp_path)
    w.emit("run.started", spec={"scenario_id": "r1"}, include_dialogue=False)
    assert diagnose_failure("failed", w.events) is None


def test_an_assert_failure_without_an_error_event_is_still_named(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    got = diagnose_failure("failed", w.events, {"assert_failed": True})
    assert got == "assert verify failed"


def test_events_without_a_spec_are_tolerated(tmp_path: Path) -> None:
    # EventWriter always sets spec, but a hand-built or legacy event may not.
    # A malformed entry must not abort the scan.
    w = _writer(tmp_path)
    w.emit("run.error", spec={"error": "the real cause"}, include_dialogue=False)
    assert "the real cause" in (diagnose_failure("failed", [{}, {"spec": None}, *w.events]) or "")


# ---------------------------------------------------------------------------
# a failure must point at its own evidence
# ---------------------------------------------------------------------------


def test_a_failure_message_names_its_report_dir(tmp_path) -> None:
    """A run that wrote 40 events reported as having written none.

    Measured on the target repo, 2026-09-30: a read timeout produced
    `TimeoutError: The read operation timed out (in webrtc_sim)` and nothing
    else. The report next to it held 40 events including a
    `contract.turn_published` and a `judge.verdict`. A peer session read the
    one line the CLI prints and reported the run as evidence-free, which cost a
    round trip to find evidence that was already on disk.
    """
    from livekit_agent_simulator.run_orchestrator import _with_report_pointer

    report = tmp_path / "068-gpt-live-happy-path-20260930-070804-ba93"
    report.mkdir()
    (report / "events.jsonl").write_text('{"kind": "run.error"}\n', encoding="utf-8")

    message = _with_report_pointer("TimeoutError: The read operation timed out (in webrtc_sim)", report)

    assert message is not None
    assert str(report) in message, "the failure must name where its evidence is"
    assert "TimeoutError" in message, "the cause must survive the pointer"


def test_a_success_carries_no_pointer(tmp_path) -> None:
    from livekit_agent_simulator.run_orchestrator import _with_report_pointer

    assert _with_report_pointer(None, tmp_path) is None


def test_the_cli_enables_faulthandler_before_doing_anything() -> None:
    """A native crash produces no traceback and no `run.error`.

    Measured on the target repo, 2026-09-30: `uv run lks execute` exited 139
    (SIGSEGV) with three events on disk and nothing else. A live run holds two
    native extensions in-process — `livekit-rtc` and the sherpa-onnx TTS
    runtime — and the exit code alone cannot say which faulted.

    The check reads the source rather than calling `main()`: calling it would
    start the background updater and dispatch a command, and the property
    under test is an ordering one (enabled FIRST), which is only visible in
    the source.
    """
    import inspect

    from livekit_agent_simulator import cli

    source = inspect.getsource(cli.main)
    assert "faulthandler.enable" in source, (
        "the CLI must enable faulthandler or a native crash leaves no trace"
    )
    enable_at = source.index("faulthandler.enable")
    work_at = source.index("start_background_check")
    assert enable_at < work_at, (
        "faulthandler must be enabled before any work starts — the run that "
        "needs it is the one that cannot be instrumented in advance"
    )


async def test_a_failed_room_delete_is_recorded_and_does_not_stop_the_rest() -> None:
    """A surviving room is the failure mode nobody is told about.

    `delete_room` was the one unguarded step in the post-run cleanup, so a
    failure propagated and skipped everything after it. A bare `except: pass`
    would be worse in the other direction: a surviving room still holds a
    LiveKit dispatch job, and a worker still holding one may refuse to
    initialise a runner for the next dispatch — the shape of the
    `AgentJoinTimeout` reports on the target repo. So it must be loud.
    """
    from livekit_agent_simulator.run_orchestrator import _cleanup_rooms

    class _ExplodingAdapter:
        def __init__(self) -> None:
            self.attempted: list[str] = []

        async def delete_room(self, room_name: str) -> None:
            self.attempted.append(room_name)
            raise RuntimeError("room not found")

    class _Writer:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        def emit(self, kind, spec=None, **kwargs) -> None:
            self.events.append((kind, spec or {}))

    class _Leg:
        rooms_to_delete = ["room-a", "room-b"]

        async def disconnect_rooms(self) -> None:
            return None

    adapter, writer = _ExplodingAdapter(), _Writer()
    await _cleanup_rooms(adapter, _Leg(), {}, writer)

    # Both rooms attempted: one failure must not abandon the rest.
    assert adapter.attempted == ["room-a", "room-b"]
    errors = [spec for kind, spec in writer.events if kind == "sim.error"]
    assert len(errors) == 2, f"each failed room must be recorded: {errors}"
    assert all(e["where"] == "delete_room" for e in errors)
    assert {e["room"] for e in errors} == {"room-a", "room-b"}, (
        "the message must name which room survived — an unnamed failure is one "
        "nobody can act on"
    )
    assert "dispatch job" in errors[0]["note"]


async def test_a_disconnect_failure_does_not_prevent_deletion() -> None:
    """Order matters: disconnect fails, delete still runs."""
    from livekit_agent_simulator.run_orchestrator import _cleanup_rooms

    class _Adapter:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def delete_room(self, room_name: str) -> None:
            self.deleted.append(room_name)

    class _Writer:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        def emit(self, kind, spec=None, **kwargs) -> None:
            self.events.append((kind, spec or {}))

    class _Leg:
        rooms_to_delete = ["only-room"]

        async def disconnect_rooms(self) -> None:
            raise RuntimeError("already gone")

    adapter, writer = _Adapter(), _Writer()
    await _cleanup_rooms(adapter, _Leg(), {}, writer)

    assert adapter.deleted == ["only-room"]
    kinds = [k for k, _ in writer.events]
    assert "sim.error" in kinds
    where = [s.get("where") for k, s in writer.events if k == "sim.error"]
    assert where == ["disconnect_rooms"]


def test_a_cancelled_run_still_writes_evidence() -> None:
    """A run that vanishes leaves nothing to investigate.

    Measured on the target repo, 2026-09-30: runs 118 and 119 wrote exactly
    three events — run.started, dispatch.created, dispatch.agent_joined — and
    stopped. No sim.connected, no contract.*, and no run.error at all.

    Cause: `asyncio.CancelledError` is a BaseException, not an Exception, so the
    `except Exception` in `run_scenario_instance` does not catch it and it
    walks past every handler in the function.
    """
    import asyncio
    import inspect

    from livekit_agent_simulator import run_orchestrator

    assert not issubclass(asyncio.CancelledError, Exception), (
        "Python moved CancelledError back under Exception; the explicit clause "
        "is then redundant but harmless, and this note would be wrong"
    )

    source = inspect.getsource(run_orchestrator.run_scenario_instance)
    cancelled = source.index("except asyncio.CancelledError")
    # The generic clause must be the one AFTER the cancellation clause. This
    # function has several `except Exception` blocks, so a plain .index finds
    # the first one anywhere and orders the comparison wrongly.
    # Indented form, not the bare string: the CancelledError clause's own
    # comment contains the words "except Exception", and a plain .index finds
    # that instead of the real clause.
    generic = source.index("\n        except Exception", cancelled)
    assert cancelled < generic, (
        "the CancelledError clause must come first — BaseException subclasses "
        "are matched in order"
    )
    # It must record, not swallow.
    window = source[cancelled:generic]
    assert '"run.error"' in window, (
        "a cancelled run must still write run.error, or the report is empty"
    )
    assert "raise" in window, (
        "swallowing a cancellation leaves the task silently unfinished"
    )
    # The generic clause keeps its original behaviour: record, mark failed,
    # and carry on so the report is still written.
    assert 'status = "failed"' in source[generic : generic + 400]


# ---------------------------------------------------------------------------
# the bounded sim-leg retry — as behaviour, not as source text
# ---------------------------------------------------------------------------


class _Writer:
    """Mirrors `EventWriter`: `events` holds DICTS with a "spec" key.

    The first version stored `(kind, spec)` tuples, and the retry helper —
    which correctly reads `ev.get("spec")` like the rest of the module — blew
    up on them. The fake has to match the real shape or it tests nothing.
    """

    def __init__(self, events=()) -> None:
        self.events: list[dict] = [
            e if isinstance(e, dict) else {"kind": e[0], "spec": e[1]}
            for e in events
        ]
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, kind, spec=None, include_dialogue=True, **_):
        self.events.append({"kind": kind, "spec": spec or {}})
        self.emitted.append((kind, spec or {}))


class _Leg:
    """Fails `fail_times` times with `exc`, then succeeds."""

    def __init__(self, *, fail_times=1, exc=None):
        self.fail_times = fail_times
        self.exc = exc or TimeoutError("The read operation timed out")
        self.calls = 0

    async def connect(self, _ctx):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return SimpleNamespace(agent_room_name="r", sim_room_name="r")


async def test_a_timeout_before_any_speech_is_retried_once():
    """The case that actually happens, and the one worth retrying.

    Measured on the target repo, 2026-09-30: the read timeout ended runs 066,
    068, 112, 114 and damaged 115. A call that never spoke cost a room and a
    join, so one more attempt is cheap.
    """
    from livekit_agent_simulator.run_orchestrator import _connect_leg_with_retry

    leg, writer = _Leg(fail_times=1), _Writer()
    handle = await _connect_leg_with_retry(leg, object(), writer, caller_mode="webrtc_sim")

    assert handle is not None
    assert leg.calls == 2, "exactly one retry"
    kinds = [k for k, _ in writer.emitted]
    assert kinds == ["sim.leg_retry"], writer.emitted
    assert writer.emitted[0][1]["attempt"] == 1


async def test_a_timeout_is_not_retried_twice():
    """Bounded at one. An unbounded retry against a dead leg is a hang."""
    from livekit_agent_simulator.run_orchestrator import _connect_leg_with_retry

    leg, writer = _Leg(fail_times=5), _Writer()
    with pytest.raises(TimeoutError):
        await _connect_leg_with_retry(leg, object(), writer, caller_mode="webrtc_sim")

    assert leg.calls == 2, f"retried {leg.calls - 1} times; must be exactly one"
    last = writer.emitted[-1]
    assert last[0] == "sim.leg_error"
    assert last[1]["attempts"] == 2
    assert last[1]["retry_skipped_because_transcribed"] is False


async def test_a_call_that_already_spoke_is_never_retried():
    """The money condition.

    A call that has transcribed anything already paid for the OpenAI realtime
    session. Reconnecting would pay twice for the same run. Measured: run 134
    hit a read timeout AFTER turns were transcribed, and the retry correctly
    stood down — that branch was exercised by a real run; this one now covers
    the other direction, where it must NOT retry.
    """
    from livekit_agent_simulator.run_orchestrator import _connect_leg_with_retry

    leg = _Leg(fail_times=1)
    writer = _Writer([("transcript.agent.final", {"text": "hi"})])
    with pytest.raises(TimeoutError):
        await _connect_leg_with_retry(leg, object(), writer, caller_mode="webrtc_sim")

    assert leg.calls == 1, "retried a call that had already spoken"
    last = writer.emitted[-1]
    assert last[0] == "sim.leg_error"
    assert last[1]["retry_skipped_because_transcribed"] is True
    assert not [k for k, _ in writer.emitted if k == "sim.leg_retry"]


async def test_a_deliberate_timeout_is_not_retried():
    """`AgentJoinTimeout` is a configuration problem, not a flaky wire.

    Retrying it does not fix it — it just hides it for longer.
    """
    from livekit_agent_simulator.livekit.adapter import AgentJoinTimeout
    from livekit_agent_simulator.run_orchestrator import _connect_leg_with_retry

    leg = _Leg(fail_times=1, exc=AgentJoinTimeout("never joined"))
    writer = _Writer()
    with pytest.raises(AgentJoinTimeout):
        await _connect_leg_with_retry(leg, object(), writer, caller_mode="webrtc_sim")

    assert leg.calls == 1
    assert writer.emitted[-1][0] == "dispatch.agent_timeout"


async def test_a_clean_first_attempt_emits_nothing():
    """The retry must be invisible on a healthy run."""
    from livekit_agent_simulator.run_orchestrator import _connect_leg_with_retry

    leg, writer = _Leg(fail_times=0), _Writer()
    await _connect_leg_with_retry(leg, object(), writer, caller_mode="webrtc_sim")
    assert leg.calls == 1
    assert writer.emitted == []
