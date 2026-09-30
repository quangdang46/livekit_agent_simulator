"""The report player must not render an empty panel for a routed run.

`web/api.py` and `web/cues.py` referenced only `caller_steps` / `behavior`.
After the migration a routed report grows
`summary["caller_contract"]["router"]`, and a player still reading the old key
does not crash — it renders an empty or stale panel. The report looks fine and
the routing evidence is invisible to the human reviewing the call.

That is the same failure class as an unread snapshot key, in a place no test
guarded. These tests pin the three states the player must be able to tell
apart, because they mean different things to whoever is debugging a call.
"""

from __future__ import annotations

from livekit_agent_simulator.web.cues import _router_evidence


# --------------------------------------------------------------------------
# the three states
# --------------------------------------------------------------------------


def test_a_routed_run_exposes_the_counts() -> None:
    got = _router_evidence(
        {
            "router": {
                "matched": 4,
                "off_script": 1,
                "faults": 2,
                "unroutable": 1,
                "decisions": [{"response_id": "company", "off_script": False}],
            }
        }
    )
    assert got["available"] is True
    assert (got["matched"], got["off_script"]) == (4, 1)
    assert len(got["decisions"]) == 1
    assert got["reason"] == ""


def test_a_caller_steps_run_says_so_rather_than_showing_nothing() -> None:
    """`caller_contract` without `router` is the NORMAL legacy shape."""
    got = _router_evidence({"contract": "anything"})
    assert got["available"] is False
    assert "caller_steps" in got["reason"], (
        "the panel must be able to say 'this run was not routed' instead of "
        "rendering an empty box"
    )


def test_a_report_with_no_contract_block_says_why() -> None:
    got = _router_evidence(None)
    assert got["available"] is False
    assert "predates" in got["reason"] or "no caller_contract" in got["reason"]


def test_a_malformed_router_block_is_named_as_a_defect_not_a_legacy_run() -> None:
    """The important distinction.

    "not routed" and "routed but unreadable" look identical to a player that
    only checks for a falsy value — and they mean completely different things.
    The second is a bug the human is looking for.
    """
    got = _router_evidence({"router": "oops"})
    assert got["available"] is False
    assert "malformed" in got["reason"]
    assert "caller_steps" not in got["reason"], (
        "a malformed block must not be reported as an ordinary legacy run"
    )


# --------------------------------------------------------------------------
# attribution must not be laundered
# --------------------------------------------------------------------------


def test_harness_problems_are_not_counted_as_agent_deviations() -> None:
    """`faults` and `unroutable` are HARNESS problems.

    They are counted outside the decision list in the summary precisely so a
    reader cannot add them into "the agent went off-script". A player that
    offered a single total would reintroduce the false accusation `off_script`
    exists to prevent, so the payload keeps them separate AND says so.
    """
    got = _router_evidence(
        {"router": {"matched": 1, "off_script": 0, "faults": 9, "unroutable": 9}}
    )
    assert got["faults"] == 9 and got["unroutable"] == 9
    assert got["off_script"] == 0
    assert got["harness_problems_are_not_agent_deviations"] is True


def test_a_missing_block_never_fabricates_zeros_that_read_as_a_clean_run() -> None:
    """Absent counts must not be presented as measurements.

    They are present (the shape is stable) but `available` is False, so a
    player keys off that rather than rendering "0 off-script" as a verdict.
    """
    got = _router_evidence({"router": None})
    assert got["available"] is False
    assert got["off_script"] == 0  # shape is stable
    assert got["reason"], "but it is never reported as a measurement"


# --------------------------------------------------------------------------
# malformed inputs must not break the player
# --------------------------------------------------------------------------


def test_junk_types_are_tolerated_and_read_as_zero() -> None:
    for bad in ("1", True, None, [], {}):
        got = _router_evidence({"router": {"matched": bad, "decisions": "nope"}})
        assert got["available"] is True
        assert got["matched"] == 0, f"non-numeric count {bad!r} must read as 0"
        assert got["decisions"] == [], "non-list decisions must read as empty"


def test_a_boolean_count_is_not_treated_as_a_number() -> None:
    """`True` is an int in Python; a router reporting it is malformed, not 1."""
    got = _router_evidence({"router": {"matched": True}})
    assert got["matched"] == 0


def test_every_absent_shape_still_carries_the_full_key_set() -> None:
    """A player must not have to probe for keys.

    All three states return the same keys, differing only in `available` and
    `reason`, so the UI can render one shape.
    """
    shapes = [
        _router_evidence(None),
        _router_evidence({}),
        _router_evidence({"router": None}),
        _router_evidence({"router": "bad"}),
        _router_evidence({"router": {"matched": 1}}),
    ]
    expected = {
        "available", "reason", "matched", "off_script",
        "faults", "unroutable", "decisions",
        "harness_problems_are_not_agent_deviations",
    }
    for got in shapes:
        assert set(got) == expected, f"key set drifted: {set(got) ^ expected}"
