from __future__ import annotations

import os
import time
from pathlib import Path

from livekit_agent_simulator.paths import package_web_dir


def test_package_web_dir_finds_repo_dist(tmp_path: Path, monkeypatch) -> None:
    import livekit_agent_simulator.paths as paths_mod

    repo = tmp_path / "repo"
    pkg = repo / "src" / "livekit_agent_simulator"
    pkg.mkdir(parents=True)
    dist = repo / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")

    monkeypatch.setattr(paths_mod, "__file__", str(pkg / "paths.py"))
    assert package_web_dir() == dist.resolve()


def test_package_web_dir_prefers_newer_packaged_web_static(tmp_path: Path, monkeypatch) -> None:
    """When both exist, newer index.html wins (packaged if strictly newer)."""
    import livekit_agent_simulator.paths as paths_mod

    pkg = tmp_path / "livekit_agent_simulator"
    static = pkg / "web_static"
    static.mkdir(parents=True)
    packaged_html = static / "index.html"
    packaged_html.write_text("<html>packaged</html>", encoding="utf-8")

    repo_dist = tmp_path / "web" / "dist"
    repo_dist.mkdir(parents=True)
    dist_html = repo_dist / "index.html"
    dist_html.write_text("<html>repo</html>", encoding="utf-8")

    # Make packaged clearly newer than dist
    now = time.time()
    os.utime(dist_html, (now - 100, now - 100))
    os.utime(packaged_html, (now, now))

    monkeypatch.setattr(paths_mod, "__file__", str(pkg / "paths.py"))
    assert package_web_dir() == static.resolve()


def test_package_web_dir_prefers_newer_repo_dist(tmp_path: Path, monkeypatch) -> None:
    import livekit_agent_simulator.paths as paths_mod

    pkg = tmp_path / "livekit_agent_simulator"
    static = pkg / "web_static"
    static.mkdir(parents=True)
    packaged_html = static / "index.html"
    packaged_html.write_text("<html>packaged</html>", encoding="utf-8")

    # dist as sibling walk-up from pkg → tmp_path/web/dist
    repo_dist = tmp_path / "web" / "dist"
    repo_dist.mkdir(parents=True)
    dist_html = repo_dist / "index.html"
    dist_html.write_text("<html>repo</html>", encoding="utf-8")

    now = time.time()
    os.utime(packaged_html, (now - 100, now - 100))
    os.utime(dist_html, (now, now))

    monkeypatch.setattr(paths_mod, "__file__", str(pkg / "paths.py"))
    assert package_web_dir() == repo_dist.resolve()
