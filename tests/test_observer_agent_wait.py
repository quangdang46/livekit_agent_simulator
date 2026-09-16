"""AgentWait evidence tiers: a real agent answer must never be reported as
AGENT_TIMEOUT just because one evidence channel was missing.

Regression: run 011 (contract single-path live smoke) — the agent audibly
answered the opening `say:` (voice_ai.transcript/topic data + R-channel PCM),
but ObserverAgentWait polled only last_agent_final_*, which the Observer
never set because first_speaker=user + _user_has_spoken=False tagged the
reply transcript.agent.preamble. Result: spurious AGENT_TIMEOUT despite a
real answer.
"""

from __future__ import annotations

import time

import pytest

from livekit_agent_simulator.caller_contract.agent_wait import ObserverAgentWait


class FakeObserver:
    def __init__(self):
        self.last_agent_final_mono: float | None = None
        self.last_agent_final_text: str = ""
        self.agent_is_active_speaker = False
        self.agent_has_spoken = False


@pytest.mark.asyncio
async def test_tier1_final_transcript_wins_immediately():
    obs = FakeObserver()
    obs.last_agent_final_mono = time.monotonic()
    obs.last_agent_final_text = "Hello! How can I help?"
    wait = ObserverAgentWait(observer=obs)
    assert await wait.wait_agent_turn(timeout_s=2.0) == "Hello! How can I help?"


@pytest.mark.asyncio
async def test_tier3_audio_floor_returns_marker_not_timeout():
    """Agent provably talked (active-speaker energy) but no words were ever
    transcribed — must return the untranscribed marker, NEVER None (which
    the driver would map to AGENT_TIMEOUT)."""
    obs = FakeObserver()
    obs.agent_has_spoken = True  # e.g. active_speakers_changed fired
    wait = ObserverAgentWait(observer=obs, poll_s=0.02)
    result = await wait.wait_agent_turn(timeout_s=0.15)
    assert result == "[untranscribed agent speech]"
    assert result is not None


@pytest.mark.asyncio
async def test_true_silence_still_times_out_to_none():
    """No audio, no transcript, no session — genuine silence must still
    return None so the driver can classify AGENT_TIMEOUT correctly."""
    obs = FakeObserver()
    wait = ObserverAgentWait(observer=obs, poll_s=0.02)
    assert await wait.wait_agent_turn(timeout_s=0.1) is None


@pytest.mark.asyncio
async def test_preamble_style_final_is_still_tier1_evidence():
    """The Observer's transcript.agent.preamble fix records last_agent_final_*
    even for pre-user-speech agent turns — simulate exactly that state and
    assert the wait sees it (this is the run-011 shape)."""
    obs = FakeObserver()
    # Observer sets these even on the preamble path now; _user_has_spoken
    # is irrelevant to the wait — presence of final text IS the evidence.
    obs.last_agent_final_mono = time.monotonic()
    obs.last_agent_final_text = "Hello! I can help with that."
    wait = ObserverAgentWait(observer=obs)
    assert await wait.wait_agent_turn(timeout_s=2.0) == "Hello! I can help with that."
@pytest.mark.asyncio
async def test_same_final_never_returned_twice_run_039():
    """Run 039 regression: the agent answered turn 2 ("What make and model?")
    and the driver consumed it, then asked again (turn 3). The Observer still
    holds that same final (the agent's real turn-3 reply was interrupted and
    never finalized). wait_agent_turn must NOT return the stale final again —
    it must fall through to snapshot/audio tiers or time out, never repeat."""
    import asyncio

    obs = FakeObserver()
    obs.last_agent_final_mono = time.monotonic()
    obs.last_agent_final_text = "What make and model?"
    wait = ObserverAgentWait(observer=obs)
    first = await wait.wait_agent_turn(timeout_s=2.0)
    assert first == "What make and model?"
    # Same final still present (agent never produced a new one) — must not repeat.
    second = await wait.wait_agent_turn(timeout_s=0.2)
    assert second is None
