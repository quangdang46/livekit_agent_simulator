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

from livekit_agent_simulator.scenario import (
    Scenario,
    ScenarioError,
    find_scenario,
    list_scenarios,
    parse_scenario,
)
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


def test_list_scenarios_shadows_jsonl_by_yaml(tmp_path) -> None:
    """After `lks convert`, foo.jsonl + foo.yaml share an id — YAML is canonical."""
    yaml_text = (
        "apiVersion: agent-sim/v1\n"
        "kind: Scenario\n"
        "metadata: {id: foo}\n"
        "persona: {brief: b}\n"
    )
    (tmp_path / "foo.yaml").write_text(yaml_text, encoding="utf-8")
    jsonl_text = (
        '{"apiVersion":"agent-sim/v1","kind":"Scenario","metadata":{"id":"foo"}}\n'
        '{"kind":"Persona","spec":{"brief":"b"}}\n'
    )
    (tmp_path / "foo.jsonl").write_text(jsonl_text, encoding="utf-8")

    entries = list_scenarios(tmp_path)
    foo_entries = [e for e in entries if e.get("id") == "foo"]
    assert len(foo_entries) == 1
    assert foo_entries[0]["file"] == "foo.yaml"


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


def test_convert_scenario_rejects_traversal_id(tmp_path: Path) -> None:
    from livekit_agent_simulator.config import ConfigError
    from livekit_agent_simulator.ops import convert_scenario

    with pytest.raises(ConfigError, match="Invalid scenario_id"):
        convert_scenario(tmp_path, "../evil")


def test_find_scenario_rejects_traversal_id(tmp_path: Path) -> None:
    with pytest.raises(ScenarioError, match="Invalid scenario_id"):
        find_scenario(tmp_path, "../../victim")


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


def _convert_fixture(tmp_path: Path) -> Path:
    """Copy a template .jsonl into a fresh .agent-sim/scenarios dir; return the dir."""
    import shutil

    scen = tmp_path / ".agent-sim" / "scenarios"
    scen.mkdir(parents=True)
    src = Path(__file__).resolve().parents[1] / "templates" / "examples" / "constraint-no-card.jsonl"
    shutil.copyfile(src, scen / "constraint-no-card.jsonl")
    return scen


def test_convert_scenario_failed_yaml_leaves_no_dest(tmp_path: Path, monkeypatch) -> None:
    """A YAML that fails to re-parse must never shadow the valid .jsonl."""
    from livekit_agent_simulator.ops import convert_scenario

    scen = _convert_fixture(tmp_path)

    def broken(scenario) -> str:
        return "apiVersion: agent-sim/v1\nkind: Scenario\n  bad: [unclosed"

    monkeypatch.setattr(
        "livekit_agent_simulator.scenario_yaml.scenario_to_yaml_text", broken
    )
    with pytest.raises(ScenarioError):
        convert_scenario(tmp_path, "constraint-no-card")

    assert not (scen / "constraint-no-card.yaml").exists()
    assert not list(scen.glob("*.yaml.tmp"))
    # The source .jsonl is untouched.
    assert (scen / "constraint-no-card.jsonl").exists()


def test_scenario_from_run_failed_validation_leaves_no_dest(tmp_path: Path, monkeypatch) -> None:
    """write=True must clean up when the synthesized draft fails to validate."""
    import json

    from livekit_agent_simulator.ops import scenario_from_run

    dot = tmp_path / ".agent-sim"
    (dot / "scenarios").mkdir(parents=True)
    (dot / "config.yaml").write_text(
        "livekit:\n"
        "  url: wss://example.livekit.cloud\n"
        "  api_key: k\n"
        "  api_secret: s\n"
        "  agent_name: a\n"
        "simulator:\n"
        "  api_key: g\n",
        encoding="utf-8",
    )
    report_dir = dot / "reports" / "run-abc-1"
    report_dir.mkdir(parents=True)
    (report_dir / "meta.json").write_text(
        json.dumps({"run_id": "run-abc-1", "scenario_id": "smoke-hello"}),
        encoding="utf-8",
    )
    (report_dir / "summary.json").write_text(
        json.dumps({"run_id": "run-abc-1", "status": "done", "turn_count": 3}),
        encoding="utf-8",
    )

    def boom(*_args, **_kwargs) -> None:
        raise ScenarioError("boom")

    monkeypatch.setattr("livekit_agent_simulator.scenario_yaml.load_scenario_yaml", boom)
    with pytest.raises(ScenarioError, match="boom"):
        scenario_from_run(tmp_path, "run-abc-1", write=True)

    assert not list((dot / "scenarios").glob("*.yaml"))
    assert not list((dot / "scenarios").glob("*.yaml.tmp"))


