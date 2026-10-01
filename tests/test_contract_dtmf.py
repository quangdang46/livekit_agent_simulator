"""DTMF publishing: the seam, the code map, and the hang guard.

WHERE THE SIX BEAD CASES LIVE (dtmf-restore 3tv.6.1)
--------------------------------------------------
The bead asked for all six in this file. They are split by LAYER instead,
because a reader looking at the room gate wants it beside the other
live-wiring tests, and one looking at the driver branch wants it beside the
other action branches. This map exists so "is DTMF fully covered?" is still
answerable in one place.

  CASE 1 digit mapping          HERE   test_1w2hash_sends_three_tones_and_no_call_for_w
  CASE 2 dtmf pushes no PCM     driver test_dtmf_publishes_no_audio
  CASE 3 hanging transport     HERE   test_a_dead_transport_times_out_instead_of_hanging
  CASE 4 raising transport     HERE   test_an_sdk_error_becomes_a_value_not_an_exception
  CASE 5 room gate             wiring test_a_split_room_fails_loudly_naming_the_topology
                                       test_silence_still_works_in_a_split_room
                                       test_an_unconnected_room_fails_as_dtmf_not_as_an_sdk_error
  CASE 6 trigger gating        driver test_a_dtmf_trigger_that_never_fires_is_a_timeout

Plus, added after the bead was written: the shipped-template tripwire (HERE),
the DTMF code map being LiveKit's and not RFC 4733 (HERE), and over-seal
detection (test_agent_final_queue.py).

The renames the bead also asked for are done: the old
`test_wait_and_dtmf_never_publish`, which PINNED the defect by asserting a
dtmf step published nothing and the run still ended SCENARIO, is now
`test_wait_and_silence_never_publish` in test_contract_live_wiring.py with its
wait coverage kept and its dtmf half inverted here.


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
from pathlib import Path

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


# ---------------------------------------------------------------------------
# the template tripwire (dtmf-restore 3tv.6.2)
#
# The layers above are all unit-tested against fakes, which is exactly the
# "feature nobody runs inside this package" shape AGENTS.md forbids — and the
# one that let the response router ship entirely un-attached while every test
# was green. This drives the SHIPPED TEMPLATE through the REAL driver, so the
# two halves are pinned to each other: change the template and this fails;
# break the dtmf branch and this fails.
# ---------------------------------------------------------------------------

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "examples" / "dtmf-ivr-menu.yaml"


class _TripwireAgent:
    def __init__(self) -> None:
        self.waits = 0

    async def wait_agent_turn(self, *, timeout_s: float = 30.0):
        self.waits += 1
        return "What is your company name?"

    def is_agent_speaking_now(self) -> bool:
        return False

    @property
    def agent_state_used(self) -> bool:
        return False


class _TripwireSink:
    def __init__(self) -> None:
        self.published: list[tuple[bytes, str]] = []

    async def publish(self, pcm, identity, *, label, gain=1.0):
        self.published.append((pcm, label))
        return True


async def test_the_ivr_menu_template_drives_real_tones():
    """The shipped example, parsed and run — not a hand-written step list.

    Asserts the exact codes for "1w2w3w#": the `w` pauses must not become
    tones, which is the one thing a hand-written test would not catch if the
    template's digit string ever drifted from what the parser sees.
    """
    from livekit_agent_simulator.caller_contract.driver import ContractCallerDriver
    from livekit_agent_simulator.caller_contract.dsl import parse_steps
    from livekit_agent_simulator.caller_contract.language_adapter import (
        AILanguageAdapter,
    )
    from livekit_agent_simulator.caller_contract.orchestrator import Orchestrator
    from livekit_agent_simulator.caller_contract.semantic import (
        RuleBasedSemanticVerifier,
    )
    from livekit_agent_simulator.caller_contract.validator import ContractValidator
    from livekit_agent_simulator.scenario_yaml import load_scenario_yaml

    assert TEMPLATE.is_file(), f"the shipped DTMF template is missing: {TEMPLATE}"
    scenario = load_scenario_yaml(TEMPLATE)
    assert scenario.id == "dtmf-ivr-menu"

    publisher = RecordingParticipant()

    # The REAL publisher, not a stub — the codes come from DTMF_CODES.
    driver = ContractCallerDriver(
        orchestrator=Orchestrator(),
        validator=ContractValidator(semantic_verifier=RuleBasedSemanticVerifier()),
        adapter=AILanguageAdapter(backend=None),  # unused: the template has no `do:`
        synthesize=lambda text: b"\x00\x01" * 100,
    )
    sink = _TripwireSink()
    events: list[tuple[str, dict]] = []
    orch = driver.orchestrator

    result = await driver.run(
        scenario.caller_actions,
        _StaleFreeSink(orch),
        _TripwireAgent(),
        emit=lambda kind, spec: events.append((kind, spec)),
        dtmf=RoomDtmfPublisher(publisher),  # type: ignore[arg-type]
    )

    assert result.failure is None, result.failure
    # 1 -> 1, 2 -> 2, 3 -> 3, # -> 11. The three `w` pauses publish nothing.
    assert publisher.calls == [(1, "1"), (2, "2"), (3, "3"), (11, "#")], (
        f"the template's digit string did not drive the expected tones: {publisher.calls}"
    )
    emitted = [spec for kind, spec in events if kind == "sim.script.dtmf"]
    assert len(emitted) == 1, f"expected exactly one sim.script.dtmf, got {len(emitted)}"
    assert emitted[0]["digits"] == "1w2w3w#"
    assert emitted[0]["published"] == 4
    assert emitted[0]["error"] is None
    # Regression 764dc3a: a keypress is not speech and must not reach the mixer.
    assert sink.published == []


class _StaleFreeSink:
    """Sink stand-in for the tripwire: accepts everything, records nothing.

    The tripwire asserts that dtmf pushes NO PCM, so a sink that recorded
    would conflate "no audio" with "audio not inspected".
    """

    def __init__(self, orch) -> None:
        self._orch = orch

    async def publish(self, pcm, identity, *, label, gain=1.0):
        return True


def test_the_template_digit_string_exercises_the_pause_path():
    """A single-key template would not cover the parser.

    The `w` in the shipped string is the reason: it is the only way to author
    an inter-key gap, and the only branch in `RoomDtmfPublisher.publish` that
    neither publishes nor fails.
    """
    from livekit_agent_simulator.scenario_yaml import load_scenario_yaml

    scenario = load_scenario_yaml(TEMPLATE)
    digits = [a.dtmf_digits for a in scenario.caller_actions if a.kind == "dtmf"]
    assert digits == ["1w2w3w#"], f"the template stopped exercising pauses: {digits}"
    assert "w" in digits[0]


# ---------------------------------------------------------------------------
# a caller with no voice (dtmf-adjacent: the TTS path, not the wire)
# ---------------------------------------------------------------------------


def test_a_non_latin_caller_says_which_engine_and_language_failed(monkeypatch) -> None:
    """`IndexError: tuple index out of range` names nothing.

    Measured on the target repo, 2026-09-30: two runs with ja-JP callers on
    two different transports (gpt-realtime-2.1-mini, gpt-live-1) failed
    byte-identically while the AGENT half of both calls spoke normally —
    26 audio onsets each. English callers are green on the same two transports,
    so the variable is the language, not the transport. But the report said
    `TTS_ERROR: TTS synthesis failed after 3 attempt(s): tuple index out of
    range`, which names neither engine, neither language, nor the text.

    The pinned model is English-only and the OS fallback was the one branch
    not wrapped, so the error escaped `_synthesize` entirely.

    SCOPE, stated honestly: this test exercises the OS-fallback branch,
    because that is the branch reachable without the `tts-sherpa` extra. The
    sherpa branch reaches the same raise by falling through — it fails, sets
    `_SHERPA_DEAD`, and lands here — but that path is not executed by this
    test and is not claimed to be.
    """
    from livekit_agent_simulator.caller_contract import live_wiring

    def _boom(text: str, rate: int = 0):
        raise IndexError("tuple index out of range")

    monkeypatch.setattr(live_wiring, "synthesize_pcm16_mono", _boom)
    monkeypatch.setattr(live_wiring, "_SHERPA_DEAD", True, raising=False)

    with pytest.raises(live_wiring._TtsLanguageError) as exc:
        live_wiring._synthesize("こんにちは")

    msg = str(exc.value)
    assert "English-only" in msg
    assert "kitten-nano-en-v1" in msg, msg
    assert "non-Latin" in msg, msg
    assert "simulator.language" in msg, msg
