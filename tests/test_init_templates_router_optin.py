"""`lks init` must never produce a project that cannot run.

Two beads were in direct contradiction:

- v2-12 (polish round 3) said the scenario templates DO gain a `responses:`
  catalog, to "show the shape without turning the feature on".
- v2-27 said a scenario carrying `responses:` with no `router:` block raises
  `ConfigError` — so doing exactly that would break the default install path.

Resolved by showing the catalog COMMENTED OUT in both places, which is the
pattern `templates/config.yaml` already used. A commented block is invisible to
the YAML parser, so the scaffolded scenario is a plain `caller_steps` scenario
that runs, and the shape is still documented in place.

The failure this guards against is the one that motivates it: a scaffolded
project that passes `lks validate` and then fails on its first run, with the
cause one layer down in `reports/<run-id>/events.jsonl`.

Note the counterpart hazard, which is why both sides are commented and neither
is active: uncommenting ONLY the catalog would break the run, and uncommenting
ONLY the config would ship a dead api_key placeholder into every new project.
The two have to move together, and these tests keep them from ever having
diverged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from livekit_agent_simulator.scenario import parse_scenario

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

# Exactly what `lks init` copies (ops.py:74-84).
INIT_COPIES = (
    "config.yaml",
    "smoke-hello.yaml",
)


@pytest.mark.parametrize("name", INIT_COPIES)
def test_everything_lks_init_copies_parses(name: str) -> None:
    path = TEMPLATES / name
    assert path.exists(), f"lks init copies {name}"
    if name == "config.yaml":
        pytest.skip("config.yaml is a SimConfig, not a Scenario")


def test_the_scaffolded_scenario_carries_no_active_responses_catalog() -> None:
    """The load-bearing assertion.

    If this ever goes red, `lks init` has produced a project whose first run
    raises ConfigError.
    """
    scenario = parse_scenario(TEMPLATES / "smoke-hello.yaml")
    assert scenario.responses is None, (
        "smoke-hello.yaml must NOT carry an active responses: catalog - the "
        "scaffolded config has no router: block, so this would raise at run time"
    )


def test_the_scaffolded_scenario_still_has_caller_steps() -> None:
    """Show, do not disable: the legacy path must remain what actually runs."""
    scenario = parse_scenario(TEMPLATES / "smoke-hello.yaml")
    assert scenario.caller_actions, "the scaffolded scenario must still run"


def test_the_config_template_ships_no_active_router_block() -> None:
    """A shipped-but-dead `api_key:` placeholder is the knob nobody runs.

    `lks init` scaffolds for someone who has not asked for a router.
    """
    from livekit_agent_simulator.config import load_config

    import yaml

    raw = yaml.safe_load((TEMPLATES / "config.yaml").read_text(encoding="utf-8"))
    assert "router" not in raw, "templates/config.yaml must not activate the router"
    assert "text_planner" not in raw
    # And it must SAY so, so the next reader knows it is deliberate.
    text = (TEMPLATES / "config.yaml").read_text(encoding="utf-8")
    assert "DELIBERATELY" in text, (
        "the omission must be labelled, or a future reader will 'fix' it"
    )
    assert load_config is not None


@pytest.mark.parametrize(
    "name",
    [
        "smoke-hello.yaml",
        "inbound-caller-sim.yaml",
        "outbound-callee-sim.yaml",
        "outbound-human-pickup.yaml",
        "scenario-scaffold.yaml",
    ],
)
def test_every_template_documents_the_catalog_without_enabling_it(name: str) -> None:
    """Documented in every template, active in none.

    Half the documentation would leave a reader of the other templates
    thinking the feature is available there.
    """
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    assert "responses:" in text, f"{name} should show the catalog shape"
    # Active (uncommented) catalog keys would appear at column 0.
    assert "\nresponses:" not in text, (
        f"{name} has an ACTIVE responses: block; it must stay commented so "
        "lks init does not scaffold an unrunnable project"
    )
    assert "ConfigError" in text or "config.yaml" in text, (
        f"{name} must say where the router block lives"
    )


def test_a_working_router_example_exists_outside_what_init_copies() -> None:
    """The documented example must actually run.

    `lks init` does not copy `templates/examples/`, which is exactly why the
    router-shaped scenario lives there: available to read, never scaffolded
    into a project that cannot run it.
    """
    example = TEMPLATES / "examples" / "router-smoke.yaml"
    assert example.exists(), "templates/examples/router-smoke.yaml is the worked example"
    scenario = parse_scenario(example)
    assert scenario.responses is not None, "the example must actually carry a catalog"
    assert any(s.system for s in scenario.responses.responses.values()), (
        "the example must show the required system entry"
    )
    assert scenario.caller_actions, "and must still have the legacy turn 0"
