#!/usr/bin/env python3
"""Build a relocatable lk-sim portable pack for the current OS/arch.

Layout (example windows-x64)::

    dist/portable/lk-sim-windows-x64/
      python/          # full CPython from uv (relocatable standalone)
      lk-sim.cmd       # Windows launcher
      lk-sim-mcp.cmd
      lk-sim           # Unix launcher
      lk-sim-mcp
      README.txt

CI zips the directory; installers only download + extract + PATH.
No uv/pip on the user machine.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def _run(cmd: list[str], **kw) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.check_call(cmd, **kw)


def _asset_name() -> str:
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    if sysname == "darwin":
        os_part = "macos"
    elif sysname == "windows":
        os_part = "windows"
    else:
        os_part = "linux"
    if machine in ("x86_64", "amd64"):
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = machine
    return f"lk-sim-{os_part}-{arch}"


def _uv() -> str:
    return shutil.which("uv") or "uv"


def _find_python_home(uv: str) -> Path:
    """Return the install prefix of the uv-managed CPython we will ship."""
    _run([uv, "python", "install", "3.12"])
    out = subprocess.check_output([uv, "python", "find", "3.12"], text=True).strip()
    py = Path(out).resolve()
    if not py.exists():
        raise SystemExit(f"uv python find failed: {out!r}")
    # .../install/bin/python3  or  .../install/python.exe
    if py.name.lower().startswith("python"):
        home = py.parent
        if home.name in ("bin", "Scripts"):
            home = home.parent
        return home
    raise SystemExit(f"unexpected python path: {py}")


def _write_launchers(root: Path, is_windows: bool) -> None:
    if is_windows:
        # Use Scripts\lk-sim.exe when present; else -m module.
        (root / "lk-sim.cmd").write_text(
            "\r\n".join(
                [
                    "@echo off",
                    "setlocal",
                    'set "ROOT=%~dp0"',
                    'set "ROOT=%ROOT:~0,-1%"',
                    'if exist "%ROOT%\\python\\Scripts\\lk-sim.exe" (',
                    '  "%ROOT%\\python\\Scripts\\lk-sim.exe" %*',
                    "  exit /b %ERRORLEVEL%",
                    ")",
                    '"%ROOT%\\python\\python.exe" -m livekit_agent_simulator %*',
                    "exit /b %ERRORLEVEL%",
                    "",
                ]
            ),
            encoding="ascii",
            newline="\r\n",
        )
        (root / "lk-sim-mcp.cmd").write_text(
            "\r\n".join(
                [
                    "@echo off",
                    "setlocal",
                    'set "ROOT=%~dp0"',
                    'set "ROOT=%ROOT:~0,-1%"',
                    'if exist "%ROOT%\\python\\Scripts\\lk-sim-mcp.exe" (',
                    '  "%ROOT%\\python\\Scripts\\lk-sim-mcp.exe" %*',
                    "  exit /b %ERRORLEVEL%",
                    ")",
                    '"%ROOT%\\python\\python.exe" -m livekit_agent_simulator.mcp_server %*',
                    "exit /b %ERRORLEVEL%",
                    "",
                ]
            ),
            encoding="ascii",
            newline="\r\n",
        )
    # Always write Unix-style launchers too (Git Bash / WSL friendly).
    # Critical:
    # - Resolve symlinks so ~/.local/bin/lk-sim -> pack/current/lk-sim works
    # - Prefer `python -m` over entrypoint scripts (shebangs embed CI absolute paths)
    for name, mod in (
        ("lk-sim", "livekit_agent_simulator"),
        ("lk-sim-mcp", "livekit_agent_simulator.mcp_server"),
    ):
        body = f"""#!/usr/bin/env bash
