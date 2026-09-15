"""P0-3: Behavior DSL parser tests.

Covers shorthand and explicit forms, unknown-behavior error, the two
canonical example scenarios (ask_price, negotiate with
max_budget/max_turns/interaction), validation errors for unknown keys, and
the say-bypasses-AI-and-validator-but-never-the-orchestrator-turn-gate
invariant (report §28.5(17)).
"""

from __future__ import annotations

import pytest

from livekit_agent_simulator.caller_contract.dsl import (
    DEFAULT_BEHAVIOR_CATALOG,
    DSLError,
    parse_step,
    parse_steps,
)


# ---------------------------------------------------------------------------
# say: exact utterances
# ---------------------------------------------------------------------------


def test_say_shorthand_plain_string() -> None:
    action = parse_step({"say": "Hi, I'm calling about my car."}, line_no=1)
    assert action.kind == "say"
    assert action.say_text == "Hi, I'm calling about my car."


def test_say_rejects_mapping_value() -> None:
    with pytest.raises(DSLError, match="say: must be a plain string"):
        parse_step({"say": {"text": "not allowed"}}, line_no=1)


def test_say_rejects_empty_string() -> None:
    with pytest.raises(DSLError, match="say: must not be empty"):
        parse_step({"say": "   "}, line_no=1)


def test_say_bypasses_ai_and_validator_but_not_orchestrator_turn_gate() -> None:
    """say never calls AI/Validator, but MUST still require the
    Orchestrator's caller-turn gate — never a parser-direct publish
    (report §28.5(17)).
    """
    action = parse_step({"say": "Hi."}, line_no=1)
    assert action.bypasses_ai_and_validator is True
    assert action.requires_turn_gate is True


# ---------------------------------------------------------------------------
# do: shorthand form
# ---------------------------------------------------------------------------


def test_do_shorthand_string_form() -> None:
    action = parse_step({"do": "ask"}, line_no=1)
    assert action.kind == "do"
    assert action.contract is not None
    assert action.contract.behavior == "ask"
    assert action.contract.target is None
    assert action.bypasses_ai_and_validator is False
    assert action.requires_turn_gate is True


# ---------------------------------------------------------------------------
# do: explicit mapping form
# ---------------------------------------------------------------------------


def test_do_explicit_mapping_form_full_fields() -> None:
    action = parse_step(
        {
            "do": {
                "behavior": "negotiate",
                "target": "price",
                "constraints": {
                    "max_turns": 3,
                    "max_budget": 30000,
                    "forbidden_intents": ["financing"],
                },
                "interaction": {"pace": "slow", "hesitation": "occasional"},
            }
        },
        line_no=1,
    )
    assert action.contract.behavior == "negotiate"
    assert action.contract.target == "price"
    assert action.contract.constraints.max_turns == 3
    assert action.contract.constraints.max_budget == 30000
    assert action.contract.constraints.forbidden_intents == ["financing"]
    assert action.interaction is not None
    assert action.interaction.pace == "slow"
    assert action.interaction.hesitation == "occasional"


def test_do_requires_behavior_key_in_mapping_form() -> None:
    with pytest.raises(DSLError, match="requires a 'behavior' key"):
        parse_step({"do": {"target": "price"}}, line_no=1)


def test_do_rejects_unknown_top_level_key() -> None:
    with pytest.raises(DSLError, match="unknown do: key"):
        parse_step({"do": {"behavior": "ask", "bogus_field": 1}}, line_no=5)


def test_do_rejects_unknown_constraints_key() -> None:
    with pytest.raises(DSLError, match="unknown constraints key"):
        parse_step({"do": {"behavior": "ask", "constraints": {"allowed_topics": ["price"]}}}, line_no=1)


def test_do_rejects_unknown_interaction_key() -> None:
    with pytest.raises(DSLError, match="unknown interaction key"):
        parse_step({"do": {"behavior": "ask", "interaction": {"emotion": "happy"}}}, line_no=1)


# ---------------------------------------------------------------------------
# unknown-behavior error (must name file, line, field)
# ---------------------------------------------------------------------------


