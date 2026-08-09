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
