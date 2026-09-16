"""Slice 3b unit tests: BridgePublishSink.publish() only returns after the
mixer has actually drained; a stuck mixer raises PublishDrainTimeout (an
execution/transport failure, never CALLER_BEHAVIOR_VIOLATION — that mapping
is asserted at the driver level in test_contract_live_wiring.py).
"""

from __future__ import annotations

import time

import pytest

from livekit_agent_simulator.caller_contract import GenerationIdentity
from livekit_agent_simulator.caller_contract.driver import PublishDrainTimeout
from livekit_agent_simulator.caller_contract.orchestrator import Orchestrator
from livekit_agent_simulator.caller_contract.publish_sink import BridgePublishSink


class TimedFakeMixer:
    """Simulates real drain: speech_queued_ms() > 0 for drain_duration_s
    after a push_speech, then 0. Never drains at all when drain_duration_s
    is None (stuck mixer)."""

    def __init__(self, drain_duration_s: float | None = 0.05):
        self.pushed: list[bytes] = []
        self._drain_duration_s = drain_duration_s
        self._drain_until: float | None = None

    def push_speech(self, pcm, *, gain=1.0):
        self.pushed.append(pcm)
        if self._drain_duration_s is None:
            self._drain_until = float("inf")
        else:
            self._drain_until = time.monotonic() + self._drain_duration_s

    def end_speech_turn(self):
        pass

    def speech_queued_ms(self):
        if self._drain_until is None:
            return 0
        return 100 if time.monotonic() < self._drain_until else 0


class FakeBridge:
    def __init__(self, drain_duration_s: float | None = 0.05):
        self._mixer = TimedFakeMixer(drain_duration_s)
        self.publish_calls = 0

    def publish_validated_pcm(self, pcm, *, gain=1.0):
        self.publish_calls += 1
        if not pcm:
            return False
        self._mixer.push_speech(pcm, gain=gain)
        return True

    async def drain_persona_speech(self, *, timeout_s: float = 4.0):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._mixer.speech_queued_ms() <= 0:
                return
            import asyncio

            await asyncio.sleep(0.01)


def _identity() -> GenerationIdentity:
    return GenerationIdentity(behavior_id="b1", turn_id=1, generation_id=1)


@pytest.mark.asyncio
async def test_publish_waits_for_mixer_drain_before_returning():
    bridge = FakeBridge(drain_duration_s=0.15)
    orch = Orchestrator()
    orch.start_behavior()
    orch.advance_caller_turn()
    identity = orch.current_identity()
    sink = BridgePublishSink(bridge=bridge, orchestrator=orch, drain_timeout_s=2.0)

    started = time.monotonic()
    ok = await sink.publish(b"\x00\x01" * 100, identity, label="say:1")
    elapsed = time.monotonic() - started

    assert ok is True
    assert elapsed >= 0.15, "publish() must not return before the mixer actually drained"


@pytest.mark.asyncio
async def test_publish_raises_drain_timeout_on_stuck_mixer():
    bridge = FakeBridge(drain_duration_s=None)  # never drains
    orch = Orchestrator()
    orch.start_behavior()
    orch.advance_caller_turn()
    identity = orch.current_identity()
    sink = BridgePublishSink(bridge=bridge, orchestrator=orch, drain_timeout_s=0.05)

    with pytest.raises(PublishDrainTimeout):
        await sink.publish(b"\x00\x01" * 100, identity, label="say:1")


@pytest.mark.asyncio
async def test_stale_identity_never_reaches_bridge_publish():
    bridge = FakeBridge()
    orch = Orchestrator()
    orch.start_behavior()
    orch.advance_caller_turn()
    stale_identity = orch.current_identity()
    orch.advance_caller_turn()  # supersede — stale_identity is now stale
    sink = BridgePublishSink(bridge=bridge, orchestrator=orch)

    ok = await sink.publish(b"\x00\x01" * 100, stale_identity, label="say:1")
    assert ok is False
    assert bridge.publish_calls == 0
