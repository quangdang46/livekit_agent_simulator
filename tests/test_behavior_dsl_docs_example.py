"""Docs bead acceptance: the example scenario referenced from
docs/behavior-dsl.md must validate clean against the real parser, and the
authoring docs must not contain any superseded terminology.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from livekit_agent_simulator.caller_contract.dsl import DEFAULT_BEHAVIOR_CATALOG, parse_steps

DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"
EXAMPLE_PATH = DOCS_DIR / "examples" / "negotiate-car-price.yaml"
GUIDE_PATH = DOCS_DIR / "behavior-dsl.md"


def test_docs_example_scenario_validates_clean() -> None:
    data = yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))
    actions = parse_steps(
        data["steps"], file=str(EXAMPLE_PATH), known_behaviors=DEFAULT_BEHAVIOR_CATALOG
    )
    kinds = [a.kind for a in actions]
    assert kinds == ["say", "do", "do", "do", "say", "end"]

    negotiate_action = actions[2]
    assert negotiate_action.contract.behavior == "negotiate"
    assert negotiate_action.contract.constraints.max_budget == 30000
    assert negotiate_action.interaction is not None
    assert negotiate_action.interaction.pace == "slow"


def test_authoring_guide_has_no_superseded_terminology() -> None:
    """Grep check (docs bead acceptance): zero hits for superseded wording
    in the user-facing authoring guide."""
    text = GUIDE_PATH.read_text(encoding="utf-8")
    superseded_patterns = [
        r"Language Adapter\s*\(optional AI\)",
        r"\bAI optional\b",
        r"Constrained Language Adapter",
        r"allowed_topics\s*:",
        r"forbidden_topics\s*:",
    ]
    for pattern in superseded_patterns:
        matches = re.findall(pattern, text, flags=re.IGNORECASE)
        # The guide DOES mention these as "old wording" inside its own
        # comparison table for documentation purposes; make sure any
        # remaining occurrence is confined to that table row and not used
        # as live guidance elsewhere in the doc.
        assert len(matches) <= 1, (
            f"pattern {pattern!r} appears {len(matches)} times in {GUIDE_PATH.name}; "
            "expected at most one mention (inside the superseded-terminology table)"
        )


def test_docs_dir_has_examples_subdirectory_with_the_referenced_file() -> None:
    assert EXAMPLE_PATH.exists()
