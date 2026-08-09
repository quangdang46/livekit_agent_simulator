"""Nudge the simulated caller to speak after the agent greets first."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .callers.gemini import GeminiCallerBridge
    from .livekit.observer import Observer
    from .logging.event_writer import EventWriter

# Language-neutral: persona / scenario locale already constrain speech language.
#
# NOTE: this nudge must NOT be spoken aloud. The old implementation injected
# the text as a role=user conversation item + response.create, so the OpenAI
# caller literally READ the instruction out loud ("has finished greeting you.
# Respond now in the language of your persona") — the agent then heard it as a
# real caller utterance and the live transcript diverged from the voice. The
# caller's persona brief already tells it how to respond, so the correct nudge
# is a NON-AUDIBLE "please reply to the last agent turn" activation
# (nudge_freestyle_answer), not an audible spoken line.
AGENT_GREETED_NUDGE = (
    "(The agent has finished greeting you. Respond now in the language of your persona.)"
)


async def nudge_caller_after_agent_greeting(
    observer: "Observer",
    bridge: "GeminiCallerBridge",
    writer: "EventWriter",
    *,
    first_speaker: str,
    debounce_s: float = 1.0,
    poll_s: float = 0.15,

    silent_mode: bool = False,
) -> None:
    """When first_speaker is agent, persona-only runs stall without a text bootstrap.

    Activates the caller via ``nudge_freestyle_answer`` (commit the agent audio
    + response.create) so the caller replies naturally to the greeting — the
    nudge text itself is NEVER spoken into the room.

    Silent mode (Coval): never nudge — unresponsive caller must stay mute.
    """
    if first_speaker != "agent":
        return
    if silent_mode or getattr(bridge, "_silent_mode", False) is True:
        writer.emit(
            "sim.agent_greeted_nudge_skipped",
            spec={"reason": "silent_mode"},

            source="sim",
            include_dialogue=False,
        )
        return

    nudged = False
    while not bridge.end_call.is_set():
        if nudged:
            return
        if observer.agent_has_spoken and not observer.user_has_spoken:
            await asyncio.sleep(debounce_s)
            if bridge.end_call.is_set() or observer.user_has_spoken:
                return
            try:
                # Non-audible activation: do NOT speak the nudge text into the
                # room (that leaks the instruction as fake caller audio).
                await bridge.nudge_freestyle_answer(agent_hint=AGENT_GREETED_NUDGE)
                writer.emit(
                    "sim.agent_greeted_nudge",
                    spec={"debounce_s": debounce_s, "audible": False},
                    source="sim",
                    include_dialogue=False,
                )
                nudged = True
                return
            except RuntimeError:
                await asyncio.sleep(poll_s)
                continue
        await asyncio.sleep(poll_s)
