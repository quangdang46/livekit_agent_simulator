"""SimLeg protocol, handle, context, errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from livekit import rtc

if TYPE_CHECKING:
    from ...config import SimConfig
    from ...logging.event_writer import EventWriter
    from ...scenario import Scenario
    from ..adapter import LiveKitAdapter


class SimLegError(Exception):
    """Raised when a transport leg cannot connect (dial fail, missing config, etc.)."""


@dataclass
class SimLegHandle:
    """Normalized connect result — later phases do not branch on mode."""

    agent_room: rtc.Room
    sim_room: rtc.Room
    agent_room_name: str
    sim_room_name: str
    sim_identity: str
    agent_identity: str
    mode: str
    # Agent audio for Gemini: identity in sim_room to subscribe (WebRTC agent or SIP bridge).
    gemini_listen_identity: str | None = None
    # Prefer any SIP audio on sim_room when True (outbound/inbound hairpin).
    gemini_listen_sip: bool = False
    # When True, also feed Gemini from agent_room WebRTC (outbound without sim SIP leg).
    gemini_listen_agent_room: bool = False
    rooms_to_delete: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    async def disconnect_rooms(self) -> None:
        for room in {id(self.agent_room): self.agent_room, id(self.sim_room): self.sim_room}.values():
            try:
                await room.disconnect()
            except Exception:
                pass


@dataclass
class SimLegContext:
    adapter: "LiveKitAdapter"
    cfg: "SimConfig"
    scenario: "Scenario"
    writer: "EventWriter"
    run_id: str
    dispatch_metadata: str | None
    first_speaker: str


class SimLeg(Protocol):
    async def connect(self, ctx: SimLegContext) -> SimLegHandle: ...
