import pytest

from livekit_agent_simulator.plugins import VerifyContext, register_verify, reset_for_tests, verify_plugin
from livekit_agent_simulator.plugins.loader import ensure_plugins_loaded
from livekit_agent_simulator.scenario import parse_scenario
from livekit_agent_simulator.scenario_from_dict import export_scenario_dict, scenario_from_dict
from livekit_agent_simulator.script_parse import parse_script_verify
from livekit_agent_simulator.script import ScriptStep, evaluate_script_log


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_for_tests()
    yield
    reset_for_tests()


def test_parse_verify_plugins():
    v = parse_script_verify(
        {
            "plugins": ["a", "b"],
            "plugin": "ignored_when_plugins_set",
            "plugin_options": {"a": {"min": 2}},
        }
    )
    assert v is not None
    assert v.plugins == ("a", "b")
    assert v.plugin_options["a"] == {"min": 2}


def test_parse_verify_single_plugin_shorthand():
    v = parse_script_verify({"plugin": "only_one"})
    assert v is not None
    assert v.plugins == ("only_one",)


def test_parse_plugins_kind(tmp_path):
    content = """\
{"apiVersion":"agent-sim/v1","kind":"Scenario","metadata":{"id":"p1","locale":"ja-JP"}}
{"kind":"Persona","spec":{"brief":"caller"}}
{"kind":"Plugins","spec":{"modules":["my_checks"]}}
"""
    f = tmp_path / "p1.jsonl"
    f.write_text(content, encoding="utf-8")
    s = parse_scenario(f)
    assert s.plugin_modules == ["my_checks"]


def test_evaluate_with_registered_plugin(tmp_path):
    @verify_plugin("agent_spoke_again")
    def agent_spoke_again(ctx: VerifyContext) -> dict:
        n = ctx.finals_after_first_cue("agent")
        return {"pass": n >= 1, "checks": [{"check": "n", "pass": n >= 1, "actual": n}]}

    steps = [ScriptStep("bc", "agent_speaking", 800, "うん", "backchannel")]
    events = [
        {"kind": "sim.script.cue", "ts_mono_ms": 5000, "spec": {"step_id": "bc", "during_agent_speech": True}},
        {"kind": "transcript.agent.final", "ts_mono_ms": 8000, "spec": {"text": "ok"}},
    ]
    scenario = scenario_from_dict(
        {"id": "t", "persona": {"brief": "x"}, "plugin_modules": []},
        path=tmp_path / "t.jsonl",
    )
    result = evaluate_script_log(
        events,
        steps,
        parse_script_verify({"plugins": ["agent_spoke_again"]}),
        scenario=scenario,
        project_root=tmp_path,
    )
    assert result["pass"] is True
    assert result["plugin_results"][0]["plugin"] == "agent_spoke_again"


def test_evaluate_missing_plugin_fails(tmp_path):
    steps = [ScriptStep("bc", "agent_speaking", 800, "うん", "backchannel")]
    events = [
        {"kind": "sim.script.cue", "ts_mono_ms": 5000, "spec": {"step_id": "bc", "during_agent_speech": True}},
    ]
    scenario = scenario_from_dict({"id": "t", "persona": {"brief": "x"}}, path=tmp_path / "t.jsonl")
    result = evaluate_script_log(
        events,
        steps,
        parse_script_verify({"plugins": ["not_registered"]}),
        scenario=scenario,
        project_root=tmp_path,
    )
    assert result["pass"] is False


def test_load_local_plugin_module(tmp_path):
    plugins_dir = tmp_path / ".agent-sim" / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "local_check.py").write_text(
        """
from livekit_agent_simulator.plugins import verify_plugin

@verify_plugin("local_ok")
def local_ok(ctx):
    return {"pass": True}
""",
        encoding="utf-8",
    )
    info = ensure_plugins_loaded(tmp_path, ["local_check"])
    assert "local:local_check" in info["loaded"]
    assert "local_ok" in info["verify_plugins"]


def test_scenario_from_dict_and_export():
    s = scenario_from_dict(
        {
            "id": "dyn",
            "persona": {"brief": "caller"},
            "script": {
                "steps": [{"id": "s1", "say": "hi", "delay_ms": 100}],
                "verify": {"plugins": ["p1"]},
            },
            "plugin_modules": ["mod"],
        }
    )
    out = export_scenario_dict(s)
    # export_scenario_dict now returns the canonical section-object shape
    # (id lives under metadata; lossless round-trip via scenario_from_dict).
    assert out["metadata"]["id"] == "dyn"
    assert out["script"]["verify"]["plugins"] == ["p1"]
    assert out["plugin_modules"] == ["mod"]
    # round-trip: the exported dict must re-import to an equal scenario
    s2 = scenario_from_dict(out)
    assert s2.id == "dyn"
    assert s2.plugin_modules == ["mod"]
