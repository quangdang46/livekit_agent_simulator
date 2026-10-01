"""DTMF/IVR Script action tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from livekit_agent_simulator.script import ScriptStep, normalize_interrupt_class
from livekit_agent_simulator.script_parse import parse_script_steps

# import for tests that need actual models via module

# REMOVED (dtmf-restore 3tv.10) — three tests on `script: steps: - action: dtmf`
#
# All FOUR asserted that the INERT legacy surface parses a keypress: one on the
# digits round-trip, one on invalid-digit rejection, one on export carrying the
# digits. That surface had no executor — 665ec8c deleted the ScriptRunner that
# ran it — so every one of them was green while the verb did nothing. They are
# the same class as test_wait_and_dtmf_never_publish, which pinned the driver
# defect for the same reason.
#
# The behaviour they described is not lost, it moved to the live path:
#   - digits are walked and published by RoomDtmfPublisher
#     (caller_contract/dtmf.py), covered by
#     test_an_unknown_digit_stops_and_reports_what_was_sent in
#     tests/test_contract_dtmf.py
#   - the `w` pause is covered by
#     test_pause_delays_but_does_not_publish
#   - end-to-end tone codes are covered by
#     test_the_ivr_menu_template_drives_real_tones
#
# `script: steps: - action: dtmf` now REJECTS with a migration message rather
# than parsing into nothing — see
# test_legacy_script_dtmf_is_rejected_with_a_migration_message below.

from livekit_agent_simulator.script.models import SUPPORTED_ACTIONS


def test_dtmf_in_supported_actions():
    assert "dtmf" in SUPPORTED_ACTIONS




def test_legacy_script_dtmf_is_rejected_with_a_migration_message() -> None:
    """The verb parsed, ran, and did nothing.

    `665ec8c` deleted the ScriptRunner that executed it, so `action: dtmf` was
    left accepting input it never acted on — the same shape as the bug the DTMF
    restore existed to fix, one layer down. Rejecting is option (c) from the
    bead: deleting the verb outright would diverge from Rust, whose contract
    path projects caller steps onto this surface and legitimately accepts it.
    """
    with pytest.raises(ValueError) as exc:
        parse_script_steps(
            {"steps": [
                {"id": "d1", "trigger": "time", "delay_ms": 0,
                 "action": "dtmf", "digits": "1w2"},
            ]},
            "demo.yaml",
        )
    msg = str(exc.value)
    assert "caller_steps" in msg, msg
    assert 'dtmf: "1w2"' in msg, (
        f"the suggested replacement must carry the author's own digits: {msg}"
    )
    assert "sim.script.dtmf" in msg, msg


def test_other_legacy_script_actions_still_parse() -> None:
    """Only dtmf moved. `speak`/`wait`/`hang_up` are untouched by this change."""
    spec = {
        "steps": [
            {"id": "a", "trigger": "time", "delay_ms": 0, "say": "Hi"},
            {"id": "b", "trigger": "time", "delay_ms": 100, "action": "wait"},
        ]
    }
    steps = parse_script_steps(spec, "demo.yaml")
    assert [s.action for s in steps] == ["speak", "wait"]


def test_an_unknown_action_is_still_rejected_the_old_way() -> None:
    """The dtmf branch must not have swallowed the generic unknown-action error."""
    with pytest.raises(ValueError) as exc:
        parse_script_steps(
            {"steps": [
                {"id": "a", "trigger": "time", "delay_ms": 0, "action": "levitate"},
            ]},
            "demo.yaml",
        )
    assert "levitate" in str(exc.value) or "speak|wait" in str(exc.value)
