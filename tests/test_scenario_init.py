from pathlib import Path

import pytest

from livekit_agent_simulator.config import ConfigError
from livekit_agent_simulator.ops import init_scenario
from livekit_agent_simulator.scenario import parse_scenario, strip_extension_keys


def test_strip_extension_keys() -> None:
    assert strip_extension_keys({"kind": "Persona", "_doc": "note", "spec": {}}) == {
        "kind": "Persona",
        "spec": {},
    }


def test_init_scenario_writes_valid_yaml(tmp_path: Path) -> None:
    result = init_scenario(tmp_path, "order-cancel")
    path = Path(result["path"])
    assert path.exists()
    assert path.suffix == ".yaml"
    assert result["scenario_id"] == "order-cancel"

    text = path.read_text(encoding="utf-8")
    assert "apiVersion: agent-sim/v1" in text  # header survives comments
    assert "order-cancel" in text
    assert "persona:" in text
    assert "#" in text  # YAML comments are native

    scenario = parse_scenario(path)
    assert scenario.id == "order-cancel"
    assert scenario.persona.get("brief")
    assert scenario.execute is not None


def test_parse_ignores_full_line_slash_comments(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    path.write_text(
        "\n".join(
            [
                "// header note",
                '{"apiVersion":"agent-sim/v1","kind":"Scenario","metadata":{"id":"c","locale":"en-US"}}',
                "// persona note",
                '{"kind":"Persona","spec":{"brief":"Caller brief."}}',
                '{"kind":"Execute","spec":{"max_turns":2,"first_speaker":"user"}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    s = parse_scenario(path)
    assert s.id == "c"
    assert s.persona["brief"] == "Caller brief."


def test_init_scenario_refuses_overwrite(tmp_path: Path) -> None:
    init_scenario(tmp_path, "demo")
    with pytest.raises(ConfigError, match="already exists"):
        init_scenario(tmp_path, "demo")
    init_scenario(tmp_path, "demo", force=True)


def test_init_scenario_rejects_bad_id(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Invalid scenario_id"):
        init_scenario(tmp_path, "../evil")


def test_convert_jsonl_to_yaml(tmp_path: Path) -> None:
    import shutil

    from livekit_agent_simulator.config import ConfigError
    from livekit_agent_simulator.ops import convert_scenario

    scen = tmp_path / ".agent-sim" / "scenarios"
    scen.mkdir(parents=True)
    src = Path(__file__).resolve().parents[1] / "templates" / "examples" / "constraint-no-card.jsonl"
    dst = scen / "constraint-no-card.jsonl"
    shutil.copyfile(src, dst)

    r = convert_scenario(tmp_path, "constraint-no-card")
    assert Path(r["written_to"]).exists()
    assert r["written_to"].endswith(".yaml")
    assert Path(r["source"]).exists(), "original .jsonl must be left in place"

    # Both formats resolve by id.
    s = parse_scenario(Path(r["written_to"]))
    assert s.id == "constraint-no-card"
    assert s.persona.get("brief")

    # Idempotent: converting again is an error unless force.
    with pytest.raises(ConfigError, match="already exists"):
        convert_scenario(tmp_path, "constraint-no-card")


def test_convert_jsonl_to_yaml_overwrite(tmp_path: Path) -> None:
    import shutil

    from livekit_agent_simulator.ops import convert_scenario

    scen = tmp_path / ".agent-sim" / "scenarios"
    scen.mkdir(parents=True)
    src = Path(__file__).resolve().parents[1] / "templates" / "examples" / "constraint-no-card.jsonl"
    shutil.copyfile(src, scen / "constraint-no-card.jsonl")

    convert_scenario(tmp_path, "constraint-no-card")
    convert_scenario(tmp_path, "constraint-no-card", force=True)  # force overwrites


def test_convert_malformed_jsonl_fails_cleanly(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from livekit_agent_simulator.cli import app

    scen = tmp_path / ".agent-sim" / "scenarios"
    scen.mkdir(parents=True)
    (scen / "broken.jsonl").write_text(
        '{"kind":"Scenario","apiVersion":"agent-sim/v1","metadata":{"id":"broken"}}\n'
        "this is not json\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["convert", "broken", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "invalid JSON" in result.stderr
    assert "Traceback" not in result.stderr
