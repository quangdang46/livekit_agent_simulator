"""Wraps scripts/e2e_caller_smoke.py so the full mocked pipeline
(DSL -> Orchestrator -> Language Adapter -> Validator -> Interaction
Planner -> Behavior Evaluator) is exercised as part of the normal test
suite too, not only as a standalone script. No network, no LiveKit
credentials.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def test_e2e_caller_smoke_full_mocked_loop_passes() -> None:
    from e2e_caller_smoke import run_smoke

    assert run_smoke() is True