def test_unknown_behavior_error_includes_file_and_line() -> None:
    with pytest.raises(DSLError) as exc_info:
        parse_step({"do": "fly_to_the_moon"}, line_no=7, file="scenario.jsonl")
    message = str(exc_info.value)
    assert "scenario.jsonl:7" in message
    assert "fly_to_the_moon" in message
    assert "unknown behavior" in message


def test_known_behaviors_can_be_extended_per_scenario() -> None:
    """Behavior catalog is an allowlist loaded per-scenario, not hardcoded
    business strings in core (AGENTS.md generic-core rule)."""
    action = parse_step(
        {"do": "check_inventory"},
        line_no=1,
        known_behaviors=DEFAULT_BEHAVIOR_CATALOG | {"check_inventory"},
    )
    assert action.contract.behavior == "check_inventory"


# ---------------------------------------------------------------------------
# other action kinds: wait / dtmf / interrupt / end / silence / hangup
# ---------------------------------------------------------------------------


def test_wait_action() -> None:
    action = parse_step({"wait": 500}, line_no=1)
    assert action.kind == "wait"
    assert action.wait_ms == 500


def test_wait_rejects_negative_duration() -> None:
    with pytest.raises(DSLError, match="wait: must be a non-negative integer"):
        parse_step({"wait": -10}, line_no=1)


def test_dtmf_action() -> None:
    action = parse_step({"dtmf": "123#"}, line_no=1)
    assert action.kind == "dtmf"
    assert action.dtmf_digits == "123#"


def test_dtmf_rejects_empty_digits() -> None:
    with pytest.raises(DSLError, match="dtmf: must be a non-empty digit string"):
        parse_step({"dtmf": ""}, line_no=1)


@pytest.mark.parametrize("kind", ["interrupt", "end", "silence", "hangup"])
def test_control_actions_parse(kind: str) -> None:
    action = parse_step({kind: True}, line_no=1)
    assert action.kind == kind
    assert action.requires_turn_gate is True


# ---------------------------------------------------------------------------
# structural errors
# ---------------------------------------------------------------------------


def test_step_with_no_recognized_key_raises() -> None:
    with pytest.raises(DSLError, match="no recognized action key"):
        parse_step({"foo": "bar"}, line_no=1)


def test_step_with_multiple_action_keys_raises() -> None:
    with pytest.raises(DSLError, match="multiple action keys"):
        parse_step({"say": "hi", "wait": 100}, line_no=1)


# ---------------------------------------------------------------------------
# Two canonical example scenarios (ask_price, negotiate)
# ---------------------------------------------------------------------------


def test_canonical_scenario_ask_price_validates_clean() -> None:
    steps = [
        {"say": "Hi."},
        {"say": "I'm calling about the 2022 Honda CR-V."},
        {"do": "ask"},
    ]
    actions = parse_steps(steps, file="ask_price.jsonl")
    assert [a.kind for a in actions] == ["say", "say", "do"]
    assert actions[2].contract.behavior == "ask"


def test_canonical_scenario_negotiate_with_full_config_validates_clean() -> None:
    steps = [
        {"say": "I'm calling about the 2022 Honda CR-V."},
        {"do": "ask"},
        {
            "do": {
                "behavior": "negotiate",
                "target": "price",
                "constraints": {
                    "max_turns": 3,
                    "max_budget": 30000,
                    "forbidden_intents": ["financing", "trade_in", "vehicle_change"],
                    "must_not": ["invent_facts", "end_call"],
                },
                "interaction": {"pace": "slow", "hesitation": "occasional"},
            }
        },
        {"say": "Thanks, bye."},
        {"end": True},
    ]
    actions = parse_steps(steps, file="negotiate.jsonl")
    assert len(actions) == 5
    negotiate_action = actions[2]
    assert negotiate_action.contract.behavior == "negotiate"
    assert negotiate_action.contract.constraints.max_turns == 3
    assert negotiate_action.contract.constraints.max_budget == 30000
    assert actions[-1].kind == "end"


def test_parse_steps_preserves_order_and_line_numbers() -> None:
    steps = [{"say": "one"}, {"say": "two"}, {"say": "three"}]
    actions = parse_steps(steps)
    assert [a.line_no for a in actions] == [1, 2, 3]
    assert [a.say_text for a in actions] == ["one", "two", "three"]
