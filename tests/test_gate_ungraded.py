"""A run the judge never graded must not report `ok`.

Evidence (peer session, run 026-gpt-live-queue-fifo, 2026-09-30):

    CLI:  ok ✓   status done   gate soft   hard_reasons —
    DB:   judge.verdict { verdict: "error", score: null }
    no script.verify, no assert.verify

So the summary table said the run was fine while NOTHING had been graded.
An ungraded run is not a pass, and the previous gate could not tell the two
apart: `ok` was computed as `not hard_fail`, and a judge error contributed
only a *soft* note.

That is the same failure class this migration has removed all session — a
signal that looks alive because it has a renderer and a test, while being
structurally empty. `confidence` looked alive because a consumer wrote it
and a test asserted the field; here `ok ✓` looked alive because the gate
had no `ungraded` state.

The fix keeps three outcomes distinct, because collapsing any pair is the
bug:

  pass      graded, passed
  failed    graded, did not pass           (a regression)
  UNGRADED  the judge could not decide     (not evidence of anything)

A CI that cannot tell the third from the first learns to ignore the gate;
one that cannot tell it from the second goes red on every judge blip.
"""

from __future__ import annotations

from livekit_agent_simulator.suite import evaluate_run_result


def _result(**kw):
    base = {
        "executed": True,
        "status": "done",
        "summary": {},
    }
    base.update(kw)
    return base


def _with_verdict(verdict: str, score=None) -> dict:
    return _result(
        summary={"verdict": {"verdict": verdict, "score": score, "notes": ""}}
    )


# --------------------------------------------------------------------------
# the regression
# --------------------------------------------------------------------------


def test_a_judge_error_is_not_ok():
    g = evaluate_run_result(_with_verdict("error"))
    assert g["ok"] is False, (
        "the judge could not grade this run; reporting ok is a false pass"
    )


def test_a_judge_error_is_flagged_as_ungraded_not_hard_failed():
    g = evaluate_run_result(_with_verdict("error"))
    assert g["ungraded"] is True
    assert g["hard_fail"] is False
    assert g["hard_reasons"] == [], (
        "a judge HTTP blip is not evidence of a regression, so it must not "
        "pollute hard_reasons"
    )


def test_the_gate_string_says_ungraded():
    g = evaluate_run_result(_with_verdict("error"))
    assert g["gate"] == "ungraded", (
        "'soft' reads as a near-miss; this run was never judged at all"
    )


def test_ungraded_is_distinguishable_from_a_real_failure():
    ungraded = evaluate_run_result(_with_verdict("error"))
    failed = evaluate_run_result(
        _result(
            status="done",
            summary={
                "verdict": {"verdict": "fail", "score": 0.2, "notes": "wrong node"},
                "assert_verify": {"pass": False},
            },
        )
    )
    assert ungraded["gate"] != failed["gate"]
    assert ungraded["hard_fail"] != failed["hard_fail"]
    assert failed["hard_reasons"], "a real failure must still be a hard failure"


# --------------------------------------------------------------------------
# what must NOT change
# --------------------------------------------------------------------------


def test_a_real_judge_pass_is_still_ok():
    g = evaluate_run_result(_with_verdict("pass", score=0.9))
    assert g["ok"] is True
    assert g["ungraded"] is False
    assert g["gate"] == "pass"


def test_a_soft_fail_is_still_ok_by_default():
    """`judge_fail` stays soft unless --strict-judge, which is a policy
    choice, not a false-pass. Unchanged here."""
    g = evaluate_run_result(_with_verdict("fail", score=0.2))
    assert g["ok"] is True
    assert g["soft_fail"] is True
    assert evaluate_run_result(_with_verdict("fail", score=0.2), strict_judge=True)["ok"] is False


def test_a_scenario_with_no_pass_criteria_is_unaffected():
    """No verdict key at all means the scenario had no rubric — a legitimate
    pass. Only a judge that RAN and FAILED counts as ungraded."""
    g = evaluate_run_result(_result())
    assert g["ok"] is True
    assert g["ungraded"] is False


def test_a_skipped_judge_is_unaffected():
    """`skipped` has the same meaning as no PassCriteria."""
    g = evaluate_run_result(_with_verdict("skipped"))
    assert g["ok"] is True
    assert g["ungraded"] is False


def test_maybe_is_still_soft():
    g = evaluate_run_result(_with_verdict("maybe"))
    assert g["ok"] is True
    assert "judge_maybe" in g["soft_reasons"]


# --------------------------------------------------------------------------
# the key stays stable
# --------------------------------------------------------------------------


def test_every_gate_outcome_exposes_the_same_keys():
    """The renderer reads these; a key that appears only on one outcome is a
    crash waiting for the path nobody exercised."""
    outcomes = [
        evaluate_run_result(_with_verdict("pass", 0.9)),
        evaluate_run_result(_with_verdict("error")),
        evaluate_run_result(_with_verdict("fail", 0.1)),
        evaluate_run_result(_result()),
    ]
    expected = {
        "ok", "hard_fail", "soft_fail", "ungraded",
        "hard_reasons", "soft_reasons", "gate",
    }
    for got in outcomes:
        assert set(got) == expected, f"key set drifted: {set(got) ^ expected}"


# --------------------------------------------------------------------------
# the judge error must be diagnosable
# --------------------------------------------------------------------------


def test_a_judge_error_reports_enough_to_diagnose_it():
    """A bare "TimeoutError: timed out" cost hours on 2026-09-30.

    It said nothing about prompt size, elapsed time, or how many criteria
    were being graded — so there was no way to distinguish a slow endpoint
    from a slow generation, and the obvious hypotheses (dead endpoint, huge
    prompt, broken config) all had to be eliminated by hand.
    """
    import asyncio

    from livekit_agent_simulator.evals import runner as R

    class _Boom:
        async def complete_json(self, *, system, user):
            raise TimeoutError("timed out")

    def _fake_backend(*a, **k):
        return _Boom()

    turns = [
        {"turn": 1, "user_text": "hello there", "agent_text": "hi, who are you?"},
        {"turn": 2, "user_text": "acme", "agent_text": "thanks"},
    ]
    real_backend_from_config = R.backend_from_config
    R.backend_from_config = _fake_backend  # type: ignore[assignment]
    try:
        out = asyncio.run(
            R.judge_run(
                R.JudgeConfig(base_url="http://x/v1", api_key="k", model="m"),
                "sk-x",
                ["criterion one", "criterion two"],
                turns,
                [],
            )
        )
    finally:
        R.backend_from_config = real_backend_from_config

    assert out["verdict"] == "error"
    notes = out["notes"]
    for token in ("prompt_chars=", "transcript_chars=", "criteria=2", "turns=2", "elapsed_s="):
        assert token in notes, f"notes must carry {token!r} to be diagnosable: {notes!r}"
