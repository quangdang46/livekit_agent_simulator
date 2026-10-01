"""P1.G / #27 — authoring quality gate (rule-based soft warnings, no LLM)."""

from __future__ import annotations

from types import SimpleNamespace

from livekit_agent_simulator.authoring import (
    authoring_scorecard,
    authoring_tier,
    build_authoring_report,
    collect_authoring_findings,
    collect_authoring_warnings,
)
from livekit_agent_simulator.script.models import ScriptStep


def _scenario(**kwargs):
    base = dict(
        persona={"brief": "caller", "goals": ["Ask for status"], "traits": ["polite"]},
        script_steps=[],
        behavior_spec=None,
        script_verify=None,
        asserts=None,
        tags=["smoke"],
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_empty_goals_warns():
    s = _scenario(persona={"brief": "x", "goals": [], "traits": []})
    w = collect_authoring_warnings(s)
    assert any("goals" in x.lower() for x in w)
    codes = {f.code for f in collect_authoring_findings(s)}
    assert "empty_goals" in codes


def test_stress_trait_without_script_warns():
    s = _scenario(persona={"brief": "x", "goals": ["g"], "traits": ["interrupts"]})
    w = collect_authoring_warnings(s)
    assert any("interrupts" in x for x in w)
    assert "stress_trait_without_interaction" in {
        f.code for f in collect_authoring_findings(s)
    }


def test_barge_without_recovery_assert_warns():
    step = ScriptStep(
        id="b1",
        trigger="agent_speaking",
        delay_ms=200,
        say="Wait",
        barge_in=True,
        interrupt_class="correction",
    )
    s = _scenario(script_steps=[step], persona={"brief": "x", "goals": ["g"]})
    w = collect_authoring_warnings(s)
    assert any("recovery" in x.lower() for x in w)
    assert "barge_without_recovery" in {f.code for f in collect_authoring_findings(s)}


def test_barge_with_recovery_assert_clean():
    step = ScriptStep(
        id="b1",
        trigger="agent_speaking",
        delay_ms=200,
        say="Wait",
        barge_in=True,
        interrupt_class="correction",
    )
    asserts = SimpleNamespace(
        outcomes=[SimpleNamespace(type="recovery", id="r")],
    )
    s = _scenario(
        script_steps=[step],
        asserts=asserts,
        persona={"brief": "x", "goals": ["g"], "traits": ["interrupts"]},
    )
    w = collect_authoring_warnings(s)
    assert not any("recovery" in x.lower() and "no Assert" in x for x in w)
    assert "barge_without_recovery" not in {
        f.code for f in collect_authoring_findings(s) if f.severity == "warn"
    }


def test_noise_barge_does_not_require_recovery():
    step = ScriptStep(
        id="n1",
        trigger="agent_speaking",
        delay_ms=200,
        say="[noise]",
        barge_in=True,
        interrupt_class="noise",
    )
    s = _scenario(script_steps=[step], persona={"brief": "x", "goals": ["g"]})
    w = collect_authoring_warnings(s)
    assert not any("Recovery barge" in x for x in w)


def test_hang_up_without_ended_by_warns():
    step = ScriptStep(
        id="h1", trigger="time", delay_ms=100, say="bye", action="hang_up"
    )
    s = _scenario(script_steps=[step], persona={"brief": "x", "goals": ["g"]})
    w = collect_authoring_warnings(s)
    assert any("ended_by" in x for x in w)
    assert "hang_up_without_ended_by" in {f.code for f in collect_authoring_findings(s)}


def test_constraint_without_assert_warns():
    s = _scenario(
        persona={
            "brief": "x",
            "goals": ["g"],
            "constraints": ["Will not share card numbers"],
        }
    )
    codes = {f.code for f in collect_authoring_findings(s) if f.severity == "warn"}
    assert "constraint_without_assert" in codes


def test_constraint_with_assert_clean():
    asserts = SimpleNamespace(
        outcomes=[SimpleNamespace(type="constraint_respected", id="c")],
    )
    s = _scenario(
        persona={
            "brief": "x",
            "goals": ["g"],
            "constraints": ["Will not share card numbers"],
        },
        asserts=asserts,
    )
    codes = {f.code for f in collect_authoring_findings(s) if f.severity == "warn"}
    assert "constraint_without_assert" not in codes


def test_no_tags_is_info_not_warn():
    s = _scenario(tags=[], persona={"brief": "x", "goals": ["g"]})
    findings = collect_authoring_findings(s)
    no_tags = [f for f in findings if f.code == "no_tags"]
    assert no_tags and no_tags[0].severity == "info"
    # flat warn list should not include no_tags message
    assert not any("no metadata.tags" in m for m in collect_authoring_warnings(s))


def test_no_risk_tag_warns_when_tags_present():
    s = _scenario(tags=["billing"], persona={"brief": "x", "goals": ["g"]})
    codes = {f.code for f in collect_authoring_findings(s) if f.severity == "warn"}
    assert "no_risk_tag" in codes


def test_scorecard_totals():
    s = _scenario(
        persona={
            "brief": "x",
            "goals": ["g"],
            "constraints": ["no card"],
            "traits": ["polite"],
        },
        script_steps=[
            ScriptStep(
                id="b",
                trigger="agent_speaking",
                delay_ms=1,
                say="x",
                barge_in=True,
                interrupt_class="correction",
            )
        ],
        asserts=SimpleNamespace(
            outcomes=[
                SimpleNamespace(type="recovery"),
                SimpleNamespace(type="constraint_respected"),
            ]
        ),
        tags=["smoke", "regression"],
    )
    sc = authoring_scorecard(s)
    assert sc["max"] == 12
    assert sc["total"] >= 8


def test_build_authoring_report_tier_and_codes():
    weak = _scenario(persona={"brief": "x", "goals": []}, tags=[])
    rep = build_authoring_report(weak)
    assert rep["soft"] is True
    assert rep["tier"] == "exploratory"
    assert "empty_goals" in rep["warning_codes"]
    assert "scorecard" in rep and rep["scorecard"]["max"] == 12

    strong = _scenario(
        persona={
            "brief": "x",
            "goals": ["g"],
            "constraints": ["no card"],
        },
        script_steps=[
            ScriptStep(
                id="b",
                trigger="agent_speaking",
                delay_ms=1,
                say="x",
                barge_in=True,
                interrupt_class="correction",
            )
        ],
        asserts=SimpleNamespace(
            outcomes=[
                SimpleNamespace(type="recovery"),
                SimpleNamespace(type="constraint_respected"),
            ]
        ),
        tags=["smoke"],
    )
    rep2 = build_authoring_report(strong)
    assert rep2["tier"] in ("blocking", "scheduled")
    assert "barge_without_recovery" not in rep2["warning_codes"]


def test_authoring_tier_helper():
    sc = {"total": 10, "max": 12}
    assert authoring_tier(sc, []) == "blocking"
    from livekit_agent_simulator.authoring import AuthoringWarning

    assert (
        authoring_tier(
            sc,
            [AuthoringWarning(code="empty_goals", message="x")],
        )
        == "exploratory"
    )


# ---------------------------------------------------------------------------
# Interaction shaping keys the delivery layer cannot apply.
#
# plan_speak computes pace/hesitation/stumble and only pre_delay_ms is used:
# the contract path synthesizes the EXACT validated utterance, so inserting a
# token after validation would put unvalidated words on the wire. The DSL
# still accepts the keys, so an author would reasonably assume speech is
# being shaped — warn instead of staying silent.
# ---------------------------------------------------------------------------


def _interaction_scenario(interaction):
    action = SimpleNamespace(kind="say", interaction=interaction)
    return _scenario(caller_actions=[action])


def _codes(scenario):
    return {f.code for f in collect_authoring_findings(scenario)}


def test_interaction_hesitation_and_stumble_warn_as_not_applied():
    interaction = SimpleNamespace(pace=None, hesitation="low", stumble="low", pre_delay_ms=300)
    assert "interaction_shaping_not_applied" in _codes(_interaction_scenario(interaction))


def test_interaction_pace_warns_as_not_applied():
    interaction = SimpleNamespace(pace="slow", hesitation=None, stumble=None, pre_delay_ms=200)
    assert "interaction_shaping_not_applied" in _codes(_interaction_scenario(interaction))


def test_interaction_pre_delay_only_does_not_warn():
    """pre_delay_ms IS applied, so it must not be flagged."""
    interaction = SimpleNamespace(pace=None, hesitation=None, stumble=None, pre_delay_ms=200)
    assert "interaction_shaping_not_applied" not in _codes(_interaction_scenario(interaction))


def test_scenario_without_caller_actions_does_not_warn():
    assert "interaction_shaping_not_applied" not in _codes(_scenario())


# ---------------------------------------------------------------------------
# the dtmf warning follows the surface that can actually send tones
# (dtmf-restore 3tv.5.2)
# ---------------------------------------------------------------------------


def test_dtmf_warning_fires_for_caller_steps():
    """`caller_steps: - dtmf:` is the surface with an execution path.

    The warning used to filter on the legacy `script_steps`, which
    `665ec8c` left with no execution path at all — so it promised DTMF
    handling for a verb that cannot run, and stayed silent about the verb that
    does. Neither direction had a test, which is how it sat there.
    """
    from livekit_agent_simulator.caller_contract.dsl import parse_steps

    s = _scenario(
        persona={"brief": "x", "goals": ["g"], "traits": []},
        caller_actions=parse_steps(
            [{"dtmf": "123"}, {"end": True}], file="t"
        ),
    )
    codes = {f.code for f in collect_authoring_findings(s)}
    assert "dtmf_untagged_draft" in codes


def test_dtmf_warning_does_not_fire_for_the_legacy_surface():
    """A dtmf in `script_steps` does nothing, so promising on it is wrong."""
    s = _scenario(
        persona={"brief": "x", "goals": ["g"], "traits": []},
        script_steps=[
            ScriptStep(
                id="d1",
                trigger="time",
                delay_ms=0,
                action="dtmf",
                digits="123",
            ),
        ],
    )
    codes = {f.code for f in collect_authoring_findings(s)}
    assert "dtmf_untagged_draft" not in codes, (
        "the warning is attached to a surface with no execution path again"
    )


def test_a_tagged_draft_stays_quiet():
    """The `draft` tag is the documented opt-out and must keep working."""
    from livekit_agent_simulator.caller_contract.dsl import parse_steps

    s = _scenario(
        persona={"brief": "x", "goals": ["g"], "traits": []},
        caller_actions=parse_steps([{"dtmf": "1"}, {"end": True}], file="t"),
        tags=["smoke", "draft"],
    )
    codes = {f.code for f in collect_authoring_findings(s)}
    assert "dtmf_untagged_draft" not in codes


def test_the_warning_names_the_caller_surface_not_the_legacy_one():
    """The message itself promised the wrong syntax."""
    from livekit_agent_simulator.caller_contract.dsl import parse_steps

    s = _scenario(
        persona={"brief": "x", "goals": ["g"], "traits": []},
        caller_actions=parse_steps([{"dtmf": "1"}, {"end": True}], file="t"),
    )
    msg = next(
        f.message
        for f in collect_authoring_findings(s)
        if f.code == "dtmf_untagged_draft"
    )
    assert "caller_steps" in msg and "`- dtmf:`" in msg
    assert "Script action=dtmf" not in msg


# ---------------------------------------------------------------------------
# an overlapping catalog entry is flagged at authoring time
# ---------------------------------------------------------------------------


def _catalog_scenario(instruction: str):
    import tempfile
    from pathlib import Path

    from livekit_agent_simulator.scenario import parse_scenario

    d = Path(tempfile.mkdtemp()) / "s.yaml"
    d.write_text(
        "apiVersion: agent-sim/v1\n"
        "kind: Scenario\n"
        "metadata: {id: t}\n"
        "persona: {brief: x, goals: [g]}\n"
        "responses:\n"
        f"  ack: {{intent: ack, instruction: {instruction!r}, text: 'Welcome.'}}\n"
        "  sys:\n"
        "    intent: off\n"
        "    instruction: 'Select only when nothing else matches.'\n"
        "    text: 'Sorry?'\n"
        "    system: true\n",
        encoding="utf-8",
    )
    return parse_scenario(d)


def test_a_negation_clause_in_an_instruction_is_flagged():
    """A "never choose this when" clause is a symptom, not a style choice.

    Measured 2026-09-30 on the target repo: an entry reading "Never choose
    this for a turn that contains a question" was selected at 0.950 confidence
    FOR a turn that contained a question. It had been narrowed into that shape
    because a wider entry kept winning the same turns — so rewriting
    instructions relocated the failure rather than fixing it.

    Flagged here because authoring time is where it is free. The router cannot
    see the overlap, and a confidence floor cannot catch it either, because the
    wrong pick scores HIGHER than the right one.
    """
    s = _catalog_scenario(
        "Select for a thank-you. Never choose this for a question."
    )
    codes = {f.code for f in collect_authoring_findings(s)}
    assert "response_instruction_negation" in codes, sorted(codes)


def test_a_catalog_without_a_negation_clause_is_quiet():
    """The warning must not fire on a catalog that is fine.

    It matches a CLAUSE, not an overlap, because an overlap cannot be detected
    without a model — and a false warning on a good catalog is worse than
    silence, because it teaches people to ignore the warning.
    """
    s = _catalog_scenario("Select only for a thank-you.")
    codes = {f.code for f in collect_authoring_findings(s)}
    assert "response_instruction_negation" not in codes, sorted(codes)
