"""Background version check + ``lks update`` for the Python CLI.

Follows the pattern used by strix / marimo / pyselfupdate:
- Background daemon-thread check against PyPI, at most once per 24 h.
- State cached in ``~/.lks/update-check.json``.
- Non-intrusive notice printed to stderr after command output.
- ``lks update`` runs the right package-manager upgrade command.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__ as _current_version

PYPI_PACKAGE = "livekit-agent-simulator"
CHECK_INTERVAL_SECONDS = 24 * 60 * 60  # 24 h
REQUEST_TIMEOUT_SECONDS = 5

_STATE_DIR = Path.home() / ".lks"
_STATE_PATH = _STATE_DIR / "update-check.json"

_background_thread: threading.Thread | None = None

# ── env / CI gates ──────────────────────────────────────────────────────────

_SKIP_ENV_KEYS = (
    "CI",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "JENKINS_URL",
    "BUILDKITE",
    "CIRCLECI",
)


def _is_disabled() -> bool:
    if os.environ.get("NO_UPDATE_CHECK"):
        return True
    return any(os.environ.get(k) for k in _SKIP_ENV_KEYS)


# ── state file ──────────────────────────────────────────────────────────────

def _read_cache() -> dict[str, Any]:
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _write_cache(**fields: Any) -> None:
    try:
        cache = _read_cache()
        cache.update(fields)
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        pass


# ── version helpers ─────────────────────────────────────────────────────────

def _parse_version(v: str) -> tuple[int, ...]:
    parts = v.strip().lstrip("v").split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return (0,)


def _is_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


# ── network ─────────────────────────────────────────────────────────────────

def _fetch_latest_version() -> str | None:
    """Query PyPI JSON API for the latest release version."""
    import urllib.request
    import urllib.error

    url = f"https://pypi.org/pypi/{PYPI_PACKAGE}/json"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read())
        return str(data["info"]["version"])
    except Exception:
        return None


# ── install method detection ────────────────────────────────────────────────

def _get_install_method() -> str:
    """Detect how lks was installed: pipx / uv / pip / unknown."""
    prefix = str(Path(sys.prefix)).replace("\\", "/")
    if "/pipx/" in prefix or prefix.endswith("/pipx"):
        return "pipx"
    if "/uv/tools/" in prefix:
        return "uv"
    if "/uv/tool/" in prefix:
        return "uv"
    return "pip"


def _get_upgrade_command(method: str | None = None) -> str:
    method = method or _get_install_method()
    commands = {
        "pipx": "pipx upgrade livekit-agent-simulator",
        "uv": "uv tool upgrade livekit-agent-simulator",
        "pip": "pip install --upgrade livekit-agent-simulator",
    }
    return commands.get(method, f"pip install --upgrade {PYPI_PACKAGE}")


# ── background check ────────────────────────────────────────────────────────

def _refresh_cache() -> None:
    latest = _fetch_latest_version()
    if latest:
        _write_cache(latest_version=latest, checked_at=int(time.time()))
    else:
        # Record attempt even on failure to avoid hammering API
        _write_cache(checked_at=int(time.time()))


def start_background_check() -> None:
    """Spawn a daemon thread to refresh the cached latest-version (once per 24 h)."""
    global _background_thread
    if _is_disabled():
        return
    cache = _read_cache()
    checked_at = cache.get("checked_at")
    if isinstance(checked_at, (int, float)) and time.time() - checked_at < CHECK_INTERVAL_SECONDS:
        return
    _background_thread = threading.Thread(target=_refresh_cache, daemon=True)
    _background_thread.start()


# ── notification (called from CLI root callback) ────────────────────────────

def get_available_update(*, respect_skip: bool = True) -> str | None:
    """Return the newer version string, or None if up-to-date / unknown."""
    if _is_disabled():
        return None
    if _background_thread is not None:
        _background_thread.join(timeout=0.2)
    cache = _read_cache()
    latest = cache.get("latest_version")
    current = _current_version
    if not isinstance(latest, str) or not _is_newer(latest, current):
        return None
    if respect_skip and cache.get("skipped_version") == latest:
        return None
    return latest


def notify_update() -> None:
    """Print a one-line update notice to stderr if a newer version exists."""
    latest = get_available_update()
    if not latest:
        return
    method = _get_install_method()
    upgrade_cmd = _get_upgrade_command(method)
    sys.stderr.write(
        f"A new version of lks is available: {_current_version} → {latest}\n"
        f"  Run `{upgrade_cmd}` to upgrade.\n"
    )


def skip_version(version: str) -> None:
    """Remember not to prompt again for this version (newer releases still notify)."""
    _write_cache(skipped_version=version)


# ── interactive update (lks update) ─────────────────────────────────────────

def run_update() -> bool:
    """Run the package-manager upgrade command. Returns True on success."""
    method = _get_install_method()
    cmd = _get_upgrade_command(method)
    sys.stderr.write(f"Running: {cmd}\n")
    try:
        result = subprocess.run(cmd.split(), check=False)
    except OSError as e:
        sys.stderr.write(f"Update failed: {e}\n")
        return False
    if result.returncode != 0:
        sys.stderr.write(
            f"Update failed (exit code {result.returncode}). "
            f"Run manually: {cmd}\n"
        )
        return False
    sys.stderr.write("lks updated — restart to use the new version.\n")
    return True
