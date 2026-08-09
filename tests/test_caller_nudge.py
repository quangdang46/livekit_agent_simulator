"""Tests for caller nudge after agent-first scenarios."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from livekit_agent_simulator.caller_nudge import (
    AGENT_GREETED_NUDGE,
    nudge_caller_after_agent_greeting,
)
from livekit_agent_simulator.config import ObserveConfig
from livekit_agent_simulator.livekit.observer import Observer
from livekit_agent_simulator.logging.event_writer import EventWriter


def _observer(tmp_path, *, first_speaker: str = "agent") -> Observer:
    writer = EventWriter("r-test", tmp_path / "reports" / "r-test", timezone_name="UTC")
    return Observer(
        MagicMock(),
        writer,
        ObserveConfig(),
        agent_identity="agent-1",
        sim_identity="sim-1",
        first_speaker=first_speaker,
    )


@pytest.mark.asyncio
async def test_nudge_skipped_when_user_speaks_first(tmp_path) -> None:
    bridge = MagicMock()
    bridge.end_call = asyncio.Event()
    bridge.nudge_freestyle_answer = AsyncMock()
    writer = MagicMock()

    await nudge_caller_after_agent_greeting(
        _observer(tmp_path, first_speaker="user"),
        bridge,
        writer,
        first_speaker="user",
        debounce_s=0.05,
        poll_s=0.01,
    )

    bridge.nudge_freestyle_answer.assert_not_called()


@pytest.mark.asyncio
async def test_nudge_after_agent_greeting(tmp_path) -> None:
    obs = _observer(tmp_path, first_speaker="agent")
    obs.on_transcript("agent", "こんにちは", final=True, source="lk.transcription")

    bridge = MagicMock()
    bridge.end_call = asyncio.Event()
    bridge.nudge_freestyle_answer = AsyncMock()
    writer = MagicMock()

    await nudge_caller_after_agent_greeting(
        obs,
        bridge,
        writer,
        first_speaker="agent",
        debounce_s=0.05,
        poll_s=0.01,
    )

    # The nudge must be a NON-AUDIBLE activation — never spoken into the room.
    bridge.nudge_freestyle_answer.assert_awaited_once_with(
        agent_hint=AGENT_GREETED_NUDGE,
    )
    writer.emit.assert_called_once()


@pytest.mark.asyncio
async def test_nudge_never_speaks_instruction(tmp_path) -> None:
    """Regression: the nudge text must not be injected as audible caller audio."""
    obs = _observer(tmp_path, first_speaker="agent")
    obs.on_transcript("agent", "Hello", final=True, source="lk.transcription")

    bridge = MagicMock()
    bridge.end_call = asyncio.Event()
    bridge.inject_cue = AsyncMock()
    bridge.nudge_freestyle_answer = AsyncMock()
    writer = MagicMock()

    await nudge_caller_after_agent_greeting(
        obs,
        bridge,
        writer,
        first_speaker="agent",
        debounce_s=0.05,
        poll_s=0.01,
    )

    bridge.nudge_freestyle_answer.assert_awaited_once()
    bridge.inject_cue.assert_not_called()
