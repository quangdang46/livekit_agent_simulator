"""Spike: YAML scenario files parse through the existing dict validator.

Not wired into ops/CLI yet — proves the transport layer only. Mirrors the
JSONL-equivalent files in templates/examples/.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from livekit_agent_simulator.scenario import ScenarioError
from livekit_agent_simulator.scenario_yaml import load_scenario_yaml

TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "examples"


@pytest.mark.parametrize(
    "fname, scenario_id, n_steps, n_outcomes",
    [
        ("constraint-no-card.yaml", "constraint-no-card", 0, 2),
        ("interrupt-correction.yaml", "interrupt-correction", 3, 1),
        ("multi-judge-smoke.yaml", "multi-judge-smoke", 0, 1),
    ],
)
def test_yaml_parse_matches_jsonl_shape(
    fname: str, scenario_id: str, n_steps: int, n_outcomes: int
) -> None:
    s = load_scenario_yaml(TEMPLATES / fname)
    assert s.id == scenario_id
    assert s.persona["brief"].strip(), "brief must survive YAML multi-line"
    assert len(s.script_steps) == n_steps
    assert s.asserts is None or len(s.asserts.outcomes) == n_outcomes


def test_yaml_group_wrapper_errors_on_multi_scenario() -> None:
    tmp = TEMPLATES / "_group_multi.yaml"
    tmp.write_text(
        "name: Appointment scheduling\n"
        "scenarios:\n"
        "  - label: one\n"
        "    persona: {brief: x}\n"
        "  - label: two\n"
        "    persona: {brief: y}\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(ScenarioError, match="one scenario per file"):
            load_scenario_yaml(tmp)
    finally:
        tmp.unlink(missing_ok=True)
