"""Background version check + ``lks update`` for the Python CLI.

Follows the pattern used by strix / marimo / pyselfupdate, and mirrors the
Rust ``lksr`` implementation (``crates/lks/src/update.rs``) so both CLIs have
the same release-based behavior:

- Background daemon-thread check against **GitHub Releases** (not PyPI), at
  most once per 24 h.
- State cached in ``~/.lks/update-check.json``.
- Non-intrusive notice printed to stderr after command output.
- ``lks update`` downloads the matching portable-pack asset for this platform
  from GitHub Releases and replaces the install in place. No ``pip`` /
  ``pipx`` / ``uv tool`` invocation anywhere in this module.

This repo publishes two independent release tracks into the *same* GitHub
Releases list: Python (`lks`, tags like ``v0.1.10``) and Rust (`lksr`, tags
like ``v0.1.0-rust``). ``GET /releases/latest`` returns whichever track
tagged most recently and would hand us a release with no ``lks-*.zip``
asset. ``install.sh``/``install.ps1`` already solve this by listing releases
and skipping ``-rust`` tags — do the same here.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

from . import __version__ as _current_version

GITHUB_REPO = "quangdang46/livekit-agent-simulator"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=100"
CHECK_INTERVAL_SECONDS = 24 * 60 * 60  # 24 h
REQUEST_TIMEOUT_SECONDS = 5
DOWNLOAD_TIMEOUT_SECONDS = 60

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


# ── GitHub Releases ──────────────────────────────────────────────────────────

def _fetch_latest_release() -> tuple[str, dict[str, str]] | None:
    """Return (tag_name, {asset_name: browser_download_url}) for the newest
    *Python-track* release, or ``None`` on any failure.

    Skips draft/prerelease entries and any tag ending in ``-rust`` (the lksr
    binary track) — see module docstring.
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        RELEASES_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "lks-update-check"},
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            releases = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None

    if not isinstance(releases, list):
        return None

    for release in releases:
        if not isinstance(release, dict):
            continue
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str) or tag.endswith("-rust"):
            continue
        assets = {
            a["name"]: a["browser_download_url"]
            for a in release.get("assets", [])
            if isinstance(a, dict) and a.get("name") and a.get("browser_download_url")
        }
        return tag, assets

    return None


def _download(url: str) -> bytes:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "lks-update"})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp:
        return resp.read()


# ── platform detection ──────────────────────────────────────────────────────
#
# Must match the `lks-<platform>.zip` asset names built by
# .github/workflows/python-release.yml (windows-x64 / linux-x64 / macos-arm64
# only, for now — no macos-x64 or *-arm64 linux/windows portable pack yet).

def _current_platform() -> str | None:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Windows" and machine in ("amd64", "x86_64"):
        return "windows-x64"
    if system == "Linux" and machine in ("x86_64", "amd64"):
        return "linux-x64"
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        return "macos-arm64"
    return None


# ── portable-install layout ──────────────────────────────────────────────────
#
# Mirrors install.sh / install.ps1: portable packs live under
# INSTALL_ROOT/current, with a `python/` embedded interpreter inside it and
# PATH shims (symlinks on Unix, a launcher on Windows) pointing back into it.

def _install_root() -> Path:
    env = os.environ.get("INSTALL_ROOT")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) / "lks" if base else Path.home() / "AppData" / "Local" / "lks"
    return Path.home() / ".local" / "share" / "lks"


def _current_dir() -> Path:
    return _install_root() / "current"


def _has_embedded_python(pack_dir: Path) -> bool:
    return (
        (pack_dir / "python" / "Lib" / "encodings" / "__init__.py").is_file()
        or any((pack_dir / "python" / "lib").glob("python3.*/encodings/__init__.py"))
    )


def _is_portable_install() -> bool:
    """True if this process's own interpreter is running out of
    INSTALL_ROOT/current — i.e. it was installed via install.sh/install.ps1,
    not pip/pipx/uv tool into some other venv/site-packages."""
    try:
        exe = Path(sys.executable).resolve()
        cur = _current_dir().resolve()
        return exe == cur or cur in exe.parents
    except OSError:
        return False


def _portable_install_hint() -> str:
    if sys.platform == "win32":
        return (
            'irm "https://github.com/{repo}/releases/latest/download/install.ps1" '
            '-OutFile "$env:TEMP\\lks-install.ps1"; '
            'powershell -NoProfile -ExecutionPolicy Bypass -File "$env:TEMP\\lks-install.ps1"'
        ).format(repo=GITHUB_REPO)
    return (
        'curl -fsSL "https://github.com/{repo}/releases/latest/download/install.sh" | bash'
    ).format(repo=GITHUB_REPO)


def _find_payload_dir(extract_dir: Path) -> Path | None:
    """Locate the `lks-<platform>/` payload inside the extracted zip, and
    repair the "zip inside a zip" nested-layout edge case the installers
    also guard against."""
    candidates = [p for p in extract_dir.iterdir() if p.is_dir() and p.name.startswith("lks-")]
    payload = candidates[0] if candidates else extract_dir
    if _has_embedded_python(payload):
        return payload

    nested = [p for p in payload.iterdir() if p.is_dir() and p.name.startswith("lks-")] if payload.is_dir() else []
    for n in nested:
        if _has_embedded_python(n):
            return n
    return None


# ── background check ────────────────────────────────────────────────────────

def _refresh_cache() -> None:
    result = _fetch_latest_release()
    if result:
        tag, _assets = result
        _write_cache(latest_version=tag.lstrip("v"), checked_at=int(time.time()))
    else:
        # Record attempt even on failure to avoid hammering the API.
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
    sys.stderr.write(
        f"A new version of lks is available: {_current_version} -> {latest}\n"
        f"  Run `lks update` to upgrade.\n"
    )


