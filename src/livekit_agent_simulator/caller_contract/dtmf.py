"""DTMF publishing for the simulated caller — the one wire call in the restore.

`DtmfPublisher` is the seam (so the driver can be tested without a room) and
`RoomDtmfPublisher` is the only implementation that knows LiveKit.

Two things here are not obvious from the code and are the reason this file
carries a long docstring.

The `asyncio.wait_for` wrapper is MANDATORY, not defensive
-----------------------------------------------------------
`publish_dtmf` does::

    queue = FfiClient.instance.queue.subscribe()
    resp = FfiClient.instance.request(req)
    cb = await queue.wait_for(lambda e: ...)

and LiveKit's `Queue.wait_for` (`livekit/rtc/_utils.py:103`) is::

    async def wait_for(self, fnc) -> T:
        while True:
            event = await self.get()
            if fnc(event):
                return event

There is no `timeout` parameter on that method at all — not defaulted, not
dropped. So `publish_dtmf` has no cancel path of its own, and on a dead
transport the await never returns. The wrapper below is the only bound that
exists.

What that hangs if it is missing: the driver's turn task wedges, and it wedges
*silently*. The hold-timeout path calls `bridge.sim_hang_up()`, which does not
cancel the driver task, so the `finally: bridge.stop()` in `run_orchestrator.py`
never runs and the run never finalizes. Bounded is the difference between a
failed run and a process that never comes back. The unknown-digit case does not
have this problem, which is exactly why the wrapper is easy to forget: a typo in
a scenario returns promptly and looks fine.

DTMF_CODES is LiveKit's map, NOT RFC 4733
------------------------------------------
    LiveKit:  "0"-"9" -> 0-9,  "*" -> 10, "#" -> 11
    RFC 4733:  "0"-"9" -> 0-9,  "*" -> 10, "#" -> 15

A real SIP caller pressing `#` produces code 15. `livekit/sip` ignores `code` on
the room-to-phone leg, so the divergence is invisible in an actual call — but an
agent that asserts on `SipDTMF.code` rather than `.digit` would see 11 here and
15 there. **Do not "fix" this map to match the RFC.** It is the map the wire
actually uses, and matching the RFC would make it wrong in the one place it runs.

What a `sim.script.dtmf` event proves
-------------------------------------
It proves the tones were submitted to the LOCAL participant. It does **not**
prove an agent received them. The server excludes the sender from data fan-out,
so the simulated caller structurally cannot observe its own tone landing.

The only in-repo evidence that a tone was delivered is the agent's own reaction
— see `demo/dtmf-feature/agent/dtmf_agent.py`. A scenario asserting that a DTMF
step "worked" is asserting that publishing did not raise, which is a weaker
claim than it sounds and should be worded that way in `PassCriteria`.

Error strings are prefixed with a stable category
-------------------------------------------------
`DtmfResult` is frozen with exactly three fields because the driver emits all
three into the event contract (`script/summary.py`, `script/verify.py`) and a
fourth field is a breaking change to that contract. The category is therefore a
prefix on `error` rather than a new field:

    unknown-digit:   the scenario is wrong — fix the YAML
    timeout:         the transport is wedged; the wrapper fired
    publish-failed:  the SDK raised; the room or the FFI client is unhappy

Those three lead to three different actions, and collapsing them into one
free-form string is the same undiagnosable-error shape that a bare
`except Exception: pass` produces. The prefix is stable and greppable; the text
after it is not.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover — type only, so no LiveKit import is needed
    from livekit.rtc import LocalParticipant

#: Pause character in a digit string. Not a digit: it only inserts delay, so it
#: is skipped by the code map and never reaches `publish_dtmf`.
DTMF_PAUSE_CHAR = "w"

#: How long to pause on `w`, and how long to wait after each published tone.
#: The gap is not cosmetic: it keeps consecutive tones in the same order when
#: they are published faster than the transport drains them.
DTMF_PAUSE_MS = 120
DTMF_GAP_MS = 150

#: Bound on a single `publish_dtmf`. See the module docstring — without it the
#: await has no cancel path and a dead transport hangs the driver forever.
PUBLISH_TIMEOUT_S = 5.0

#: LiveKit's SIP DTMF code map. NOT RFC 4733 (see module docstring). Do not
#: "correct" `#` to 15.
DTMF_CODES: dict[str, int] = {
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4,
    "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "*": 10, "#": 11,
}

#: What a digit string may contain. `w` is a pause, not a tone.
_ALLOWED = set(DTMF_CODES) | {DTMF_PAUSE_CHAR}


@dataclass(frozen=True)
class DtmfResult:
    """Outcome of one `publish` call.

    Three fields, exactly, because the driver emits all three into the event
    contract. Do not add a field without updating `script/summary.py` and
    `script/verify.py` in the same change.

    `published` counts tones actually submitted, so a run that fails on the
    fourth character reports 3 — the partial work is real and hiding it would
    make "it failed" indistinguishable from "it sent something wrong".
    """

    digits: str
    published: int
    error: str | None


@runtime_checkable
class DtmfPublisher(Protocol):
    """The seam the driver depends on.

    Exists so the driver can be tested against a fake, and so a future
    non-room transport (real telephony) can be added without touching the
    driver.
    """

    async def publish(self, digits: str) -> DtmfResult: ...


class RoomDtmfPublisher:
    """Publish DTMF through a LiveKit local participant.

    Takes a `LocalParticipant`, not a `Room`. Resolving the room and deciding
    whether the caller and the agent are even in the SAME room is the caller's
    job (see the room-identity gate in `live_wiring`); this class only ever
    calls `participant.publish_dtmf(code=, digit=)`. Keeping the room out of
    here means the unit tests need a one-method fake, not a room stand-in.
    """

    def __init__(self, participant: "LocalParticipant") -> None:
        self._participant = participant

    async def publish(self, digits: str) -> DtmfResult:
        """Walk `digits` left to right, publishing each tone.

        Stops at the first character it cannot send and reports why. It does
        NOT pre-validate the whole string: partial publication is real, and
        refusing to send "12" because of a typo in the third character would
        throw away work that already succeeded.
        """
        published = 0
        for ch in digits:
            if ch == DTMF_PAUSE_CHAR:
                await asyncio.sleep(DTMF_PAUSE_MS / 1000)
                continue
            code = DTMF_CODES.get(ch)
            if code is None:
                return DtmfResult(
                    digits=digits,
                    published=published,
                    error=(
                        f"unknown-digit: {ch!r} is not a DTMF char "
                        f"(allowed: {''.join(sorted(_ALLOWED))})"
                    ),
                )
            try:
                await asyncio.wait_for(
                    self._participant.publish_dtmf(code=code, digit=ch),
                    PUBLISH_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                return DtmfResult(
                    digits=digits,
                    published=published,
                    error=(
                        f"timeout: publish_dtmf({ch!r}) did not return within "
                        f"{PUBLISH_TIMEOUT_S}s — the transport is wedged"
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — every failure is a value
                return DtmfResult(
                    digits=digits,
                    published=published,
                    error=f"publish-failed: {type(exc).__name__}: {exc}",
                )
            published += 1
            await asyncio.sleep(DTMF_GAP_MS / 1000)
        return DtmfResult(digits=digits, published=published, error=None)
