"""Tests for `lks` rich-table renderers + the `--json` flag.

IMPORTANT: every test here is a plain ``def test_...`` (never ``async def``).
``pytest.ini`` sets ``asyncio_mode = "auto"``, so async tests run inside an
event loop; the async-backed CLI commands call ``asyncio.run()`` internally,
which raises "cannot be called from a running event loop". Render functions
are sync and the CliRunner smoke tests target sync commands only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from livekit_agent_simulator import cli_render as cr


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------


def test_fmt_ms() -> None:
    assert cr.fmt_ms(None) == "−"
    assert cr.fmt_ms(350) == "350ms"
    assert cr.fmt_ms(1500) == "1.5s"
    assert cr.fmt_ms(1500.0) == "1.5s"


def test_fmt_bool_tristate() -> None:
    assert cr.fmt_bool(True) == "✓"
    assert cr.fmt_bool(False) == "✗"
    assert cr.fmt_bool(None) == "−"
    assert cr.fmt_tristate(None) == "−"
    assert cr.fmt_tristate(True) == "✓"
    assert cr.fmt_tristate(False) == "✗"
    assert cr.fmt_tristate("skipped") == "−"


def test_fmt_list_truncate_pct() -> None:
    assert cr.fmt_list(["a", "b"]) == "a, b"
    assert cr.fmt_list([]) == "−"
    assert cr.fmt_list(None) == "−"
    assert cr.truncate("x" * 100, 10) == "x" * 9 + "…"
    assert cr.truncate(None) == "−"
    assert cr.fmt_pct(0.87) == "87%"
    assert cr.fmt_pct(None) == "−"


def test_fmt_utc() -> None:
    assert cr.fmt_utc("2026-08-07T03:04:05.123+00:00") == "2026-08-07 03:04:05"
    assert cr.fmt_utc(None) == "−"


def test_render_plain_no_ansi() -> None:
    out = cr.render_text(cr.render_scenarios, [{"id": "x", "file": "x.yaml"}])
    assert "\x1b[" not in out


# ---------------------------------------------------------------------------
# render unit tests (inline sample dicts, no disk / DB)
# ---------------------------------------------------------------------------


def test_render_scenarios_valid_and_invalid() -> None:
    rows = [
        {
            "id": "smoke-hello",
            "file": "smoke-hello.yaml",
            "locale": "en-US",
            "tags": ["smoke"],
            "max_turns": 2,
            "first_speaker": "user",
            "has_execute": True,
            "has_dispatch": False,
            "caller_mode": "realtime",
            "pass_criteria": 2,
            "script_steps": 0,
        },
        {"id": None, "file": "broken.jsonl", "error": "parse error boom"},
    ]
    out = cr.render_text(cr.render_scenarios, rows)
    assert "smoke-hello" in out
    assert "caller_mode" in out
    assert "✓" in out  # has_execute
    assert "✗" in out  # has_dispatch False AND invalid row marker
    assert "parse error boom" in out


def test_render_scenarios_empty() -> None:
    assert "no scenarios" in cr.render_text(cr.render_scenarios, [])


def test_render_runs() -> None:
    rows = [
        {
            "run_id": "001-x",
            "status": "done",
            "scenario_id": "smoke-hello",
            "room_name": "lks-001-x",
            "agent_name": "voice",
            "started_utc": "2026-08-07T03:04:05.123+00:00",
            "duration_ms": 1200,
            "turn_count": 5,
            "tool_errors": 0,
            "verdict": "pass",
        }
    ]
    out = cr.render_text(cr.render_runs, rows)
    assert "001-x" in out
    assert "done" in out
    assert "1.2s" in out
    assert "2026-08-07 03:04:05" in out


def test_render_preflight() -> None:
    data = {
        "ok": False,
        "checks": [
            {"name": "config", "status": "pass", "detail": "ok"},
            {"name": "livekit.url", "status": "fail", "detail": "bad ws url"},
        ],
    }
    out = cr.render_text(cr.render_preflight, data)
    assert "config" in out
    assert "livekit.url" in out
    assert "✓" in out
    assert "✗" in out
    assert "preflight failed" in out


def test_render_execute_iterations() -> None:
    data = {
        "executed": True,
        "validation": {"valid": True, "id": "smoke-hello"},
        "repeat": 2,
        "pass_at_k": 1,
        "hard_passes": 2,
        "ok": True,
        "run_id": "001-x",
        "status": "done",
        "iterations": [
            {
                "i": 1,
                "run_id": "001-x",
                "status": "done",
                "gate": "pass",
                "ok": True,
                "ttfw_ms": 2200,
                "turn_p50_ms": 800,
                "turn_p95_ms": 1500,
                "hard_reasons": [],
            }
        ],
    }
    out = cr.render_text(cr.render_execute, data)
    assert "execute" in out
    assert "iterations" in out
    assert "2.2s" in out  # ttfw
    assert "800ms" in out  # p50
    assert "1.5s" in out  # p95


def test_render_execute_all_matrix() -> None:
    data = {
        "suite": {
            "ok": True,
            "exit_code": 0,
            "totals": {"total": 1, "passed_gate": 1, "failed_hard": 0, "failed_soft_judge": 0},
        },
        "results": [
            {
                "scenario_id": "smoke-hello",
                "gate": "pass",
                "status": "done",
                "assert_pass": True,
                "script_pass": True,
                "judge_verdict": "pass",
                "turn_p50_ms": 800,
                "turn_p95_ms": 1500,
                "ttfw_ms": 2200,
                "duration_ms": 30000,
                "run_id": "001-x",
            }
        ],
    }
    out = cr.render_text(cr.render_execute_all, data)
    for col in ("scenario", "gate", "status", "assert", "script", "judge", "p50", "p95", "ttfw", "duration", "run_id"):
        assert col in out
    assert "suite ok" in out


def test_render_compare_deltas() -> None:
    data = {
        "a": {"run_id": "A", "duration_ms": 1000, "ttfw_ms": 2000, "turn_count": 3, "barge_recovery_rate": 0.9},
        "b": {"run_id": "B", "duration_ms": 1500, "ttfw_ms": 2500, "turn_count": 3, "barge_recovery_rate": 0.8},
        "gate": {"ok": False, "reasons": ["ttfw regression"], "checks": [{"check": "regression:ttfw_ms", "pass": False}]},
    }
    out = cr.render_text(cr.render_compare, data)
    assert "A → B" in out
    assert "+500ms" in out  # duration delta
    assert "gate fail" in out


def test_render_validate_invalid() -> None:
    out = cr.render_text(
        cr.render_validate,
        {"valid": False, "error": "not found", "available": ["smoke-hello.yaml"]},
    )
    assert "✗ not valid" in out
    assert "not found" in out
    assert "smoke-hello.yaml" in out


def test_render_status() -> None:
    data = {
        "found": True,
        "run_id": "001-x",
        "status": "done",
        "scenario_id": "smoke-hello",
        "room_name": "lks-001-x",
        "started_utc": "2026-08-07T03:04:05",
        "ended_utc": "2026-08-07T03:05:05",
        "duration_ms": 60000,
        "turn_count": 5,
        "tool_errors": 0,
        "report_dir": "/x/.agent-sim/reports/001-x",
    }
    out = cr.render_text(cr.render_status, data)
    assert "001-x" in out
    assert "60.0s" in out


def test_render_status_not_found() -> None:
    out = cr.render_text(cr.render_status, {"found": False, "run_id": "nope"})
    assert "not found" in out


def test_render_report() -> None:
    data = {
        "found": True,
        "run_id": "001-x",
        "summary": {
            "status": "done",
            "duration_ms": 60000,
            "turn_count": 5,
            "event_count": 100,
            "tool_calls": 10,
            "tool_errors": 1,
            "interruptions": 2,
            "silences": 1,
            "caller_mode": "realtime",
            "end_reason": "hang_up",
            "verdict": {"verdict": "pass"},
            "metrics": {
                "ttfw_ms": 2200,
                "turn_taking_ms": {"p50": 800, "p95": 1500},
                "recovery_ms": {"p50": 400},
                "barge_count": 2,
                "barge_recovery_rate": 0.9,
            },
            "assert_verify": {"pass": True},
            "script_verify": {"pass": True},
        },
        "meta": {},
        "suspicious_turns": [
            {"turn": 3, "turn_taking_ms": 5000, "tool_errors": 1, "interrupted": True, "reasons": ["slow turn-taking"]}
        ],
        "timeline_path": "/x/timeline.md",
        "events_path": "/x/events.jsonl",
        "audio_path": "/x/conversation.wav",
    }
    out = cr.render_text(cr.render_report, data)
    assert "001-x" in out
    assert "60.0s" in out
    assert "suspicious turns" in out
    assert "slow turn-taking" in out
    assert "timeline" in out


def test_render_log() -> None:
    data = {
        "found": True,
        "run_id": "001-x",
        "total_events": 3,
        "matched": 2,
        "truncated": False,
        "events": [
            {"ts_mono_ms": 1000, "turn": 1, "kind": "transcript.user.final", "source": "caller", "spec": {"text": "Hello there"}},
            {"ts_mono_ms": 5000, "turn": 1, "kind": "tool.start", "source": "agent", "spec": {"name": "lookup", "duration_ms": 300}},
        ],
    }
    out = cr.render_text(cr.render_log, data)
    assert "2/3 events" in out
    assert "transcript.user.final" in out
    assert "tool.start" in out
    assert "Hello there" in out


# ---------------------------------------------------------------------------
# CliRunner smoke tests (sync commands only — async ops run via asyncio.run)
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    from livekit_agent_simulator.cli import app

    result = CliRunner().invoke(app, ["init", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.stderr
    return tmp_path


def test_cli_scenarios_json(runner: CliRunner, proj: Path) -> None:
    from livekit_agent_simulator.cli import app

    result = runner.invoke(app, ["scenarios", "--json", "--root", str(proj)])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert any(s.get("id") == "smoke-hello" for s in payload)


def test_cli_scenarios_table(runner: CliRunner, proj: Path) -> None:
    from livekit_agent_simulator.cli import app

    # COLUMNS mirrors a real terminal so rich does not wrap the table at 80.
    result = runner.invoke(
        app, ["scenarios", "--root", str(proj)], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stderr
    assert "smoke-hello" in result.stdout
    assert "locale" in result.stdout
    assert "Traceback" not in result.stderr


def test_cli_cues_json_roundtrip(runner: CliRunner, proj: Path) -> None:
    from livekit_agent_simulator.cli import app

    result = runner.invoke(app, ["cues", "--json", "--root", str(proj)])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "builtin" in payload
    assert "resolve_order" in payload


def test_cli_validate_invalid_exits_1_in_both_modes(runner: CliRunner, proj: Path) -> None:
    from livekit_agent_simulator.cli import app

    table = runner.invoke(app, ["validate", "does-not-exist", "--root", str(proj)])
    assert table.exit_code == 1
    assert "not valid" in table.stdout or "not found" in table.stdout

    as_json = runner.invoke(app, ["validate", "does-not-exist", "--json", "--root", str(proj)])
    assert as_json.exit_code == 1
    assert json.loads(as_json.stdout)["valid"] is False
