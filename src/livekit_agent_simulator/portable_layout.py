"""Helpers for portable pack install layout (flatten nested lk-sim-* dirs)."""

from __future__ import annotations

import shutil
from pathlib import Path


def portable_python_exe(root: Path) -> Path | None:
    win = root / "python" / "python.exe"
    if win.is_file():
        return win
    unix = root / "python" / "bin" / "python3"
    if unix.is_file():
        return unix
    return None


def portable_python_valid(root: Path) -> bool:
    py = portable_python_exe(root)
    if py is None:
        return False
    enc = root / "python" / "Lib" / "encodings" / "__init__.py"
    if enc.is_file():
        return True
    enc_unix = root / "python" / "lib" / "python3.12" / "encodings" / "__init__.py"
    return enc_unix.is_file()


def find_nested_payload(root: Path) -> Path | None:
    if not root.is_dir():
        return None
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name.startswith("lk-sim-") and portable_python_valid(child):
            return child
    return None


def repair_nested_portable_layout(root: Path) -> bool:
    """Hoist current/lk-sim-*/ contents into current/ (legacy broken installs)."""
    root = root.resolve()
    if portable_python_valid(root):
        return True
    nested = find_nested_payload(root)
    if nested is None:
        return False
    broken = root / "python"
    if broken.exists() and not portable_python_valid(root):
        if broken.is_dir():
            shutil.rmtree(broken)
        else:
            broken.unlink()
    for item in nested.iterdir():
        dest = root / item.name
        if dest.exists():
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        shutil.move(str(item), str(dest))
    nested.rmdir()
    return portable_python_valid(root)
