from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import livekit_agent_simulator.update_check as uc


# ── GitHub release fetch / track filtering ──────────────────────────────────

def _release(tag, *, draft=False, prerelease=False, assets=None):
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "assets": [
            {"name": name, "browser_download_url": url} for name, url in (assets or {}).items()
        ],
    }


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> None:
        return None


def _mock_urlopen(monkeypatch, releases_json: list) -> None:
    import urllib.request

    payload = json.dumps(releases_json).encode("utf-8")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload))


def test_fetch_latest_release_skips_rust_track(monkeypatch) -> None:
    """A newer -rust tagged release must not shadow the Python-track release."""
    releases = [
        _release("v0.2.0-rust", assets={"lksr-linux-x86_64.tar.gz": "https://x/lksr.tar.gz"}),
        _release("v0.1.11", assets={"lks-linux-x64.zip": "https://x/lks.zip"}),
        _release("v0.1.10", assets={"lks-linux-x64.zip": "https://x/lks-old.zip"}),
    ]
    _mock_urlopen(monkeypatch, releases)

    result = uc._fetch_latest_release()
    assert result is not None
    tag, assets = result
    assert tag == "v0.1.11"
    assert assets == {"lks-linux-x64.zip": "https://x/lks.zip"}


def test_fetch_latest_release_skips_draft_and_prerelease(monkeypatch) -> None:
    releases = [
        _release("v0.1.12", draft=True, assets={"lks-linux-x64.zip": "https://x/draft.zip"}),
        _release("v0.1.11", prerelease=True, assets={"lks-linux-x64.zip": "https://x/pre.zip"}),
        _release("v0.1.10", assets={"lks-linux-x64.zip": "https://x/stable.zip"}),
    ]
    _mock_urlopen(monkeypatch, releases)

    tag, assets = uc._fetch_latest_release()
    assert tag == "v0.1.10"
    assert assets["lks-linux-x64.zip"] == "https://x/stable.zip"


def test_fetch_latest_release_returns_none_on_network_error(monkeypatch) -> None:
    import urllib.error
    import urllib.request

    def _boom(*a, **k):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert uc._fetch_latest_release() is None


# ── platform detection ───────────────────────────────────────────────────────

def test_current_platform_mapping(monkeypatch) -> None:
    cases = [
        ("Windows", "AMD64", "windows-x64"),
        ("Linux", "x86_64", "linux-x64"),
        ("Darwin", "arm64", "macos-arm64"),
        ("Linux", "aarch64", None),  # no linux-arm64 pack shipped yet
        ("Windows", "ARM64", None),  # no windows-arm64 pack shipped yet
    ]
    for system, machine, expected in cases:
        monkeypatch.setattr(uc.platform, "system", lambda system=system: system)
        monkeypatch.setattr(uc.platform, "machine", lambda machine=machine: machine)
        assert uc._current_platform() == expected


# ── portable-install detection ───────────────────────────────────────────────

def test_is_portable_install_true_when_executable_under_current_dir(tmp_path, monkeypatch) -> None:
    current_dir = tmp_path / "lks" / "current"
    python_exe = current_dir / "python" / "python.exe"
    python_exe.parent.mkdir(parents=True)
    python_exe.write_text("", encoding="utf-8")

    monkeypatch.setattr(uc, "_current_dir", lambda: current_dir)
    monkeypatch.setattr(uc.sys, "executable", str(python_exe))
    assert uc._is_portable_install() is True


def test_is_portable_install_false_for_venv_python(tmp_path, monkeypatch) -> None:
    current_dir = tmp_path / "lks" / "current"
    venv_python = tmp_path / "some-venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")

    monkeypatch.setattr(uc, "_current_dir", lambda: current_dir)
    monkeypatch.setattr(uc.sys, "executable", str(venv_python))
    assert uc._is_portable_install() is False


# ── zip payload discovery / nested-layout repair ─────────────────────────────

def _write_pack_zip(zip_path: Path, *, top: str, nested: str | None = None) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        base = top if nested is None else f"{top}/{nested}"
        zf.writestr(f"{base}/lks", "binary")
        zf.writestr(f"{base}/lks-mcp", "binary")
        zf.writestr(f"{base}/python/lib/python3.12/encodings/__init__.py", "")
    zip_path.write_bytes(buf.getvalue())


def _extract(zip_path: Path, out_dir: Path) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out_dir)


def test_find_payload_dir_direct_layout(tmp_path) -> None:
    zip_path = tmp_path / "pack.zip"
    _write_pack_zip(zip_path, top="lks-linux-x64")
    extract_dir = tmp_path / "out"
    _extract(zip_path, extract_dir)

    payload = uc._find_payload_dir(extract_dir)
    assert payload == extract_dir / "lks-linux-x64"


def test_find_payload_dir_repairs_nested_layout(tmp_path) -> None:
    """Guards against the 'zip inside a zip' layout install.sh also repairs."""
    zip_path = tmp_path / "pack.zip"
    _write_pack_zip(zip_path, top="lks-linux-x64", nested="lks-linux-x64")
    extract_dir = tmp_path / "out"
    _extract(zip_path, extract_dir)

    payload = uc._find_payload_dir(extract_dir)
    assert payload == extract_dir / "lks-linux-x64" / "lks-linux-x64"


def test_find_payload_dir_returns_none_without_python(tmp_path) -> None:
    extract_dir = tmp_path / "out" / "lks-linux-x64"
    extract_dir.mkdir(parents=True)
    (extract_dir / "lks").write_text("binary", encoding="utf-8")

    assert uc._find_payload_dir(extract_dir.parent) is None


# ── run_update: guard rails ──────────────────────────────────────────────────

