"""REST API (lks serve) — JSON/HTTP wrapper over the shared ops layer.

Covers routing, JSON error responses, and that GET runs / scenarios reuse the
ops layer (same behavior as CLI/MCP — design lock 8).
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

from livekit_agent_simulator.web.api import PREFIX, RestApiHandler, start_api_server

MIN_CONFIG = """livekit:
  url: wss://example.livekit.cloud
  api_key: test-key
  api_secret: test-secret
  agent_name: test-agent
simulator:
  api_key: test-sim-key
"""


def _seed_config(root: Path) -> None:
    cfg_dir = root / ".agent-sim"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(MIN_CONFIG, encoding="utf-8")


class _FakeResp:
    def __init__(self) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}
        self.body = BytesIO()

    def send_response(self, code: int) -> None:
        self.status = code

    def send_header(self, key: str, value: str) -> None:
        self.headers[key] = value

    def end_headers(self) -> None:
        pass


class _FakeHeaders:
    def __init__(self, length: int) -> None:
        self._length = length

    def get(self, key: str, default: str | None = None) -> str | None:  # noqa: A003
        if key == "Content-Length":
            return str(self._length)
        return default


def _invoke(*, method: str, path: str, root: Path, body: bytes = b"") -> tuple[int, dict]:
    """Build a RestApiHandler instance and drive one request through _route."""
    resp = _FakeResp()
    handler = RestApiHandler.__new__(RestApiHandler)
    handler.project_root = str(root)
    handler.rfile = BytesIO(body)
    handler.headers = _FakeHeaders(len(body))
    handler.wfile = resp.body
    handler.send_response = resp.send_response
    handler.send_header = resp.send_header
    handler.end_headers = resp.end_headers
    handler._route(method, path)
    resp.body.seek(0)
    payload = resp.body.read()
    try:
        parsed = json.loads(payload.decode("utf-8")) if payload else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = {}
    return resp.status, parsed


def test_routes_health(tmp_path: Path) -> None:
    code, body = _invoke(method="GET", path=f"{PREFIX}/health", root=tmp_path)
    assert code == 200
    assert body["ok"] is True
    assert body["root"] == str(tmp_path.resolve())


def test_get_runs_returns_list(tmp_path: Path) -> None:
    _seed_config(tmp_path)
    # Seed the SQLite run store (list_runs reads runs.sqlite, not report dirs).
    import asyncio

    from livekit_agent_simulator.logging.sqlite_store import RunStore
    from livekit_agent_simulator.config import load_config

    cfg = load_config(tmp_path)
    asyncio.run(
        RunStore(cfg.sqlite_path).create_run(
            run_id="r1",
            scenario_id="scen-a",
            room_name="lks-r1",
            agent_name="test-agent",
            started_utc="2026-01-01T00:00:00+00:00",
            report_dir=str(tmp_path / ".agent-sim" / "reports" / "r1"),
        )
    )

    code, body = _invoke(method="GET", path=f"{PREFIX}/runs", root=tmp_path)
    assert code == 200
    assert isinstance(body, list)
    assert any(r.get("run_id") == "r1" for r in body)


def test_get_scenarios_returns_list(tmp_path: Path) -> None:
    _seed_config(tmp_path)
    (tmp_path / ".agent-sim" / "scenarios").mkdir(parents=True)
    code, body = _invoke(method="GET", path=f"{PREFIX}/scenarios", root=tmp_path)
    assert code == 200
    assert isinstance(body, list)


def test_unknown_route_404(tmp_path: Path) -> None:
    code, body = _invoke(method="GET", path=f"{PREFIX}/nope", root=tmp_path)
    assert code == 404
    assert "error" in body


def test_execute_missing_scenario_id_400(tmp_path: Path) -> None:
    code, body = _invoke(method="POST", path=f"{PREFIX}/execute", root=tmp_path, body=b"{}")
    assert code == 400
    assert "scenario_id" in body["error"]


def test_validate_missing_scenario_id_400(tmp_path: Path) -> None:
    code, body = _invoke(method="POST", path=f"{PREFIX}/validate", root=tmp_path, body=b"{}")
    assert code == 400
    assert "scenario_id" in body["error"]


def test_invalid_json_body_400(tmp_path: Path) -> None:
    code, body = _invoke(method="POST", path=f"{PREFIX}/execute", root=tmp_path, body=b"not json")
    assert code == 400
    assert "invalid JSON" in body["error"]


def test_start_api_server_nonblocking(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch

    with (
        patch("livekit_agent_simulator.web.api.ThreadingHTTPServer") as httpd_cls,
        patch("livekit_agent_simulator.web.api.threading.Thread") as thread_cls,
    ):
        httpd_cls.return_value = MagicMock()
        thread_cls.return_value = MagicMock()
        info = start_api_server(tmp_path, blocking=False)

    assert info["url"].startswith("http://127.0.0.1:8787")
    assert info["root"] == str(tmp_path.resolve())
    thread_cls.return_value.start.assert_called_once()
    httpd_cls.return_value.shutdown.assert_not_called()
