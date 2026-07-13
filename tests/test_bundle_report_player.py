"""Tests for staging Vite dist into templates/report-player."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "bundle_report_player",
    ROOT / "scripts" / "bundle_report_player.py",
)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
bundle_report_player = _mod.bundle_report_player


def test_bundle_report_player_copies_dist(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dest = tmp_path / "report-player"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")

    out = bundle_report_player(dist_dir=dist, dest_dir=dest)
    assert out == dest
    assert (dest / "index.html").read_text(encoding="utf-8") == "<html></html>"
    assert (dest / "assets" / "app.js").is_file()


def test_bundle_report_player_missing_dist(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        bundle_report_player(dist_dir=tmp_path / "missing", dest_dir=tmp_path / "out")


def test_bundle_report_player_replaces_existing(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dest = tmp_path / "report-player"
    dist.mkdir()
    (dist / "index.html").write_text("v2", encoding="utf-8")
    dest.mkdir()
    (dest / "index.html").write_text("v1", encoding="utf-8")
    (dest / "stale.js").write_text("old", encoding="utf-8")

    bundle_report_player(dist_dir=dist, dest_dir=dest)
    assert (dest / "index.html").read_text(encoding="utf-8") == "v2"
    assert not (dest / "stale.js").exists()
