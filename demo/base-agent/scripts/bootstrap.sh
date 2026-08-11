#!/usr/bin/env bash
# Bootstrap the base-agent demo:
#   1. cp .env.example -> .env  (fill in LIVEKIT + provider keys)
#   2. uv venv + install agent deps
#   3. generate .agent-sim/config.yaml from .env (via lks init + gen-config)
# Idempotent — safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example — edit it now (LIVEKIT_URL/API keys/AGENT_NAME/AGENT_STACK)."
fi

if [ ! -d .venv ]; then
    uv venv --python 3.12
fi
uv pip install --python .venv/bin/python -e "."

echo
echo "Done. Next:"
echo "  source .venv/bin/activate"
echo "  base-agent dev           # terminal 1 — run the agent worker"
echo "  lks preflight --root .   # terminal 2 — check the simulator"
echo "  lks scenarios --root .   # list scenarios"
echo "  lks execute frontdesk-hours --root .   # run a scenario"
