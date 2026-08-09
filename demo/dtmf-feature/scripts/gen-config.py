#!/usr/bin/env python3
"""Generate .agent-sim/config.yaml from .env (no shell scripting needed).

The lks simulator reads a config file, not env vars, so we expand the
templates in .agent-sim/config.yaml using values from .env. Idempotent:
re-run whenever .env changes.

Usage:
    python scripts/gen-config.py            # .env -> .agent-sim/config.yaml
    python scripts/gen-config.py --print    # write to stdout instead
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
TEMPLATE = ROOT / ".agent-sim" / "config.yaml"
OUT = ROOT / ".agent-sim" / "config.yaml"

# SIM_CALLER_API_KEY is an alias for the agent's provider key family:
# if unset, fall back to GOOGLE_API_KEY (provider: google) or OPENAI_API_KEY.
_FALLBACKS = {
    "SIM_CALLER_API_KEY": ["GOOGLE_API_KEY", "OPENAI_API_KEY"],
}

def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env

def resolve(key: str, env: dict[str, str], missing: list[str]) -> str:
    value = env.get(key)
    if value is not None and value:
        return value
    for alias in _FALLBACKS.get(key, []):
        if env.get(alias):
            return env[alias]
    missing.append(key)
    return ""

def main() -> int:
    env = load_env(ENV_FILE)
    missing: list[str] = []
    text = TEMPLATE.read_text(encoding="utf-8")

    def _sub(m: re.Match[str]) -> str:
        return resolve(m.group(1), env, missing)

    out = re.sub(r"\$\{([A-Z0-9_]+)\}", _sub, text)

    if missing:
        print(f"error: missing in {ENV_FILE}: {', '.join(missing)}", file=sys.stderr)
        print("copy .env.example to .env and fill in the values", file=sys.stderr)
        return 1

    if "--print" in sys.argv:
        sys.stdout.write(out)
        return 0
    OUT.write_text(out, encoding="utf-8")
    print(f"wrote {OUT} (from {ENV_FILE})")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
