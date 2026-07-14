"""Local HTTP server for report playback UI."""

from __future__ import annotations

import json
import mimetypes
import signal
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..paths import package_web_dir
from .cues import build_cues_payload, write_cues_json

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _player_dir() -> Path:
    """Built static assets for ``lk-sim web`` (wheel ``web_static`` or checkout ``web/dist``)."""
    return package_web_dir()


def list_run_ids(reports_dir: Path) -> list[str]:
    if not reports_dir.is_dir():
        return []
    runs = [
        p.name
        for p in reports_dir.iterdir()
        if p.is_dir() and (p / "events.jsonl").exists()
    ]
    return sorted(runs, reverse=True)


def _run_sort_key(run: dict[str, Any]) -> tuple[float, str]:
    """Newest first: started_utc epoch, else mtime_ms, else run_id."""
    started = run.get("started_utc")
    if isinstance(started, str) and started.strip():
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
            return (dt.timestamp(), str(run.get("run_id") or ""))
        except ValueError:
            pass
    mtime = run.get("mtime_ms")
    if isinstance(mtime, (int, float)) and mtime > 0:
        return (float(mtime) / 1000.0, str(run.get("run_id") or ""))
    return (0.0, str(run.get("run_id") or ""))


class ReportUIHandler(SimpleHTTPRequestHandler):
    """Serves player assets + per-run reports under /runs/<id>/."""

    # Set by factory
    reports_dir: Path
    player_dir: Path

    def log_message(self, fmt: str, *args: Any) -> None:
        # Quieter default; still useful when debugging
        if "404" in (fmt % args):
            super().log_message(fmt, *args)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            return self._serve_file(self.player_dir / "index.html", "text/html; charset=utf-8")
        # Vite multi-chunk assets (and legacy flat player.* files if present)
        if path == "/player.html":
            # SPA: player is index.html?run=
            qs = parse_qs(parsed.query)
            run = (qs.get("run") or [None])[0]
            loc = f"/?run={run}" if run else "/"
            return self._redirect(loc)
        if path.startswith("/assets/"):
            name = path[len("/assets/") :]
            # prevent path escape
            target = (self.player_dir / "assets" / name).resolve()
            assets_root = (self.player_dir / "assets").resolve()
            if not str(target).startswith(str(assets_root)) or not target.is_file():
                return self._error(404, "asset not found")
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            return self._serve_file(target, ctype)
        if path in ("/player.js", "/player.css"):
            return self._serve_file(self.player_dir / path.lstrip("/"), mimetypes.guess_type(path)[0] or "text/plain")

        if path == "/api/runs":
            runs = []
            for rid in list_run_ids(self.reports_dir):
                rd = self.reports_dir / rid
                summary = {}
                sp = rd / "summary.json"
                if sp.exists():
                    try:
                        summary = json.loads(sp.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        summary = {}
                scenario_id = summary.get("scenario_id")
                started_utc = summary.get("started_utc")
                mp = rd / "meta.json"
                if mp.exists():
                    try:
                        meta = json.loads(mp.read_text(encoding="utf-8"))
                        if not scenario_id:
                            scenario_id = meta.get("scenario_id")
                        if not started_utc:
                            started_utc = meta.get("started_utc")
                    except json.JSONDecodeError:
                        pass
                tool_count = summary.get("tool_calls")
                if tool_count is None and isinstance(summary.get("metrics"), dict):
                    tool_count = summary["metrics"].get("tool_calls")
                try:
                    mtime_ms = int(rd.stat().st_mtime * 1000)
                except OSError:
                    mtime_ms = 0
                runs.append(
                    {
                        "run_id": rid,
                        "scenario_id": scenario_id,
                        "status": summary.get("status"),
                        "duration_ms": summary.get("duration_ms"),
                        "turn_count": summary.get("turn_count"),
                        "tool_count": tool_count,
                        "has_audio": (rd / "conversation.wav").exists(),
                        "started_utc": started_utc,
                        "mtime_ms": mtime_ms,
                    }
                )
            runs.sort(key=_run_sort_key, reverse=True)
            return self._json(runs)

        if path.startswith("/api/runs/"):
            rest = path[len("/api/runs/") :].strip("/")
            parts = rest.split("/")
            run_id = parts[0] if parts else ""
            report_dir = self.reports_dir / run_id
            if not run_id or not report_dir.is_dir():
                return self._error(404, "run not found")
            if len(parts) == 1 or parts[1] == "cues":
                payload = build_cues_payload(report_dir)
                write_cues_json(report_dir)
                return self._json(payload)
            return self._error(404, "unknown api path")

        if path.startswith("/runs/"):
            # /runs/<run_id>/conversation.wav | cues.json | ...
            rel = path[len("/runs/") :]
            parts = rel.split("/", 1)
            run_id = parts[0]
            name = parts[1] if len(parts) > 1 else ""
            report_dir = (self.reports_dir / run_id).resolve()
            if not str(report_dir).startswith(str(self.reports_dir.resolve())):
                return self._error(403, "forbidden")
            if not report_dir.is_dir():
                return self._error(404, "run not found")
            if not name or name in ("", "player"):
                return self._redirect(f"/?run={run_id}")
            if name == "cues.json":
                write_cues_json(report_dir)
                return self._serve_file(report_dir / "cues.json", "application/json; charset=utf-8")
            # Safe file under report dir
            target = (report_dir / name).resolve()
            if not str(target).startswith(str(report_dir)):
                return self._error(403, "forbidden")
            if not target.is_file():
                return self._error(404, "file not found")
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            return self._serve_file(target, ctype)

        return self._error(404, "not found")

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _error(self, code: int, msg: str) -> None:
        body = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path, content_type: str, *, no_store: bool = False) -> None:
        if not path.is_file():
            return self._error(404, f"missing {path.name}")
        data = path.read_bytes()
        # Windows mimetypes often maps .js → text/plain; browsers may refuse ES modules.
        suffix = path.suffix.lower()
        if suffix == ".js":
            content_type = "text/javascript; charset=utf-8"
        elif suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif suffix == ".html":
            content_type = "text/html; charset=utf-8"
            no_store = True
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if no_store or suffix in (".html", ".js", ".css"):
            self.send_header("Cache-Control", "no-store")
        if content_type.startswith("audio/"):
            self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(data)