def skip_version(version: str) -> None:
    """Remember not to prompt again for this version (newer releases still notify)."""
    _write_cache(skipped_version=version)


# ── interactive update (lks update) ─────────────────────────────────────────

def _err(msg: str) -> None:
    sys.stderr.write(f"{msg}\n")


def run_update() -> bool:
    """Download the latest portable pack from GitHub Releases and replace the
    current install in place. Returns True on success (including
    already-up-to-date)."""
    platform_id = _current_platform()
    if platform_id is None:
        _err(
            f"Unsupported platform for self-update ({platform.system()}/{platform.machine()}). "
            f"See https://github.com/{GITHUB_REPO}/releases for available packs."
        )
        return False

    if not _is_portable_install():
        _err(
            "lks update only supports installs made via install.sh/install.ps1 "
            "(the portable pack) — this install looks like it came from "
            "pip/pipx/uv instead, which this command no longer manages.\n"
            "Switch to the portable installer to get self-updates:\n"
            f"  {_portable_install_hint()}"
        )
        return False

    result = _fetch_latest_release()
    if result is None:
        _err("Failed to fetch the latest release from GitHub.")
        return False
    tag, assets = result
    latest = tag.lstrip("v")
    current = _current_version

    if not _is_newer(latest, current):
        print(f"lks is already up to date ({current}).")
        return True

    asset_name = f"lks-{platform_id}.zip"
    asset_url = assets.get(asset_name)
    if not asset_url:
        _err(f"Release {tag} has no asset named {asset_name}.")
        return False

    sys.stderr.write(f"Downloading: {asset_url}\n")
    try:
        zip_bytes = _download(asset_url)
    except Exception as e:
        _err(f"Download failed: {e}")
        return False
    sys.stderr.write(f"Downloaded {len(zip_bytes)} bytes — extracting...\n")

    current_dir = _current_dir()
    staging_dir = current_dir.parent / "current.new"
    shutil.rmtree(staging_dir, ignore_errors=True)

    with tempfile.TemporaryDirectory(prefix="lks-update-") as tmp:
        tmp_path = Path(tmp)
        zip_path = tmp_path / "pack.zip"
        zip_path.write_bytes(zip_bytes)
        extract_dir = tmp_path / "out"
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)
        except zipfile.BadZipFile:
            _err("Downloaded file is not a valid zip archive.")
            return False

        payload = _find_payload_dir(extract_dir)
        if payload is None:
            _err("Portable pack invalid: embedded python not found in downloaded zip.")
            return False

        shutil.copytree(payload, staging_dir)

    if not _has_embedded_python(staging_dir):
        shutil.rmtree(staging_dir, ignore_errors=True)
        _err("Downloaded pack failed validation (embedded python missing).")
        return False

    for exe_name in ("lks", "lks-mcp"):
        exe_path = staging_dir / exe_name
        if exe_path.exists():
            exe_path.chmod(exe_path.stat().st_mode | 0o111)

    if sys.platform == "win32":
        _schedule_windows_swap(current_dir, staging_dir)
        sys.stderr.write(
            f"lks will finish updating ({current} -> {latest}) once this process "
            "exits — open a new shell afterwards.\n"
        )
    else:
        _swap_posix(current_dir, staging_dir)
        sys.stderr.write(f"lks updated: {current} -> {latest} — restart to use the new version.\n")
    return True


def _swap_posix(current_dir: Path, staging_dir: Path) -> None:
    backup_dir = current_dir.parent / "current.old"
    shutil.rmtree(backup_dir, ignore_errors=True)
    os.rename(current_dir, backup_dir)
    try:
        os.rename(staging_dir, current_dir)
    except OSError:
        os.rename(backup_dir, current_dir)  # rollback
        raise
    shutil.rmtree(backup_dir, ignore_errors=True)


def _schedule_windows_swap(current_dir: Path, staging_dir: Path) -> None:
    """The embedded python.exe currently executing this code lives inside
    `current_dir`, so Windows holds it open and neither the directory nor
    its contents can be replaced synchronously. Spawn a detached helper that
    waits for this process to exit, then performs the directory swap."""
    backup_dir = current_dir.parent / "current.old"
    log_path = current_dir.parent / "update.log"
    script = f"""
$targetPid = {os.getpid()}
for ($i = 0; $i -lt 150; $i++) {{
    if (-not (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)) {{ break }}
    Start-Sleep -Milliseconds 200
}}
Start-Sleep -Milliseconds 300
try {{
    if (Test-Path "{backup_dir}") {{ Remove-Item -Recurse -Force "{backup_dir}" -ErrorAction SilentlyContinue }}
    Rename-Item -Path "{current_dir}" -NewName "{backup_dir.name}" -ErrorAction Stop
    Rename-Item -Path "{staging_dir}" -NewName "{current_dir.name}" -ErrorAction Stop
    Remove-Item -Recurse -Force "{backup_dir}" -ErrorAction SilentlyContinue
    "update ok $(Get-Date -Format o)" | Out-File -FilePath "{log_path}" -Append -Encoding utf8
}} catch {{
    "update failed: $_" | Out-File -FilePath "{log_path}" -Append -Encoding utf8
}}
"""
    script_path = current_dir.parent / "update-finish.ps1"
    script_path.write_text(script, encoding="utf-8")
    subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(script_path),
        ],
        # DETACHED_PROCESS / CREATE_NEW_PROCESS_GROUP only exist on the
        # `subprocess` module on Windows — this function is only ever called
        # from the `sys.platform == "win32"` branch, but the module-level
        # attributes are looked up unconditionally, so guard them for
        # importability/testability on other platforms too.
        creationflags=(
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        ),
        close_fds=True,
    )
