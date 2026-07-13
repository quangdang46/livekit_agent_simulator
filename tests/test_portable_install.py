from __future__ import annotations

from pathlib import Path

from livekit_agent_simulator.portable_layout import (
    find_nested_payload,
    portable_python_exe,
    portable_python_valid,
    repair_nested_portable_layout,
)


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="ascii")


def test_portable_python_exe_windows_layout(tmp_path: Path) -> None:
    py = tmp_path / "python" / "python.exe"
    _touch(py)
    assert portable_python_exe(tmp_path) == py


def test_find_nested_payload(tmp_path: Path) -> None:
    nested = tmp_path / "lk-sim-windows-x64"
    _touch(nested / "python" / "python.exe")
    _touch(nested / "python" / "Lib" / "encodings" / "__init__.py")
    _touch(nested / "lk-sim.cmd")
    assert find_nested_payload(tmp_path) == nested


def test_repair_nested_portable_layout(tmp_path: Path) -> None:
    nested = tmp_path / "lk-sim-windows-x64"
    _touch(nested / "python" / "python.exe")
    _touch(nested / "python" / "Lib" / "encodings" / "__init__.py")
    _touch(nested / "lk-sim.cmd")
    # Broken partial python at top level (simulates bad install)
    _touch(tmp_path / "python" / "python.exe")

    assert portable_python_valid(tmp_path) is False
    assert repair_nested_portable_layout(tmp_path) is True
    assert portable_python_valid(tmp_path) is True
    assert (tmp_path / "lk-sim.cmd").is_file()
    assert not nested.exists()


def test_repair_noop_when_already_flat(tmp_path: Path) -> None:
    _touch(tmp_path / "python" / "python.exe")
    _touch(tmp_path / "python" / "Lib" / "encodings" / "__init__.py")
    assert repair_nested_portable_layout(tmp_path) is True
    assert find_nested_payload(tmp_path) is None
