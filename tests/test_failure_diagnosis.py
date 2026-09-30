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