def _install_shutdown_handlers(httpd: ThreadingHTTPServer) -> list[Any]:
    """Install platform handlers; return refs that must stay alive for process lifetime."""
    keepalive: list[Any] = []

    def _request_shutdown(signum: int | None = None, frame: Any | None = None) -> None:
        del signum, frame
        httpd.shutdown()

    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        @HandlerRoutine
        def _console_handler(ctrl_type: int) -> bool:
            # CTRL_C_EVENT / CTRL_BREAK_EVENT — shutdown server; return False so a
            # second Ctrl+C can still force-exit if shutdown is slow.
            if ctrl_type in (0, 1):
                httpd.shutdown()
            return False

        ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler, True)
        keepalive.append(_console_handler)
    else:
        previous = signal.getsignal(signal.SIGINT)

        def _sigint(signum: int | None = None, frame: Any | None = None) -> None:
            del signum, frame
            _request_shutdown()
            signal.signal(signal.SIGINT, previous)
            raise KeyboardInterrupt

        signal.signal(signal.SIGINT, _sigint)
        keepalive.append(_sigint)

    return keepalive


def _serve_blocking(httpd: ThreadingHTTPServer) -> None:
    keepalive = _install_shutdown_handlers(httpd)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
        del keepalive


def start_web_server(
    reports_dir: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    run_id: str | None = None,
    blocking: bool = True,
) -> dict[str, Any]:
    """Start report UI server. Returns {url, host, port, run_id, runs}."""
    reports_dir = Path(reports_dir).resolve()
    player_dir = _player_dir()
    if not (player_dir / "index.html").exists():
        raise FileNotFoundError(
            f"Web UI assets missing: {player_dir}/index.html — "
            "maintainers: pnpm --dir web install && pnpm --dir web build "
            "(CI attaches web/dist into the wheel as web_static; "
            "or use pnpm --dir web dev with lk-sim web in another terminal)"
        )

    runs = list_run_ids(reports_dir)
    # Open home list by default; only deep-link when run_id is explicit.
    if run_id:
        rd = reports_dir / run_id
        if rd.is_dir():
            write_cues_json(rd)

    ReportUIHandler.reports_dir = reports_dir
    ReportUIHandler.player_dir = player_dir

    httpd = ThreadingHTTPServer((host, port), ReportUIHandler)
    base = f"http://{host}:{port}"
    path = f"/?run={run_id}" if run_id else "/"
    url = base + path

    info = {
        "url": url,
        "base_url": base,
        "host": host,
        "port": port,
        "run_id": run_id,
        "runs": runs,
        "reports_dir": str(reports_dir),
        "player_dir": str(player_dir),
    }

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    if blocking:
        print(f"Open: {url}", flush=True)
        print(f"UI assets: {player_dir}", flush=True)
        _serve_blocking(httpd)
    else:
        thread = threading.Thread(target=httpd.serve_forever, name="lk-sim-web", daemon=True)
        thread.start()
        info["server"] = httpd
        info["thread"] = thread

    return info
