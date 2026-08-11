"""REST API for driving lks runs programmatically (``lks serve``).

Exposes the same public ops as the CLI / MCP (one ``ops`` layer — design lock 8)
over HTTP/JSON so CI runners, workflows, and external tools can drive
execute / validate / status / report without shelling out to ``lks``.

Routes (all under ``/api/v1``):

    GET  /health                      → {ok, version, root}
    GET  /runs                        → list runs (newest first)
    GET  /runs/<id>                   → run status + report summary
    GET  /runs/<id>/report            → full get_run_report payload
    GET  /scenarios                   → list_scenarios
    GET  /scenarios/<id>              → export_scenario
    POST /validate                    → validate_scenario
    POST /execute                     → execute_scenario (repeat/pass_at_k/name/agent)
    POST /preflight                   → preflight

Sync handler wrapper: ops are async; we run them on a fresh event loop per
request (same as the CLI ``_run`` helper). Blocking execute runs are allowed
but should be used with care — prefer polling ``GET /runs/<id>`` after a long
run, or run execute with ``repeat`` for flake control.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .. import ops

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
PREFIX = "/api/v1"


def _run_op(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Invoke an ops function, awaiting it if it's a coroutine.

    The ops layer mixes sync (list_scenarios, export_scenario, init_*) and
    async (execute, list_runs, status, report) functions. Detect which and run
    accordingly — mirroring the CLI ``_run`` async path while allowing sync ops.
    """
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


class RestApiHandler(BaseHTTPRequestHandler):
    """HTTP/JSON wrapper over the shared ops layer.

    Attach a target project root via the ``project_root`` class attribute (the
    server factory sets it from the CLI ``--root``).
    """

    project_root: str = "."

    # ------------------------------------------------------------------ routing

    def _route(self, method: str, path: str) -> None:
        try:
            self._handle(method, path)
        except ValueError as exc:
            self._json_error(400, str(exc))
        except ops.ConfigError as exc:
            self._json_error(404, str(exc))
        except FileNotFoundError as exc:
            self._json_error(404, str(exc))
        except Exception as exc:  # noqa: BLE001 — surface all server errors as JSON
            self._json_error(500, f"{type(exc).__name__}: {exc}")

    def _handle(self, method: str, path: str) -> None:
        if not path.startswith(PREFIX):
            return self._json_error(404, "unknown path — REST API under /api/v1")
        rest = path[len(PREFIX) :].strip("/")
        # health shortcut
        if method == "GET" and rest == "health":
            return self._json({"ok": True, "root": str(Path(self.project_root).resolve())})

        # structured routes
        if method == "GET" and rest == "runs":
            return self._json(_run_op(ops.list_runs, self.project_root))
        if method == "GET" and rest.startswith("runs/"):
            run_id = rest[len("runs/") :].strip("/")
            if not run_id:
                return self._json_error(400, "missing run id")
            if rest.endswith("/report"):
                run_id = rest[len("runs/") : -len("/report")]
                return self._json(_run_op(ops.get_run_report, self.project_root, run_id))
            return self._json(_run_op(ops.get_run_status, self.project_root, run_id))
        if method == "GET" and rest == "scenarios":
            return self._json(_run_op(ops.list_scenarios, self.project_root))
        if method == "GET" and rest.startswith("scenarios/"):
            scenario_id = rest[len("scenarios/") :].strip("/")
            if not scenario_id:
                return self._json_error(400, "missing scenario id")
            return self._json(_run_op(ops.export_scenario, self.project_root, scenario_id))

        # POST routes — body provides params
        if method == "POST":
            body = _read_json(self)
            if rest == "validate":
                sid = body.get("scenario_id")
                if not sid:
                    return self._json_error(400, "validate needs scenario_id")
                return self._json(_run_op(ops.validate_scenario, self.project_root, sid))
            if rest == "execute":
                sid = body.get("scenario_id")
                if not sid:
                    return self._json_error(400, "execute needs scenario_id")
                result = _run_op(
                    ops.execute_scenario,
                    self.project_root,
                    sid,
                    repeat=int(body.get("repeat", 1)),
                    pass_at_k=body.get("pass_at_k"),
                    run_name=body.get("run_name"),
                    agent_name=body.get("agent_name"),
                )
                return self._json(result)
            if rest == "preflight":
                connectivity = bool(body.get("connectivity", True))
                return self._json(
                    _run_op(ops.preflight, self.project_root, connectivity=connectivity)
                )
            return self._json_error(404, f"unknown POST route: {rest}")

        return self._json_error(404, f"unknown route: {path}")

    # ------------------------------------------------------------------ helpers

    def _json(self, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_error(self, code: int, msg: str) -> None:
        body = json.dumps({"error": msg}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        self._route("GET", path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        self._route("POST", path)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # Quieter default; still surfaces 4xx/5xx like the report player.
        code = str(args[0]) if args else ""
        if code.startswith("4") or code.startswith("5"):
            super().log_message(fmt, *args)


def start_api_server(
    project_root: Path | str,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    blocking: bool = True,
) -> dict[str, Any]:
    """Start the lks REST API server. Returns {url, host, port, root}.

    Non-blocking mode spawns a daemon thread (used by tests / embedding). The
    handler reuses the shared ``ops`` layer, so REST == CLI == MCP behavior.
    """
    root = str(Path(project_root).resolve())
    RestApiHandler.project_root = root

    httpd = ThreadingHTTPServer((host, port), RestApiHandler)
    base = f"http://{host}:{port}"
    url = base + PREFIX
    info: dict[str, Any] = {
        "url": url,
        "base_url": base,
        "host": host,
        "port": port,
        "root": root,
    }

    if not blocking:
        thread = threading.Thread(target=httpd.serve_forever, name="lks-api", daemon=True)
        thread.start()
        info["server"] = httpd
        info["thread"] = thread
        return info

    print(f"REST API: {url} (root: {root})", flush=True)
    print("Ctrl+C to stop", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
    return info