def test_group_wrapper_scalar_item_raises_scenario_error(tmp_path: Path) -> None:
    """L3: a scalar `scenarios:` item must raise ScenarioError, not a raw ValueError."""
    bad = tmp_path / "group-scalar.yaml"
    bad.write_text("scenarios: [hello]\n", encoding="utf-8")
    with pytest.raises(ScenarioError, match="scenarios"):
        load_scenario_yaml(bad)


def test_scenario_from_run_redacts_before_truncate() -> None:
    """L2: a long user final with an email must be redacted even when truncated."""
    import json

    from livekit_agent_simulator.scenario_from_run import build_scenario_draft_from_run

    tmp = TEMPLATES.parent / "_redact_tmp"
    tmp.mkdir(exist_ok=True)
    try:
        report = tmp / "reports" / "r-r"
        report.mkdir(parents=True, exist_ok=True)
        (report / "meta.json").write_text(
            json.dumps({"run_id": "r-r", "scenario_id": "s", "run_spec": {"first_speaker": "user"}}),
            encoding="utf-8",
        )
        (report / "summary.json").write_text(
            json.dumps({"run_id": "r-r", "status": "done", "turn_count": 3}), encoding="utf-8"
        )
        # 200-char user final with an email near the truncation boundary.
        email = "john.doe@example.com"
        user_text = f"I need help with order {email} " + "x" * 200
        (report / "events.jsonl").write_text(
            json.dumps({"kind": "transcript.user.final", "ts_mono_ms": 1000, "spec": {"text": user_text}})
            + "\n",
            encoding="utf-8",
        )
        draft = build_scenario_draft_from_run(report)
        assert email not in draft["yaml"]
        assert "[email]" in draft["yaml"]
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def test_leading_zero_strings_quoted_in_output() -> None:
    """L4: digit strings with leading zeros must be quoted so YAML 1.1 parsers
    don't coerce them into numbers on re-parse."""
    from livekit_agent_simulator.scenario_yaml import dump_scenario_dict

    text = dump_scenario_dict(
        {"a": "0123456789", "b": "09", "c": "007", "d": "hello", "e": "4111"}
    )
    # leading-zero + pure-digit strings are single-quoted
    assert "'0123456789'" in text
    assert "'09'" in text
    assert "'007'" in text
    assert "'4111'" in text
    # normal strings stay unquoted
    assert "d: hello" in text


def test_export_dict_preserves_behavior_and_assert() -> None:
    """export_scenario_dict must preserve behavior_spec and the full assert
    (must_not_match / contains_any / sip), not a summary shape."""
    from livekit_agent_simulator.scenario_from_dict import export_scenario_dict

    # people-pleaser-refuse-card has a Behavior barge + assert with must_not_match.
    s = parse_scenario(TEMPLATES / "examples" / "people-pleaser-refuse-card.yaml")
    exported = export_scenario_dict(s)
    assert "behavior" in exported
    assert "assert" in exported
    s2 = scenario_from_dict(exported)
    assert s2.behavior_spec, "behavior_spec must survive export"
    assert s2.asserts and s2.asserts.transcript
    assert s2.asserts.transcript[0].must_not_match, "must_not_match must survive"


def test_export_dict_preserves_caller_telephony_sip() -> None:
    from livekit_agent_simulator.scenario_from_dict import export_scenario_dict

    s = parse_scenario(TEMPLATES / "inbound-caller-sim.yaml")
    s2 = scenario_from_dict(export_scenario_dict(s))
    assert s2.caller and s2.caller.mode == "inbound_sip"
    assert s2.telephony and s2.telephony.dial_in == "+15551234567"
    assert s2.asserts and s2.asserts.sip
