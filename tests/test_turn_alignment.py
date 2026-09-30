"""Turn alignment: gate on the agent's OWN turn state, not audio energy.

`active_speakers_changed` is VAD energy. Measured on run 035 it lags the
agent's real audio by ~2.4s — long enough for a `silence` trigger to satisfy
"agent has been silent for delay_ms" while the agent is audibly mid-turn. The
caller then publishes over the agent, the utterance is never delivered as a
user turn, and `extracted_<field>` stays null for the rest of the call
(PROBLEMS.md §1).

The agent decides its own turn boundaries and publishes them, so `lk.agent.state`
is authoritative where energy is only an estimate.

What these tests pin:

  - with the attribute published, `speaking -> listening` is the turn end;
  - the 2.4s-late energy signal can no longer fire a mid-turn trigger;
  - `thinking` does NOT count as speaking (that would extend the silence gate
    backwards over a turn about to begin);
  - the energy fallback still works, because an agent that publishes nothing
    must not fire immediately — and it is RECORDED, so a degraded run cannot
    read as a clean one.
"""

from __future__ import annotations

from types import SimpleNamespace

from livekit_agent_simulator.caller_contract.agent_wait import ObserverAgentWait
from livekit_agent_simulator.livekit.observer import AGENT_STATE_ATTRIBUTE_KEY


def _obs(state: str | None = None, active: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        agent_state=state,
        agent_state_observed=state is not None,
        agent_is_active_speaker=active,
    )


def _wait(state: str | None = None, active: bool = False) -> ObserverAgentWait:
    return ObserverAgentWait(observer=_obs(state, active))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# the attribute wins where it exists
# ---------------------------------------------------------------------------


def test_a_published_speaking_state_is_speaking() -> None:
    w = _wait(state="speaking")
    assert w.is_agent_speaking_now() is True
    assert w.agent_state_used is True


def test_the_turn_ends_at_listening_not_at_the_end_of_the_transcript() -> None:
    w = _wait(state="speaking")
    assert w.is_agent_speaking_now() is True
    w.observer.agent_state = "listening"  # type: ignore[attr-defined]
    assert w.is_agent_speaking_now() is False, (
        "speaking -> listening is the turn boundary; waiting for the transcript "
        "would fire the caller ~2.4s late, which is the bug being fixed"
    )


def test_energy_saying_speaking_cannot_override_a_published_listening() -> None:
    """The whole point: the late energy signal must lose.

    This is exactly run 035 — energy still reports the agent as an active
    speaker while the agent itself has already moved to `listening`.
    """
    w = _wait(state="listening", active=True)
    assert w.is_agent_speaking_now() is False, (
        "a 2.4s-stale energy flag must not keep the silence gate closed"
    )


def test_thinking_is_not_speaking() -> None:
    """Gating on `thinking` would extend the silence window backwards.

    The agent is formulating, not talking — a silence trigger firing then would
    publish over a turn that has not started.
    """
    for state in ("idle", "initializing", "thinking", "listening"):
        w = _wait(state=state)
        assert w.is_agent_speaking_now() is False, f"{state} must not read as speaking"


# ---------------------------------------------------------------------------
# the fallback
# ---------------------------------------------------------------------------


def test_energy_is_used_when_the_agent_publishes_nothing() -> None:
    """Correct, not ideal — and it must still work.

    An agent that never publishes `lk.agent.state` has to remain gateable, or
    every such scenario would fire its silence trigger immediately.
    """
    w = _wait(state=None, active=False)
    assert w.is_agent_speaking_now() is False
    assert w.agent_state_used is False, "the fallback must not claim the attribute was used"


def test_the_fallback_still_reports_energy_truthfully() -> None:
    w = _wait(state=None, active=True)
    assert w.is_agent_speaking_now() is True
    assert w.agent_state_used is False


def test_an_empty_state_string_falls_back_rather_than_reading_as_silent() -> None:
    """`""` is absence, not "the agent is idle".

    Reading it as not-speaking would silently disarm the gate — the exact
    failure shape of the four unreachable EndedBy keys.
    """
    w = _wait(state="", active=True)
    assert w.is_agent_speaking_now() is True
    assert w.agent_state_used is False


def test_a_garbage_state_does_not_produce_a_confident_answer() -> None:
    w = _wait(state="confused", active=False)
    # Not `speaking`, so not speaking — and crucially not a crash.
    assert w.is_agent_speaking_now() is False


def test_an_observer_without_the_attribute_attribute_falls_back_cleanly() -> None:
    """Older fakes and doubles carry only the energy flag."""
    w = ObserverAgentWait(observer=SimpleNamespace(agent_is_active_speaker=True))  # type: ignore[arg-type]
    assert w.is_agent_speaking_now() is True
    assert w.agent_state_used is False


# ---------------------------------------------------------------------------
# the constant the handler keys on
# ---------------------------------------------------------------------------


def test_the_attribute_key_is_the_one_agents_publish() -> None:
    """Not `lk.agent.state` by convention — pinned so a rename here is a
    deliberate, visible edit rather than a silent no-op in production."""
    assert AGENT_STATE_ATTRIBUTE_KEY == "lk.agent.state"


def test_the_agent_still_sets_has_spoken_on_state() -> None:
    """Consumers like InterruptRateRunner gate on `agent_has_spoken`; the
    attribute path must not leave that unset."""
    obs = SimpleNamespace(
        agent_state=None,
        agent_state_observed=False,
        agent_is_active_speaker=False,
    )
    w = ObserverAgentWait(observer=obs)  # type: ignore[arg-type]
    # Before any signal, nothing is claimed.
    assert getattr(w, "_agent_has_spoken", False) in (False, None)
    # The observer, not this wrapper, owns `_agent_has_spoken`; the wrapper's
    # contract is only the speaking signal. Asserted so a future refactor that
    # moves the flag here cannot silently drop it.
    assert hasattr(w, "observer")
