"""Edge-case tests for the YAML scenario transport (round-trip fidelity).

Covers the fields the section-object serializer must preserve through a
serialize → parse cycle: persona, context, execute/simulator, dispatch, caller,
telephony, behavior, script steps + verify, asserts, and pass_criteria
(flat list AND the {mode, criteria, judges} dict form).
"""

from __future__ import annotations

import glob
from pathlib import Path

import pytest

from livekit_agent_simulator.scenario import Scenario, ScenarioError, parse_scenario
from livekit_agent_simulator.scenario_from_dict import scenario_from_dict
from livekit_agent_simulator.scenario_yaml import load_scenario_yaml, scenario_to_yaml_text

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _roundtrip(scenario: Scenario) -> Scenario:
    text = scenario_to_yaml_text(scenario)
    p = TEMPLATES / "_roundtrip_tmp.yaml"
    p.write_text(text, encoding="utf-8")
    try:
        return parse_scenario(p)
    finally:
        p.unlink(missing_ok=True)


def _assert_equal(a: Scenario, b: Scenario) -> None:
    assert a.id == b.id
    assert a.tags == b.tags
    assert (a.persona or {}) == (b.persona or {})
    assert (a.context or {}) == (b.context or {})
    assert (a.execute is None) == (b.execute is None)
    if a.execute is not None:
        assert a.execute.max_turns == b.execute.max_turns
        assert a.execute.timeout_s == b.execute.timeout_s
        assert a.execute.first_speaker == b.execute.first_speaker
    assert (a.dispatch is None) == (b.dispatch is None)
    if a.dispatch is not None:
        assert a.dispatch.metadata == b.dispatch.metadata
    assert (a.caller is None) == (b.caller is None)
    if a.caller is not None:
        assert a.caller.mode == b.caller.mode
    assert (a.telephony is None) == (b.telephony is None)
    if a.telephony is not None:
        assert a.telephony.call_to == b.telephony.call_to
        assert a.telephony.dial_in == b.telephony.dial_in
        assert a.telephony.prepare_ms == b.telephony.prepare_ms
        assert a.telephony.wait_until_answered == b.telephony.wait_until_answered
        assert a.telephony.handset_isolation == b.telephony.handset_isolation
    assert (a.behavior_spec or {}) == (b.behavior_spec or {})
    assert len(a.script_steps) == len(b.script_steps)
    for sa, sb in zip(a.script_steps, b.script_steps):
        assert sa.id == sb.id
        assert sa.say == sb.say
        assert sa.barge_in == sb.barge_in
        assert sa.action == sb.action
    assert a.pass_criteria_mode == b.pass_criteria_mode
    assert len(a.pass_judges) == len(b.pass_judges)
    assert (a.pass_criteria or []) == (b.pass_criteria or [])


@pytest.mark.parametrize(
    "fname",
    sorted(
        str(p.relative_to(TEMPLATES))
        for p in (TEMPLATES / "examples").glob("*.yaml")
    ),
)
def test_template_yaml_roundtrip(fname: str) -> None:
    s = parse_scenario(TEMPLATES / fname)
    s2 = _roundtrip(s)
    _assert_equal(s, s2)


def test_pass_criteria_dict_with_judges_roundtrips() -> None:
    s = scenario_from_dict(
        {
            "id": "pc-dict",
            "persona": {"brief": "b"},
            "pass_criteria": {
                "mode": "majority",
                "criteria": ["flat one"],
                "judges": [
                    {"id": "task", "criteria": ["The agent responded"]},
                    {"id": "safety", "builtin": "safety_judge"},
                ],
            },
        }
    )
    assert len(s.pass_judges) == 2
    assert s.pass_criteria_mode == "majority"
    s2 = _roundtrip(s)
    _assert_equal(s, s2)
    assert s2.pass_judges[1]["builtin"] == "safety_judge"


def test_export_dict_reimport_preserves_judges() -> None:
    from livekit_agent_simulator.scenario_from_dict import export_scenario_dict

    s = scenario_from_dict(
        {
            "id": "exp",
            "persona": {"brief": "b"},
            "pass_criteria": {
                "mode": "all",
                "judges": [
                    {"id": "task", "criteria": ["The agent responded"]},
                    {"id": "safety", "builtin": "safety_judge"},
                ],
            },
        }
    )
    exported = export_scenario_dict(s)
    s2 = scenario_from_dict(exported)
    assert len(s2.pass_judges) == 2
    assert s2.pass_criteria_mode == "all"


