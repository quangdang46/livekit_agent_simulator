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


def test_init_scenario_writes_valid_jsonl_with_slash_comments(tmp_path: Path) -> None:
    result = init_scenario(tmp_path, "order-cancel")
    path = Path(result["path"])
    assert path.exists()
    assert result["scenario_id"] == "order-cancel"

    text = path.read_text(encoding="utf-8")
    assert text.lstrip().startswith("//")
    assert '"_doc"' not in text
    assert "order-cancel" in text
    assert "// === Scenario" in text
    assert "// === Persona" in text
    assert "// metadata.id:" in text

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
