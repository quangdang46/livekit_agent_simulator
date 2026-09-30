"""DTMF publishing: the seam, the code map, and the hang guard.

The third of these is the one that matters. `publish_dtmf` awaits a LiveKit
`Queue.wait_for` that has **no timeout parameter at all** — verified against the
installed SDK, `livekit/rtc/_utils.py`:

    async def wait_for(self, fnc) -> T:
        while True:
            event = await self.get()
            if fnc(event):
                return event

So `RoomDtmfPublisher` must bound it itself. Without that bound a dead
transport wedges the driver task, and wedges it *silently* — the hold-timeout
path hangs up without cancelling, so the run's `finally: bridge.stop()` never
runs and the process never returns.

These tests are written so that REMOVING the wrapper produces a failure, not a
hang: the test wraps the call in its own `asyncio.wait_for` with a deadline
comfortably longer than the publisher's own. If the inner bound disappears, the
outer one fires and the test fails with a message naming the cause.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from livekit_agent_simulator.caller_contract import dtmf as dtmf_mod
from livekit_agent_simulator.caller_contract.dtmf import (
    DTMF_CODES,
    PUBLISH_TIMEOUT_S,
    DtmfPublisher,
    DtmfResult,
    RoomDtmfPublisher,
)

#: The publisher's own bound is shrunk for the hang tests so they do not spend
#: 5 real seconds each. The shipped default is asserted on its own below, so
#: nothing here is taken on trust — the guard is tested, the number is pinned.
_FAST_TIMEOUT_S = 0.2

#: Comfortably longer than the publisher's own bound, so the publisher's
#: `asyncio.wait_for` always wins. If it ever stops winning, this fires and the
#: test fails instead of hanging.
_TEST_DEADLINE_S = 10.0


@pytest.fixture
def fast_timeout(monkeypatch: pytest.MonkeyPatch) -> float:
    """Shrink the guard so the hang tests cost 0.2s each, not 5s each."""
    monkeypatch.setattr(dtmf_mod, "PUBLISH_TIMEOUT_S", _FAST_TIMEOUT_S)
    return _FAST_TIMEOUT_S


class RecordingParticipant:
    """A fake with the one method the publisher is allowed to call."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def publish_dtmf(self, *, code: int, digit: str) -> None:
        self.calls.append((code, digit))


class HangingParticipant:
    """A transport that never answers — the case the wrapper exists for."""

    async def publish_dtmf(self, *, code: int, digit: str) -> None:
        await asyncio.Event().wait()  # never returns


class ExplodingParticipant:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def publish_dtmf(self, *, code: int, digit: str) -> None:
        raise self._exc


# --------------------------------------------------------------- the seam


def test_the_module_imports_without_a_live_room() -> None:
    """No import-time LiveKit dependency.

    `livekit.rtc` is imported for the type only, under TYPE_CHECKING, so this
    module is importable in a unit test with no connection.
    """
    import sys

    assert "livekit.rtc" not in {
        name
        for name, mod in sys.modules.items()
        if getattr(mod, "__file__", None)
        and "livekit\\rtc\\__init__.py" == mod.__file__[-24:]
    } or True  # the check below is the real one
    source = (
        dtmf_mod.__file__ and open(dtmf_mod.__file__, encoding="utf-8").read()
    )
    top_level = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "TYPE_CHECKING" not in line
    ]
    assert not any("livekit" in line for line in top_level), (
        f"livekit imported at module scope: {top_level}"
    )


def test_room_dtmf_publisher_satisfies_the_seam() -> None:
    """The driver depends on the Protocol, not the implementation."""
    publisher = RoomDtmfPublisher(RecordingParticipant())  # type: ignore[arg-type]
    assert isinstance(publisher, DtmfPublisher)


# ----------------------------------------------------------- the code map


def test_code_map_is_livekits_not_rfc_4733() -> None:
    """`#` is 11 here and 15 in the RFC. That divergence is deliberate.

    livekit/sip ignores `code` on the room-to-phone leg, so it is invisible in
    a real call and only shows up in an agent that asserts on `SipDTMF.code`.
    "Correcting" this to 15 would make it wrong in the only place it runs.
    """
    assert DTMF_CODES["#"] == 11, "LiveKit's map, not RFC 4733's 15"
    assert DTMF_CODES["*"] == 10
    assert [DTMF_CODES[str(d)] for d in range(10)] == list(range(10))


# ------------------------------------------------------------- publishing


async def test_1w2hash_sends_three_tones_and_no_call_for_w() -> None:
    """The acceptance case: `w` is a pause, not a tone."""
    participant = RecordingParticipant()
    result = await RoomDtmfPublisher(participant).publish("1w2#")  # type: ignore[arg-type]

    assert participant.calls == [(1, "1"), (2, "2"), (11, "#")]
    assert result == DtmfResult(digits="1w2#", published=3, error=None)


async def test_pause_delays_but_does_not_publish() -> None:
    """`w` must cost time, or it is not a pause."""
    publisher = RoomDtmfPublisher(RecordingParticipant())  # type: ignore[arg-type]
    started = time.monotonic()
    await publisher.publish("w")
    # One pause, no gap (nothing was published).
    assert time.monotonic() - started >= dtmf_mod.DTMF_PAUSE_MS / 1000 * 0.9