def test_pass_criteria_flat_list_still_works() -> None:
    s = scenario_from_dict(
        {"id": "pc-flat", "persona": {"brief": "b"}, "pass_criteria": ["c1", "c2"]}
    )
    assert s.pass_criteria == ["c1", "c2"]
    assert s.pass_criteria_mode == "all"
    assert s.pass_judges == []
    s2 = _roundtrip(s)
    _assert_equal(s, s2)


def test_simulator_fallback_used_when_no_execute() -> None:
    s = scenario_from_dict(
        {"id": "sim-only", "persona": {"brief": "b"}, "simulator": {"max_turns": 9, "timeout_s": 60, "first_speaker": "user"}}
    )
    assert s.execute is None
    assert s.simulator.max_turns == 9
    s2 = _roundtrip(s)
    assert s2.execute is None
    assert s2.simulator.max_turns == 9


def test_bad_pass_criteria_mode_rejected() -> None:
    with pytest.raises(ScenarioError, match="mode must be all|majority|any"):
        scenario_from_dict(
            {"id": "pc-bad", "persona": {"brief": "b"}, "pass_criteria": {"mode": "invalid", "criteria": ["c"]}}
        )


def test_pass_criteria_judge_missing_criteria_and_builtin_rejected() -> None:
    with pytest.raises(ScenarioError, match="needs criteria"):
        scenario_from_dict(
            {
                "id": "pc-bad2",
                "persona": {"brief": "b"},
                "pass_criteria": {"judges": [{"id": "empty"}]},
            }
        )


def test_yaml_loader_rejects_group_with_multi_scenarios() -> None:
    tmp = TEMPLATES / "_group_multi.yaml"
    tmp.write_text(
        "name: G\nscenarios:\n  - persona: {brief: a}\n  - persona: {brief: b}\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(ScenarioError, match="one scenario per file"):
            load_scenario_yaml(tmp)
    finally:
        tmp.unlink(missing_ok=True)


def test_yaml_loader_single_scenario_group_wrapper() -> None:
    tmp = TEMPLATES / "_group_single.yaml"
    tmp.write_text(
        "name: G\nscenarios:\n  - persona: {brief: wrapper brief}\n    id: w\n",
        encoding="utf-8",
    )
    try:
        s = load_scenario_yaml(tmp)
        assert s.id == "w"
        assert s.persona["brief"] == "wrapper brief"
    finally:
        tmp.unlink(missing_ok=True)


def test_telephony_fields_survive_roundtrip() -> None:
    s = scenario_from_dict(
        {
            "id": "tel",
            "persona": {"brief": "b"},
            "telephony": {
                "call_to": "+15551234567",
                "dial_in": "+15559876543",
                "prepare_ms": 5000,
                "wait_until_answered": False,
                "handset_isolation": "mute_uplink",
            },
        }
    )
    assert s.telephony is not None
    s2 = _roundtrip(s)
    _assert_equal(s, s2)
    assert s2.telephony.call_to == "+15551234567"
    assert s2.telephony.prepare_ms == 5000
    assert s2.telephony.wait_until_answered is False
    assert s2.telephony.handset_isolation == "mute_uplink"


def test_hold_music_timeout_survives_roundtrip() -> None:
    s = scenario_from_dict(
        {
            "id": "hold",
            "persona": {"brief": "b"},
            "execute": {"max_turns": 8, "timeout_s": 120, "first_speaker": "agent", "hold_music_timeout_s": 20},
        }
    )
    assert s.hold_music_timeout_s() == 20
    s2 = _roundtrip(s)
    assert s2.hold_music_timeout_s() == 20


def test_hold_music_timeout_invalid_rejected() -> None:
    with pytest.raises(ScenarioError, match="hold_music_timeout_s must be between"):
        scenario_from_dict(
            {
                "id": "hold-bad",
                "persona": {"brief": "b"},
                "execute": {"max_turns": 8, "hold_music_timeout_s": 1},
            }
        )


def test_script_verify_fields_survive_roundtrip() -> None:
    s = scenario_from_dict(
        {
            "id": "sv",
            "persona": {"brief": "b"},
            "script": {
                "steps": [{"id": "s1", "trigger": "silence", "delay_ms": 900, "say": "hi"}],
                "verify": {
                    "require_during_agent_speech": True,
                    "min_agent_finals_after_barge_in": 2,
                    "min_interruptions": 1,
                    "max_interruptions": 3,
                },
            },
        }
    )
    assert s.script_verify is not None
    assert s.script_verify.min_agent_finals_after_barge_in == 2
    s2 = _roundtrip(s)
    assert s2.script_verify is not None
    assert s2.script_verify.min_agent_finals_after_barge_in == 2
    assert s2.script_verify.min_interruptions == 1
    assert s2.script_verify.max_interruptions == 3
