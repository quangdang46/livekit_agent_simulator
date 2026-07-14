"""Resolve package data paths (templates + web UI in checkout and installed wheel)."""

from __future__ import annotations

from pathlib import Path


def package_templates_dir() -> Path:
    """Directory with config.yaml scaffold, smoke scenario, cues, example plugins.

    Search order:
    1. ``livekit_agent_simulator/templates`` next to this package (wheel force-include)
    2. Repo-root ``templates/`` (editable / monorepo checkout)
    """
    pkg_dir = Path(__file__).resolve().parent
    # Prefer package-local templates (wheel force-include), then walk up for
    # editable checkouts: <repo>/src/livekit_agent_simulator → <repo>/templates.
    cur = pkg_dir
    for _ in range(6):
        cand = cur / "templates"
        if cand.is_dir() and (cand / "config.yaml").exists():
            return cand
        cur = cur.parent
    raise FileNotFoundError(
        "livekit-agent-simulator templates not found. "
        "Expected package data or a repo-root templates/ directory with config.yaml."
    )


def package_cues_dir() -> Path:
    return package_templates_dir() / "cues"


def _find_repo_web_dist(pkg_dir: Path) -> Path | None:
    cur = pkg_dir
    for _ in range(6):
        cand = cur / "web" / "dist"
        if (cand / "index.html").is_file():
            return cand
        cur = cur.parent
    return None


def package_web_dir() -> Path:
    """Built Vite assets for ``lk-sim web``.

    Prefer the newest of:
    1. Repo-root ``web/dist`` (editable checkout after ``pnpm --dir web build``)
    2. ``livekit_agent_simulator/web_static`` (wheel / AppData install)

    When both exist, ``web/dist`` wins if its ``index.html`` is newer — so a local
    rebuild is visible without reinstalling the wheel.
    """
    pkg_dir = Path(__file__).resolve().parent
    packaged = pkg_dir / "web_static"
    packaged_ok = (packaged / "index.html").is_file()
    dist = _find_repo_web_dist(pkg_dir)

    if dist is not None and (dist / "index.html").is_file():
        if not packaged_ok:
            return dist
        try:
            if (dist / "index.html").stat().st_mtime >= (
                packaged / "index.html"
            ).stat().st_mtime:
                return dist
        except OSError:
            return dist
        return packaged

    return packaged