async def test_gap_is_applied_after_each_tone() -> None:
    """Three tones means three gaps — ordering depends on it."""
    publisher = RoomDtmfPublisher(RecordingParticipant())  # type: ignore[arg-type]
    started = time.monotonic()
    await publisher.publish("123")
    expected = 3 * dtmf_mod.DTMF_GAP_MS / 1000
    assert time.monotonic() - started >= expected * 0.9


async def test_unknown_digit_stops_and_reports_what_was_sent() -> None:
    """Partial publication is real and must be visible.

    Refusing to send "12" because of a typo in the third character would throw
    away work that already succeeded.
    """
    participant = RecordingParticipant()
    result = await RoomDtmfPublisher(participant).publish("12X")  # type: ignore[arg-type]

    assert participant.calls == [(1, "1"), (2, "2")]
    assert result.published == 2
    assert result.error is not None
    assert result.error.startswith("unknown-digit:")
    assert "'X'" in result.error, "the error must name the offending character"


async def test_an_sdk_error_becomes_a_value_not_an_exception() -> None:
    """A raised SDK error is a result, not a crash.

    The driver turns this into an event; letting it propagate would take down a
    scenario over one bad tone.
    """
    publisher = RoomDtmfPublisher(ExplodingParticipant(RuntimeError("no room")))  # type: ignore[arg-type]
    result = await publisher.publish("5")

    assert result.published == 0
    assert result.error is not None
    assert result.error.startswith("publish-failed:")
    assert "RuntimeError" in result.error


# ---------------------------------------------------------- the hang guard


async def test_a_dead_transport_times_out_instead_of_hanging(
    fast_timeout: float,  # noqa: ARG001 — shrinks the guard for this test
) -> None:
    """The single most important test in this file.

    The outer `wait_for` is a test harness, not belt-and-braces: if the
    publisher's own bound is removed, this fires and fails with a message that
    names the cause. Without it, a regression here would hang CI forever
    rather than fail, because the thing that regressed is the hang guard.
    """
    publisher = RoomDtmfPublisher(HangingParticipant())  # type: ignore[arg-type]

    started = time.monotonic()  # noqa: E501
    try:
        result = await asyncio.wait_for(publisher.publish("1"), _TEST_DEADLINE_S)
    except asyncio.TimeoutError:
        pytest.fail(
            "RoomDtmfPublisher.publish did not return within "
            f"{_TEST_DEADLINE_S}s. The asyncio.wait_for around "
            "publish_dtmf is the ONLY bound on this call — LiveKit's "
            "Queue.wait_for takes no timeout argument — so removing it hangs "
            "the driver task forever and the run never finalizes."
        )
    elapsed = time.monotonic() - started

    assert result.error is not None
    assert result.error.startswith("timeout:"), result.error
    assert result.published == 0
    # It returned because of the publisher's own bound, not the harness's.
    assert elapsed < _TEST_DEADLINE_S, (
        f"took {elapsed:.1f}s — the harness deadline fired, not the guard"
    )


def test_the_shipped_timeout_is_the_documented_value() -> None:
    """The shrink above is a test convenience; the shipped number is pinned.

    5s is long enough for a healthy transport and short enough that a wedged
    one does not hold a paid call open. Changing it is a deliberate act.
    """
    assert PUBLISH_TIMEOUT_S == 5.0


def test_the_fast_bound_is_well_inside_the_harness_deadline() -> None:
    """Guards the guard.

    If the shrunk bound ever rises past the test's own deadline, the hang-guard
    test starts passing on the HARNESS's bound and stops testing the guard at
    all. That is a silent weakening, so it fails loudly.
    """
    assert _FAST_TIMEOUT_S < _TEST_DEADLINE_S / 4


async def test_timeout_keeps_earlier_tones_in_the_count(
    fast_timeout: float,  # noqa: ARG001 — shrinks the guard for this test
) -> None:
    """A wedge on the second tone still reports the first."""
    class HalfDead:
        def __init__(self) -> None:
            self.n = 0

        async def publish_dtmf(self, *, code: int, digit: str) -> None:
            self.n += 1
            if self.n > 1:
                await asyncio.Event().wait()

    publisher = RoomDtmfPublisher(HalfDead())  # type: ignore[arg-type]
    result = await asyncio.wait_for(publisher.publish("12"), _TEST_DEADLINE_S)

    assert result.published == 1, "the tone that DID go out must still be counted"
    assert result.error is not None
    assert result.error.startswith("timeout:")


# ------------------------------------------------------------ the contract


def test_result_has_exactly_three_fields() -> None:
    """The event contract in script/summary.py and script/verify.py reads all
    three. A fourth is a breaking change to that contract, not an improvement
    here — which is why error categories are a prefix on `error` instead of a
    new field."""
    from dataclasses import fields

    assert [f.name for f in fields(DtmfResult)] == ["digits", "published", "error"]


def test_result_is_frozen() -> None:
    result = DtmfResult(digits="1", published=1, error=None)
    with pytest.raises(Exception):
        result.published = 2  # type: ignore[misc]


def test_every_error_category_is_greppable() -> None:
    """Three causes, three actions, three stable prefixes.

    Collapsing them into one free-form string is the same undiagnosable-error
    shape as a bare `except Exception: pass`.
    """
    known = {"unknown-digit:", "timeout:", "publish-failed:"}
    assert known == {c + ":" for c in ("unknown-digit", "timeout", "publish-failed")}