def test_run_update_refuses_unsupported_platform(monkeypatch) -> None:
    monkeypatch.setattr(uc, "_current_platform", lambda: None)
    assert uc.run_update() is False


def test_run_update_refuses_non_portable_install(monkeypatch) -> None:
    monkeypatch.setattr(uc, "_current_platform", lambda: "linux-x64")
    monkeypatch.setattr(uc, "_is_portable_install", lambda: False)
    assert uc.run_update() is False


def test_run_update_already_up_to_date(monkeypatch, capsys) -> None:
    monkeypatch.setattr(uc, "_current_platform", lambda: "linux-x64")
    monkeypatch.setattr(uc, "_is_portable_install", lambda: True)
    monkeypatch.setattr(uc, "_fetch_latest_release", lambda: ("v" + uc._current_version, {}))
    assert uc.run_update() is True
    assert "already up to date" in capsys.readouterr().out


def test_run_update_missing_asset(monkeypatch) -> None:
    monkeypatch.setattr(uc, "_current_platform", lambda: "linux-x64")
    monkeypatch.setattr(uc, "_is_portable_install", lambda: True)
    monkeypatch.setattr(uc, "_fetch_latest_release", lambda: ("v99.0.0", {}))
    assert uc.run_update() is False


# ── run_update: full happy path (POSIX in-place swap) ────────────────────────

def test_run_update_posix_swap(tmp_path, monkeypatch) -> None:
    install_root = tmp_path / "lks"
    current_dir = install_root / "current"
    current_dir.mkdir(parents=True)
    (current_dir / "marker.txt").write_text("old", encoding="utf-8")

    zip_path = tmp_path / "release.zip"
    _write_pack_zip(zip_path, top="lks-linux-x64")
    zip_bytes = zip_path.read_bytes()

    monkeypatch.setattr(uc, "_current_platform", lambda: "linux-x64")
    monkeypatch.setattr(uc, "_is_portable_install", lambda: True)
    monkeypatch.setattr(uc, "_install_root", lambda: install_root)
    monkeypatch.setattr(uc, "_current_dir", lambda: current_dir)
    monkeypatch.setattr(
        uc,
        "_fetch_latest_release",
        lambda: ("v99.0.0", {"lks-linux-x64.zip": "https://x/lks-linux-x64.zip"}),
    )
    monkeypatch.setattr(uc, "_download", lambda url: zip_bytes)
    monkeypatch.setattr(uc.sys, "platform", "linux")

    assert uc.run_update() is True

    # New pack replaced the old `current/` contents in place.
    assert (current_dir / "lks").exists()
    assert (current_dir / "python" / "lib" / "python3.12" / "encodings" / "__init__.py").exists()
    assert not (current_dir / "marker.txt").exists()
    # Backup dir cleaned up, staging dir consumed.
    assert not (install_root / "current.old").exists()
    assert not (install_root / "current.new").exists()


def test_run_update_posix_rejects_invalid_pack(tmp_path, monkeypatch) -> None:
    """A zip with no embedded python must not clobber the existing install."""
    install_root = tmp_path / "lks"
    current_dir = install_root / "current"
    current_dir.mkdir(parents=True)
    (current_dir / "marker.txt").write_text("old", encoding="utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("lks-linux-x64/lks", "binary")  # no python/ payload
    zip_bytes = buf.getvalue()

    monkeypatch.setattr(uc, "_current_platform", lambda: "linux-x64")
    monkeypatch.setattr(uc, "_is_portable_install", lambda: True)
    monkeypatch.setattr(uc, "_install_root", lambda: install_root)
    monkeypatch.setattr(uc, "_current_dir", lambda: current_dir)
    monkeypatch.setattr(
        uc,
        "_fetch_latest_release",
        lambda: ("v99.0.0", {"lks-linux-x64.zip": "https://x/lks-linux-x64.zip"}),
    )
    monkeypatch.setattr(uc, "_download", lambda url: zip_bytes)
    monkeypatch.setattr(uc.sys, "platform", "linux")

    assert uc.run_update() is False
    assert (current_dir / "marker.txt").exists()  # untouched


# ── run_update: Windows path schedules a detached helper, doesn't block ─────

def test_run_update_windows_schedules_helper_without_blocking(tmp_path, monkeypatch) -> None:
    install_root = tmp_path / "lks"
    current_dir = install_root / "current"
    current_dir.mkdir(parents=True)

    zip_path = tmp_path / "release.zip"
    _write_pack_zip(zip_path, top="lks-windows-x64")
    zip_bytes = zip_path.read_bytes()

    popen_calls = []

    class _FakePopen:
        def __init__(self, *a, **k):
            popen_calls.append((a, k))

    monkeypatch.setattr(uc, "_current_platform", lambda: "windows-x64")
    monkeypatch.setattr(uc, "_is_portable_install", lambda: True)
    monkeypatch.setattr(uc, "_install_root", lambda: install_root)
    monkeypatch.setattr(uc, "_current_dir", lambda: current_dir)
    monkeypatch.setattr(
        uc,
        "_fetch_latest_release",
        lambda: ("v99.0.0", {"lks-windows-x64.zip": "https://x/lks-windows-x64.zip"}),
    )
    monkeypatch.setattr(uc, "_download", lambda url: zip_bytes)
    monkeypatch.setattr(uc.sys, "platform", "win32")
    monkeypatch.setattr(uc.subprocess, "Popen", _FakePopen)

    assert uc.run_update() is True
    # The swap itself is deferred to the spawned helper — `current/` is left
    # untouched by this process, only staged as `current.new`.
    assert (install_root / "current.new" / "lks").exists()
    assert len(popen_calls) == 1
    assert (install_root / "update-finish.ps1").exists()
