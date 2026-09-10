"""DSL parity vectors: Python side of the caller_steps parse parity suite.

Golden vectors under tests/fixtures/parity/dsl/ are language-neutral JSON —
this file replays them through the REAL Python dsl.parse_steps and asserts
the expected kinds (or expected DSLError). The Rust side
(crates/lks-core/src/caller_dsl.rs `parity_tests`) reads the SAME directory
and must accept/reject the SAME steps — see that module.

Any change to a vector file requires updating both sides in the same
commit (same rule as test_parity_vectors.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from livekit_agent_simulator.caller_contract.dsl import DSLError, parse_steps

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "parity" / "dsl"


def _vectors() -> list[Path]:
    return sorted(FIXTURES_DIR.glob("*.json"))


@pytest.mark.parametrize("path", _vectors(), ids=lambda p: p.stem)
def test_dsl_vector_parses_identically_in_python(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    steps = data["steps"]
    if data["expect"] == "ok":
        actions = parse_steps(steps, file="vector")
        assert [a.kind for a in actions] == data["expected_kinds"], data["id"]
        for idx_s, fields in (data.get("expected_fields") or {}).items():
            idx = int(idx_s)
            payload_action = actions[idx]
            for key, value in fields.items():
                if key == "trigger":
                    assert payload_action.trigger is not None, data["id"]
                    assert payload_action.trigger.kind == value["kind"], data["id"]
                    assert payload_action.trigger.delay_ms == value["delay_ms"], data["id"]
                else:  # pragma: no cover - only trigger field-checks exist today
                    raise AssertionError(f"unknown field check {key!r}")
    else:
        with pytest.raises(DSLError):
            parse_steps(steps, file="vector")
