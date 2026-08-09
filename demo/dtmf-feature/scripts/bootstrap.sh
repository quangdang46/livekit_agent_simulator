#!/usr/bin/env bash
# Bootstrap the DTMF demo repo:
#   1. cp .env.example -> .env   (fill it in yourself)
#   2. uv venv + install agent deps
#   3. generate .agent-sim/config.yaml from .env
# Idempotent — safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example — edit it now (LIVEKIT_URL/API keys/AGENT_NAME)."
fi

if [ ! -d .venv ]; then
    uv venv --python 3.12
fi
uv pip install --python .venv/bin/python -e ".[dev]"

python scripts/gen-config.py
echo
echo "Done. Next:"
echo "  source .venv/bin/activate"
echo "  dtmf-agent dev --config livekit.toml   # terminal 1 — run the agent worker"
echo "  lks preflight --root .                 # terminal 2 — check the simulator"
echo "  lks scenarios --root .                 # list scenarios"
echo "  lks run --root . dtmf-menu             # run the DTMF scenario"
