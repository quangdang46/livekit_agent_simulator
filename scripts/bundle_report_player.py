#!/usr/bin/env python3
"""Stage Vite output (web/dist) into templates/report-player for wheel packaging.

Run after ``pnpm --dir web build``. CI and release call this before ``uv build``.
Not committed to git — see ``templates/report-player/`` in .gitignore.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_DIST = ROOT / "web" / "dist"
BUNDLE_DEST = ROOT / "templates" / "report-player"


def bundle_report_player(*, dist_dir: Path = WEB_DIST, dest_dir: Path = BUNDLE_DEST) -> Path:
    index = dist_dir / "index.html"
    if not index.is_file():
        raise FileNotFoundError(
            f"Missing {index} — run: pnpm --dir web install && pnpm --dir web build"
        )
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    shutil.copytree(dist_dir, dest_dir)
    return dest_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist",
        type=Path,
        default=WEB_DIST,
        help="Vite build output (default: web/dist)",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=BUNDLE_DEST,
        help="Package data staging dir (default: templates/report-player)",
    )
    args = parser.parse_args(argv)
    try:
        out = bundle_report_player(dist_dir=args.dist.resolve(), dest_dir=args.dest.resolve())
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"report player staged: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
