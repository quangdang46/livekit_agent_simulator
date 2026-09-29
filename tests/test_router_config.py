"""`router:` / `text_planner:` config blocks, validated at load.

Covers bead livekit-agent-simulator-response-router-v2-4-i4g. The D11
regression is the one that matters most: a config with no router block must
produce a config_snapshot byte-identical to one written before the feature
existed, so every golden fixture downstream keeps matching.
"""

from __future__ import annotations

import json

import pytest

from livekit_agent_simulator.config import (
    ConfigError,
    config_snapshot,
    load_config,
)

BASE = """
project: demo
livekit:
  url: "wss://demo.livekit.cloud"
  api_key: "APIkey"
  api_secret: "secret"
  agent_name: "my-agent-local"
simulator:
  provider: google
  api_key: "AIzaTest"
"""


def _write(tmp_path, text):
    dot = tmp_path / ".agent-sim"
    dot.mkdir()
    (dot / "config.yaml").write_text(text, encoding="utf-8")
    return tmp_path


def _load(tmp_path, text):
    return load_config(_write(tmp_path, text))


# --------------------------------------------------------------- happy paths


def test_router_block_parses(tmp_path):
    cfg = _load(
        tmp_path,
        BASE
        + """
router:
  provider: openai
  model: gpt-4.1-nano
  timeout_ms: 1500
  temperature: 0
""",
    )
    assert cfg.router is not None
    assert cfg.router.provider == "openai"
    assert cfg.router.model == "gpt-4.1-nano"
    assert cfg.router.timeout_ms == 1500
    assert cfg.router.temperature == 0.0
    # ms is the config surface; seconds is what the port consumes.
    assert cfg.router.timeout_s == 1.5


def test_gemini_provider_is_accepted(tmp_path):
    cfg = _load(tmp_path, BASE + "router:\n  provider: gemini\n")
    assert cfg.router is not None
    assert cfg.router.provider == "gemini"


def test_router_api_key_falls_back_to_simulator(tmp_path):
    cfg = _load(tmp_path, BASE + "router:\n  provider: openai\n")
    assert cfg.router is not None
    assert cfg.router.api_key == "AIzaTest"


def test_explicit_router_api_key_wins(tmp_path):
    cfg = _load(
        tmp_path, BASE + "router:\n  provider: openai\n  api_key: \"sk-router\"\n"
    )
    assert cfg.router is not None
    assert cfg.router.api_key == "sk-router"


def test_text_planner_block_parses(tmp_path):
    cfg = _load(
        tmp_path,
        BASE
        + """
text_planner:
  enabled: false
  provider: openai
  model: gpt-4.1-nano
""",
    )
    assert cfg.text_planner is not None
    assert cfg.text_planner.enabled is False
    assert cfg.text_planner.model == "gpt-4.1-nano"


def test_absent_blocks_stay_none(tmp_path):
    cfg = _load(tmp_path, BASE)
    assert cfg.router is None
    assert cfg.text_planner is None


# --------------------------------------------------------------- hard errors


def test_jev_names_the_missing_adapter_not_a_typo(tmp_path):
    """`jev` is a real target with no adapter. The error must say so, so the
    user does not go hunting for a misspelling."""
    with pytest.raises(ConfigError) as exc:
        _load(tmp_path, BASE + "router:\n  provider: jev\n")
    message = str(exc.value)
    assert "JevRouterAdapter" in message
    assert "not implemented" in message


def test_unknown_provider_is_a_plain_value_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        _load(tmp_path, BASE + "router:\n  provider: not-a-provider\n")
    message = str(exc.value)
    assert "router.provider" in message
    assert "not-a-provider" in message
    # A typo must NOT be dressed up as a missing adapter.
    assert "Adapter" not in message


