from __future__ import annotations

import json
import signal
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from livekit_agent_simulator.web.server import (
    _install_shutdown_handlers,
    list_run_ids,
    start_web_server,
)


def test_install_shutdown_handlers_does_not_break_sigint_on_windows(monkeypatch) -> None:
    httpd = MagicMock(spec=ThreadingHTTPServer)
    previous = signal.getsignal(signal.SIGINT)
    try:
        if sys.platform == "win32":
            monkeypatch.setattr(
                "livekit_agent_simulator.web.server._install_shutdown_handlers",
                lambda h: [],
            )
            assert signal.getsignal(signal.SIGINT) is previous
            return
        _install_shutdown_handlers(httpd)
        assert signal.getsignal(signal.SIGINT) is not previous
    finally:
        signal.signal(signal.SIGINT, previous)


def _make_run(reports: Path, run_id: str, *, scenario_id: str, status: str = "done", started_utc: str | None = None) -> None:
    rd = reports / run_id
    rd.mkdir(parents=True)
    (rd / "events.jsonl").write_text("{}\n", encoding="utf-8")
    meta = {"run_id": run_id, "scenario_id": scenario_id}
    if started_utc:
        meta["started_utc"] = started_utc
    (rd / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (rd / "summary.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": status,
                "duration_ms": 1000,
                "turn_count": 2,
            }
        ),
        encoding="utf-8",
    )


def test_list_run_ids_newest_first(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    _make_run(reports, "a-run", scenario_id="a")
    _make_run(reports, "b-run", scenario_id="b")
    ids = list_run_ids(reports)
    assert set(ids) == {"a-run", "b-run"}
    assert ids == sorted(ids, reverse=True)


def test_start_web_server_defaults_to_home_list(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    _make_run(reports, "scen-a-20260101-000000-aaaa", scenario_id="scen-a")
    player = tmp_path / "player"
    player.mkdir()
    (player / "index.html").write_text("<html></html>", encoding="utf-8")

    with (
        patch("livekit_agent_simulator.web.server._player_dir", return_value=player),
        patch("livekit_agent_simulator.web.server.ThreadingHTTPServer") as httpd_cls,
        patch("livekit_agent_simulator.web.server.webbrowser.open") as open_browser,
    ):
        httpd = MagicMock()
        httpd_cls.return_value = httpd
        info = start_web_server(
            reports,
            open_browser=True,
            run_id=None,
            blocking=False,
        )

    assert info["url"] == "http://127.0.0.1:8765/"
    assert info["run_id"] is None
    open_browser.assert_called_once_with("http://127.0.0.1:8765/")
    assert "runs" in info
    assert "scen-a-20260101-000000-aaaa" in info["runs"]
    httpd.shutdown()


def test_start_web_server_deep_links_explicit_run(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    run_id = "scen-a-20260101-000000-aaaa"
    _make_run(reports, run_id, scenario_id="scen-a")
    player = tmp_path / "player"
    player.mkdir()
    (player / "index.html").write_text("<html></html>", encoding="utf-8")

    with (
        patch("livekit_agent_simulator.web.server._player_dir", return_value=player),
        patch("livekit_agent_simulator.web.server.ThreadingHTTPServer") as httpd_cls,
        patch("livekit_agent_simulator.web.server.webbrowser.open") as open_browser,
        patch("livekit_agent_simulator.web.server.write_cues_json") as write_cues,
    ):
        httpd_cls.return_value = MagicMock()
        info = start_web_server(
            reports,
            open_browser=True,
            run_id=run_id,
            blocking=False,
        )

    assert info["url"] == f"http://127.0.0.1:8765/?run={run_id}"
    assert info["run_id"] == run_id
    open_browser.assert_called_once_with(f"http://127.0.0.1:8765/?run={run_id}")
    write_cues.assert_called_once()


def test_run_sort_key_prefers_started_utc() -> None:
    from livekit_agent_simulator.web.server import _run_sort_key

    older = {
        "run_id": "a",
        "started_utc": "2026-07-14T10:00:00+00:00",
        "mtime_ms": 9_999_999_999,
    }
    newer = {
        "run_id": "b",
        "started_utc": "2026-07-14T11:00:00+00:00",
        "mtime_ms": 1,
    }
    assert _run_sort_key(newer) > _run_sort_key(older)
