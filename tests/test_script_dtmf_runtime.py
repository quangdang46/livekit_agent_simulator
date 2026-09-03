"""Script runtime DTMF action tests.

Regression for the `_fire()` control-flow bug where a `dtmf` step fell into the
speak (`else`) branch: `kind` was clobbered from `sim.script.dtmf` to
`sim.script.cue` and the `[DTMF: N]` text was injected as speech, so no tone
was ever observed by the agent-under-test. The expected behavior (parity with
the Rust port, crates/lks-livekit/src/script.rs) is: publish the DTMF tone,
emit `sim.script.dtmf`, and inject no text.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from livekit_agent_simulator.script.models import ScriptStep
from livekit_agent_simulator.script.runtime import ScriptRunner


class _FakeLocalParticipant:
    def __init__(self) -> None:
        self.publish_dtmf_calls: list[tuple[int, str]] = []

    async def publish_dtmf(self, *, code: int, digit: str) -> None:
        self.publish_dtmf_calls.append((code, digit))


class _FakeRoom:
    def __init__(self) -> None:
        self.local_participant = _FakeLocalParticipant()


class _FakeBridge:
    def __init__(self) -> None:
        self.room = _FakeRoom()
        self.injected: list[str] = []
        self.end_call = asyncio.Event()

    async def inject_cue(self, text: str, **kwargs: Any) -> None:
        self.injected.append(str(text))

    def sim_hang_up(self) -> None:
        self.end_call.set()


def _dtmf_step(digits: str = "4") -> ScriptStep:
    return ScriptStep(
        id="press-4",
        trigger="time",
        delay_ms=0,
        action="dtmf",
        digits=digits,
        once=True,
    )


def _runner(steps: list[ScriptStep], bridge: _FakeBridge) -> ScriptRunner:
    observer = MagicMock()
    observer.agent_is_active_speaker = False
    observer.agent_has_spoken = True
    observer.agent_active_duration_ms.return_value = 0
    writer = MagicMock()
    return ScriptRunner(steps, observer, bridge, writer)  # type: ignore[arg-type]


def _emitted_kinds(writer: MagicMock) -> list[str]:
    kinds: list[str] = []
    for call in writer.emit.call_args_list:
        args = call.args
        if args:
            kinds.append(str(args[0]))
    return kinds


def _emitted_spec(writer: MagicMock, kind: str) -> dict | None:
    for call in writer.emit.call_args_list:
        args = call.args
        call_kind = args[0] if args else call.kwargs.get("kind")
        if call_kind is not None and str(call_kind) == kind:
            if "spec" in call.kwargs:
                return call.kwargs["spec"]
            if len(args) > 1:
                return args[1]
    return None


@pytest.mark.asyncio
async def test_dtmf_publishes_tone_and_emits_sim_script_dtmf() -> None:
    """dtmf step: publish tone, emit sim.script.dtmf, and inject no text."""
    bridge = _FakeBridge()
    step = _dtmf_step("4")
    runner = _runner([step], bridge)

    await runner._fire(step, waited_ms=0)

    assert bridge.room.local_participant.publish_dtmf_calls == [(4, "4")]
    kinds = _emitted_kinds(runner.writer)
    assert "sim.script.dtmf" in kinds
    assert "sim.script.cue" not in kinds
    assert bridge.injected == []


@pytest.mark.asyncio
async def test_dtmf_emit_spec_carries_digits_and_action() -> None:
    """The sim.script.dtmf event spec carries digits + action, not the text path."""
    bridge = _FakeBridge()
    step = _dtmf_step("1234#")
    runner = _runner([step], bridge)

    await runner._fire(step, waited_ms=0)

    spec = _emitted_spec(runner.writer, "sim.script.dtmf")
    assert spec is not None
    assert spec.get("action") == "dtmf"
    assert spec.get("digits") == "1234#"
    # DTMF steps must not carry a text delivery / asset (they are tones, not lines).
    assert spec.get("delivery") is None
    assert spec.get("asset") is None
    assert bridge.injected == []


@pytest.mark.asyncio
async def test_dtmf_sequence_publishes_each_digit() -> None:
    """A multi-digit string publishes each mapped tone in order (w = pause)."""
    bridge = _FakeBridge()
    step = _dtmf_step("1w2#")
    runner = _runner([step], bridge)

    await runner._fire(step, waited_ms=0)

    # 'w' is a pause (no tone); 1→1, 2→2, #→11 per the DMAP in runtime.py.
    assert bridge.room.local_participant.publish_dtmf_calls == [(1, "1"), (2, "2"), (11, "#")]


@pytest.mark.asyncio
async def test_dtmf_does_not_fall_into_speak_inject() -> None:
    """Regression: a dtmf step must not inject its '[DTMF: N]' say as speech.

    The parser auto-populates say to "[DTMF: N]" for dtmf steps; the runtime
    must not speak it (that is the bug: the text was injected and masked the
    real tone).
    """
    bridge = _FakeBridge()
    step = ScriptStep(
        id="press-4",
        trigger="time",
        delay_ms=0,
        action="dtmf",
        digits="4",
        say="[DTMF: 4]",
        once=True,
    )
    runner = _runner([step], bridge)

    await runner._fire(step, waited_ms=0)

    assert bridge.injected == []
    assert "sim.script.cue" not in _emitted_kinds(runner.writer)
