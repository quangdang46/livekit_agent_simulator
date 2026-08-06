"""Caller bridge contract + shared provider-neutral plumbing.

A *caller bridge* owns the realtime model session (Gemini Live or OpenAI
Realtime) and the LiveKit audio tracks of the simulated caller. Consumers —
``ScriptRunner``, ``nudge_caller_after_agent_greeting``,
``InterruptRateRunner``, the orchestrator's ``_conversation_loop`` — drive it
through the ``CallerBridge`` protocol below, so the two providers stay
drop-in (Strategy + factory, same as ``SimLeg`` / ``CallerPolicy``).

The Gemini bridge historically was duck-typed (consumers used ``hasattr``).
The protocol captures that implicit contract; providers may still expose
provider-specific helpers (e.g. Gemini's activity-marker internals) that live
on the concrete class only.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from livekit import rtc

if TYPE_CHECKING:
    from ..audio.local_recorder import LocalConversationRecorder
    from ..config import SimConfig
    from ..livekit.observer import Observer
    from ..logging.event_writer import EventWriter

__all__ = ["CallerBridge"]


@runtime_checkable
class CallerBridge(Protocol):
    """The caller-brain contract consumed by Script / nudge / interrupt policy.

    Both ``GeminiCallerBridge`` and ``OpenAICallerBridge`` satisfy this.
    Attribute names mirror the historic ``hasattr`` duck-typing so no consumer
    changes are required when switching providers.
    """

    # -- lifecycle ---------------------------------------------------------
    end_call: asyncio.Event
    transport_dropped: bool

    async def run(self) -> None:
        """Connect the realtime session and pump audio until ``end_call``."""
        ...

    def stop(self) -> None:
        """Request teardown (signal ``end_call``, stop mixer)."""
        ...

    def sim_hang_up(self) -> None:
        """Hard disconnect by Script (``action=hang_up``)."""
        ...

    def bind_script_pending(self, is_pending: Any) -> None:
        """Wire ``ScriptRunner.has_pending_steps`` (None = no script gate)."""
        ...

    # -- audio wiring ------------------------------------------------------
    async def publish_mic(self) -> rtc.AudioSource:
        """Publish the sim mic + start the parallel mixer."""
        ...

    def watch_agent_tracks(self, agent_identity: str) -> None:
        """Subscribe to a remote participant's audio on the sim room."""
        ...

    def watch_agent_tracks_on_room(
        self, room: rtc.Room, agent_identity: str
    ) -> None:
        """Subscribe to agent audio on a different room (SIP 2-room)."""
        ...

    def watch_sip_audio_tracks(self) -> None:
        """Subscribe to any remote SIP (hairpin) audio on sim_room."""
        ...

    # -- persona speech control --------------------------------------------
    async def inject_cue(
        self,
        text: str,
        *,
        label: str = "script",
        delivery: str = "gemini_text",
        asset: str | None = None,
        scenario_dir: Path | None = None,
        gain: float = 1.0,
        loop: bool = False,
    ) -> None:
        """Inject caller speech while the agent is talking."""
        ...

    async def inject_reground(self, *, label: str | None = None) -> None:
        """Inject the first reground MidcallCue (goal focus)."""
        ...

    async def release_after_milestone(self) -> None:
        """Post-milestone freestyle hook (no-op on some providers)."""
        ...

    async def nudge_freestyle_answer(self, agent_hint: str = "") -> None:
        """Non-text activation so the caller replies after an unanswered ask."""
        ...

    def suppress_persona_output(self, duration_ms: int) -> None:
        """Block model audio/text to the room for a scripted silence."""
        ...

    def begin_scripted_user_silence(
        self,
        duration_ms: int,
        *,
        grace_s: float = 20.0,
        mute_persona: bool = False,
    ) -> None:
        """Hold dead_call grace for a Script wait step."""
        ...

    def scripted_silence_active(self) -> bool:
        """True while scripted silence is holding / within grace."""
        ...

    def begin_script_hangup_farewell(self) -> None:
        """Allow Script goodbye TTS past suppress/mute gates."""
        ...

    def end_script_hangup_farewell(self) -> None:
        """End the Script hang-up farewell window."""
        ...

    async def drain_persona_speech(self, *, timeout_s: float = 4.0) -> None:
        """Wait for queued sim speech to leave the mic (goodbye playout)."""
        ...