set -euo pipefail
# Resolve PATH shims / nested symlinks to the pack directory.
SOURCE="$0"
while [ -L "$SOURCE" ]; do
  DIR="$(cd "$(dirname "$SOURCE")" && pwd)"
  LINK="$(readlink "$SOURCE")"
  case "$LINK" in
    /*) SOURCE="$LINK" ;;
    *) SOURCE="$DIR/$LINK" ;;
  esac
done
ROOT="$(cd "$(dirname "$SOURCE")" && pwd)"
# Prefer python -m: entrypoint scripts ship with absolute shebangs from CI.
if [ -x "$ROOT/python/bin/python3" ]; then
  exec "$ROOT/python/bin/python3" -m {mod} "$@"
fi
if [ -x "$ROOT/python/bin/python" ]; then
  exec "$ROOT/python/bin/python" -m {mod} "$@"
fi
if [ -x "$ROOT/python/python.exe" ]; then
  exec "$ROOT/python/python.exe" -m {mod} "$@"
fi
# Fallbacks (may fail if shebang is absolute CI path - kept for odd layouts)
if [ -x "$ROOT/python/bin/{name}" ]; then
  exec "$ROOT/python/bin/{name}" "$@"
fi
if [ -x "$ROOT/python/Scripts/{name}.exe" ]; then
  exec "$ROOT/python/Scripts/{name}.exe" "$@"
fi
echo "lk-sim portable: python not found under $ROOT/python" >&2
exit 1
"""
        path = root / name
        # Launchers must stay pure ASCII (Windows cmd / minimal shells).
        body.encode("ascii")
        path.write_text(body, encoding="ascii")
        path.chmod(path.stat().st_mode | 0o755)


def _rewrite_absolute_paths(python_home: Path) -> None:
    """Rewrite #! shebangs in python/bin scripts to the pack-local python.

    Absolute CI paths (e.g. /Users/runner/work/...) break after relocate.
    We rewrite to the absolute path *inside this pack copy*; launchers still
    prefer ``python -m`` so relocating the pack remains safe.
    """
    is_win = os.name == "nt"
    for folder, py_name in (("bin", "python3"), ("Scripts", "python.exe")):
        d = python_home / folder
        if not d.is_dir():
            continue
        py_bin = d / py_name
        if not py_bin.exists() and folder == "bin":
            py_bin = d / "python"
        if not py_bin.exists() and folder == "Scripts":
            # top-level windows layout
            cand = python_home / "python.exe"
            if cand.exists():
                py_bin = cand
        shebang = f"#!{py_bin.resolve().as_posix()}\n"
        for f in d.iterdir():
            if not f.is_file():
                continue
            if f.suffix.lower() in (".exe", ".pyd", ".dll", ".so", ".dylib"):
                continue
            try:
                data = f.read_bytes()
            except OSError:
                continue
            if not data.startswith(b"#!"):
                continue
            if b"\0" in data[:200]:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            lines = text.splitlines(keepends=True)
            if not lines:
                continue
            if lines[0] == shebang:
                continue
            lines[0] = shebang
            try:
                f.write_text("".join(lines), encoding="utf-8")
            except OSError as e:
                print(f"warn: could not rewrite shebang {f}: {e}", flush=True)


def build(wheel: Path, out_dir: Path) -> Path:
    uv = _uv()
    asset = _asset_name()
    root = out_dir / asset
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    py_home_src = _find_python_home(uv)
    py_dest = root / "python"
    print(f"copying python home {py_home_src} -> {py_dest}", flush=True)
    shutil.copytree(py_home_src, py_dest, symlinks=True)

    is_windows = platform.system().lower() == "windows"
    if is_windows:
        py_bin = py_dest / "python.exe"
        if not py_bin.exists():
            # Some layouts put it under install root only
            candidates = list(py_dest.rglob("python.exe"))
            if not candidates:
                raise SystemExit("python.exe not found in copied home")
            # Prefer shortest path (top-level)
            py_bin = sorted(candidates, key=lambda p: len(p.parts))[0]
    else:
        py_bin = py_dest / "bin" / "python3"
        if not py_bin.exists():
            candidates = list(py_dest.rglob("python3"))
            py_bin = sorted(candidates, key=lambda p: len(p.parts))[0]

    # Allow installing into the shipped standalone interpreter (portable pack).
    for marker in py_dest.rglob("EXTERNALLY-MANAGED"):
        print(f"removing {marker}", flush=True)
        marker.unlink()

    # Install wheel + deps into the shipped interpreter via uv.
    _run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(py_bin),
            "--break-system-packages",
            str(wheel.resolve()),
        ]
    )

    # Rewrite absolute CI shebangs in entrypoint scripts (belt + suspenders;
    # Unix launchers prefer python -m and do not rely on these scripts).
    _rewrite_absolute_paths(py_dest)

    _write_launchers(root, is_windows=is_windows)
    (root / "README.txt").write_text(
        "\n".join(
            [
                "lk-sim portable pack (CI-built).",
                "Run ./lk-sim (Unix) or lk-sim.cmd (Windows).",
                "No uv/pip install required on the user machine.",
                "",
            ]
        ),
        encoding="ascii",
    )

    # Smoke
    if is_windows:
        smoke = root / "lk-sim.cmd"
        _run(["cmd", "/c", str(smoke), "--help"])
    else:
        _run([str(root / "lk-sim"), "--help"])

    zip_path = out_dir / f"{asset}.zip"
    if zip_path.exists():
        zip_path.unlink()
    print(f"zipping {root} -> {zip_path}", flush=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in root.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(out_dir).as_posix())
    print(f"OK {zip_path} ({zip_path.stat().st_size} bytes)", flush=True)
    return zip_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wheel", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, default=Path("dist/portable"))
    args = ap.parse_args()
    if not args.wheel.is_file():
        raise SystemExit(f"wheel not found: {args.wheel}")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    build(args.wheel, args.out_dir)


if __name__ == "__main__":
    main()
