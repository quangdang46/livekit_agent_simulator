"""Slice 1 acceptance: `caller_steps:` parses into Scenario.caller_actions
via both entry points (YAML/dict and JSONL), while legacy persona+script
scenarios are completely unaffected (no behavior change, no duplicate
execution path).
"""

from __future__ import annotations

import json
from pathlib import Path

from livekit_agent_simulator.caller_contract.dsl import CallerAction
from livekit_agent_simulator.scenario import parse_scenario
from livekit_agent_simulator.scenario_from_dict import scenario_from_dict
from livekit_agent_simulator.scenario_yaml import load_scenario_yaml


def test_caller_steps_yaml_parses_into_caller_actions(tmp_path: Path) -> None:
    yaml_text = """
apiVersion: agent-sim/v1
kind: Scenario
metadata:
  id: caller-steps-smoke
  locale: en-US
persona:
  brief: unused legacy brief (caller_actions drives this scenario instead)
caller_steps:
  - say: "Hi, I'm calling about the 2022 Honda CR-V."
  - do:
      behavior: negotiate
      target: price
      constraints:
        max_turns: 3
        max_budget: 30000
  - say: "Thanks, bye."
  - end: true
"""
    p = tmp_path / "caller-steps-smoke.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    scenario = load_scenario_yaml(p)

    assert len(scenario.caller_actions) == 4
    kinds = [a.kind for a in scenario.caller_actions]
    assert kinds == ["say", "do", "say", "end"]
    assert all(isinstance(a, CallerAction) for a in scenario.caller_actions)

    do_action = scenario.caller_actions[1]
    assert do_action.contract is not None
    assert do_action.contract.behavior == "negotiate"
    assert do_action.contract.target == "price"
    assert do_action.contract.constraints.max_budget == 30000

    # export_dict surfaces a count without needing to unparse CallerAction.
    assert scenario.export_dict()["caller_actions"] == 4


def test_caller_steps_dict_entry_point_matches_yaml(tmp_path: Path) -> None:
    data = {
        "metadata": {"id": "caller-steps-dict"},
        "persona": {"brief": "unused"},
        "caller_steps": [
            {"say": "Hello."},
            {"do": "ask"},
        ],
    }
    scenario = scenario_from_dict(data, path_label="caller-steps-dict")
    assert len(scenario.caller_actions) == 2
    assert scenario.caller_actions[0].kind == "say"
    assert scenario.caller_actions[1].kind == "do"
    assert scenario.caller_actions[1].contract.behavior == "ask"


def test_caller_steps_jsonl_entry_point(tmp_path: Path) -> None:
    lines = [
        {
            "apiVersion": "agent-sim/v1",
            "kind": "Scenario",
            "metadata": {"id": "caller-steps-jsonl"},
        },
        {"kind": "Persona", "spec": {"brief": "unused legacy brief"}},
        {
            "kind": "CallerSteps",
            "spec": {"steps": [{"say": "Hi there."}, {"wait": 500}]},
        },
    ]
    p = tmp_path / "caller-steps-jsonl.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    scenario = parse_scenario(p)
    assert len(scenario.caller_actions) == 2
    assert scenario.caller_actions[0].kind == "say"
    assert scenario.caller_actions[1].kind == "wait"
    assert scenario.caller_actions[1].wait_ms == 500


def test_legacy_scenario_without_caller_steps_is_unaffected(tmp_path: Path) -> None:
    """A scenario with no caller_steps key must have an EMPTY caller_actions
    list — legacy persona+script scenarios never populate it implicitly, and
    parsing/behavior of the legacy fields must be byte-for-byte the same as
    before this slice (no duplicate execution path is even representable)."""
    yaml_text = """
apiVersion: agent-sim/v1
kind: Scenario
metadata:
  id: legacy-smoke
  locale: en-US
  tags: [smoke]
persona:
  name: Alex
  brief: You are calling a business for the first time.
  goals:
  - Hear the agent's greeting
execute:
  max_turns: 2
  timeout_s: 90
  first_speaker: user
pass_criteria:
  criteria:
  - The agent responded to the caller
"""
    p = tmp_path / "legacy-smoke.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    scenario = load_scenario_yaml(p)

    assert scenario.caller_actions == []
    # Legacy fields parse exactly as before (persona/execute/pass_criteria).
    assert scenario.persona["name"] == "Alex"
    assert scenario.run_spec.max_turns == 2
    assert scenario.run_spec.first_speaker == "user"
    assert scenario.pass_criteria == ["The agent responded to the caller"]
    assert scenario.export_dict()["caller_actions"] == 0