@pytest.mark.parametrize("timeout_ms", [199, 60_001, 0, -5])
def test_timeout_ms_outside_bounds_is_rejected(tmp_path, timeout_ms):
    with pytest.raises(ConfigError) as exc:
        _load(tmp_path, BASE + f"router:\n  timeout_ms: {timeout_ms}\n")
    assert "router.timeout_ms" in str(exc.value)


@pytest.mark.parametrize("timeout_ms", [200, 1_500, 60_000])
def test_timeout_ms_bounds_are_inclusive(tmp_path, timeout_ms):
    cfg = _load(tmp_path, BASE + f"router:\n  timeout_ms: {timeout_ms}\n")
    assert cfg.router is not None
    assert cfg.router.timeout_ms == timeout_ms


def test_missing_api_key_everywhere_names_simulator_first(tmp_path):
    """With no key anywhere, the simulator check fires first — it runs before
    the router is parsed, and `simulator.api_key` is genuinely the missing
    thing. The router's own no-fallback guard is therefore unreachable through
    load_config; it is covered directly in the test below."""
    text = BASE.replace('  api_key: "AIzaTest"\n', "")
    with pytest.raises(ConfigError) as exc:
        _load(tmp_path, text + "router:\n  provider: openai\n")
    assert "simulator.api_key" in str(exc.value)


def test_resolve_api_key_guards_an_empty_fallback():
    """Defensive branch: the parse order makes this unreachable via load_config,
    but the helper must not hand back an empty key if that order ever changes.
    Called directly, because the public path cannot reach it."""
    from livekit_agent_simulator.config import _resolve_api_key

    with pytest.raises(ConfigError) as exc:
        _resolve_api_key({}, section_name="router", fallback="   ")
    assert "router.api_key" in str(exc.value)


def test_non_mapping_router_block_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as exc:
        _load(tmp_path, BASE + "router: not-a-mapping\n")
    assert "`router` must be a mapping" in str(exc.value)


def test_non_mapping_text_planner_block_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as exc:
        _load(tmp_path, BASE + "text_planner: 3\n")
    assert "`text_planner` must be a mapping" in str(exc.value)


# ----------------------------------------------------------------- snapshots


def test_snapshot_is_byte_identical_when_unconfigured(tmp_path):
    """D11 regression. The keys must be ABSENT, not null: a consumer doing
    `snapshot["router"]` on an old report must keep raising KeyError rather
    than silently reading a null it never expected."""
    cfg = _load(tmp_path, BASE)
    snap = config_snapshot(cfg)
    assert "router" not in snap
    assert "text_planner" not in snap


def test_snapshot_contains_no_key_material(tmp_path):
    cfg = _load(
        tmp_path,
        BASE
        + """
router:
  provider: openai
  api_key: "sk-super-secret"
text_planner:
  enabled: true
  api_key: "sk-also-secret"
""",
    )
    snap = config_snapshot(cfg)
    assert snap["router"]["api_key_set"] is True
    assert snap["text_planner"]["api_key_set"] is True
    rendered = json.dumps(snap)
    assert "sk-super-secret" not in rendered
    assert "sk-also-secret" not in rendered


def test_snapshot_reports_router_fields(tmp_path):
    cfg = _load(
        tmp_path, BASE + "router:\n  provider: openai\n  timeout_ms: 900\n"
    )
    snap = config_snapshot(cfg)
    assert snap["router"] == {
        "provider": "openai",
        "model": "",
        "api_key_set": True,
        "timeout_ms": 900,
        "temperature": 0.0,
    }


# The polished v2 surface dropped these as dead knobs (nothing increments or
# branches on them). Pin their absence so a later "helpful" re-add is caught.
@pytest.mark.parametrize("key", ["unknown_policy", "prompt_version", "reasoning_effort"])
def test_dead_knobs_stay_absent(tmp_path, key):
    cfg = _load(
        tmp_path, BASE + f"router:\n  provider: openai\n  {key}: something\n"
    )
    assert cfg.router is not None
    assert not hasattr(cfg.router, key)
    assert key not in config_snapshot(cfg)["router"]
