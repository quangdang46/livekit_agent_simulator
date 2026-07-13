"""Deterministic (A) + sip-call-id (B) room resolution for inbound SIP.

A: known room name from dispatch rule (agent_room / template).
B: correlate via ``sip_call_id`` returned by ``CreateSIPParticipant``.

Both are parallel-safe. The legacy "first agent room" fallback is
deleted from the contract — use A or B in explicit configuration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import livekit.api as api

if TYPE_CHECKING:
    from ...adapter import LiveKitAdapter


async def resolve_deterministic(
    adapter: LiveKitAdapter,
    *,
    room_name: str,
    dispatch_metadata: str | None = None,
    timeout_ms: int | None = None,
) -> tuple[str, str]:
    """A: join the known room and wait for the agent to appear.

    Typically used when the LiveKit inbound dispatch rule has
    a deterministic room name (Direct or Callee-rule template).
    """
    if dispatch_metadata is not None:
        try:
            await adapter.dispatch_agent(room_name, dispatch_metadata)
        except Exception:
            pass
    agent_identity = await adapter.wait_for_agent(room_name, timeout_ms=timeout_ms)
    return room_name, agent_identity


async def resolve_by_sip_call_id(
    adapter: LiveKitAdapter,
    *,
    sip_call_id: str,
    exclude_rooms: set[str] | None = None,
    timeout_ms: int | None = None,
    poll_ms: int = 300,
    prefer_name_substr: str | None = None,
) -> tuple[str, str]:
    """B: discover the agent room whose SIP participant attr matches ``sip_call_id``.

    ``CreateSIPParticipant`` returns ``sip_call_id`` (e.g. ``SCL_...``).
    The LiveKit SIP service sets participant attribute ``sip.callID``.
    This function polls rooms until a SIP participant with the given
    call-id appears and an agent is present in the same room.
    """
    from .adapter import AgentJoinTimeout  # avoid circular import pattern

    if not sip_call_id:
        raise ValueError("resolve_by_sip_call_id requires a non-empty sip_call_id")

    exclude = set(exclude_rooms) if exclude_rooms else set()
    needle = (prefer_name_substr or "").strip()
    deadline = adapter.lkapi._session.timeout or timeout_ms or 30_000
    import asyncio

    t0 = asyncio.get_event_loop().time()
    end = t0 + deadline / 1000 if isinstance(deadline, (int, float)) else t0 + 30

    while asyncio.get_event_loop().time() < end:
        rooms = await adapter.lkapi.room.list_rooms(api.ListRoomsRequest())
        for room in rooms.rooms:
            name = getattr(room, "name", "") or ""
            if not name or name in exclude:
                continue
            res = await adapter.lkapi.room.list_participants(
                api.ListParticipantsRequest(room=name)
            )
            agent_id: str | None = None
            for p in res.participants:
                if agent_id is None and LiveKitAdapter._is_agent_participant(p):
                    agent_id = p.identity
                sip_attrs = LiveKitAdapter._any_sip_attr(p)
                if sip_call_id in sip_attrs:
                    if agent_id is not None:
                        return name, agent_id
        await asyncio.sleep(poll_ms / 1000)

    raise AgentJoinTimeout(
        f"resolve_by_sip_call_id: no room with agent and SIP call-id "
        f"{sip_call_id!r} found within {deadline}ms. "
        f"Set Telephony.agent_room or agent_room_name_template for "
        f"deterministic inbound room resolution (parallel-safe)."
    )
