"""Deterministic (A) + sip-call-id / fresh-room (B) resolution for inbound SIP.

A: known room name from dispatch rule (agent_room / template).
B1: correlate via ``sip_call_id`` returned by ``CreateSIPParticipant``.
B2: hairpin-safe fallback — dial-digit room that did **not** exist before dial
    (or was created at/after dial), with agent + SIP present.

Never silently pick the first leftover ``call-+DID-*`` room.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import livekit.api as api

if TYPE_CHECKING:
    from ..adapter import LiveKitAdapter


@dataclass(frozen=True)
class InboundRoomHit:
    room: str
    agent_identity: str
    phase: str  # deterministic | sip_call_id | new_dial_room | newest_dial_room


def _room_creation_ms(room: object) -> int:
    ms = getattr(room, "creation_time_ms", None)
    if ms is not None:
        try:
            return int(ms)
        except (TypeError, ValueError):
            pass
    sec = getattr(room, "creation_time", None)
    if sec is not None:
        try:
            return int(float(sec) * 1000)
        except (TypeError, ValueError):
            pass
    return 0


def _dial_needles(dial_in: str | None) -> tuple[str, str]:
    needle = (dial_in or "").strip()
    digits = "".join(ch for ch in needle if ch.isdigit())
    return needle, digits


def _name_matches_dial(name: str, needle: str, needle_digits: str) -> bool:
    if not needle and not needle_digits:
        return False
    name_digits = name.replace("+", "").replace("_", "").replace("-", "")
    if needle and needle in name:
        return True
    if needle_digits and needle_digits in name_digits:
        return True
    return False


async def resolve_deterministic(
    adapter: LiveKitAdapter,
    *,
    room_name: str,
    dispatch_metadata: str | None = None,
    timeout_ms: int | None = None,
) -> InboundRoomHit:
    """A: join the known room and wait for the agent to appear."""
    if dispatch_metadata is not None:
        try:
            await adapter.dispatch_agent(room_name, dispatch_metadata)
        except Exception:
            pass
    _ = timeout_ms  # wait_for_agent uses adapter.cfg.livekit.agent_join_timeout_ms
    agent_identity = await adapter.wait_for_agent(room_name)
    return InboundRoomHit(room=room_name, agent_identity=agent_identity, phase="deterministic")


async def resolve_inbound_agent_room(
    adapter: LiveKitAdapter,
    *,
    sip_call_id: str | None,
    dial_in: str | None,
    exclude_rooms: set[str] | None = None,
    preexisting_rooms: set[str] | None = None,
    created_after_ms: int | None = None,
    timeout_ms: int | None = None,
    poll_ms: int = 400,
) -> InboundRoomHit:
    """Discover the agent room for an inbound hairpin dial (parallel + stale-safe).

    Prefer ``sip_call_id`` attr match. If Cloud hairpin uses a different inbound
    ``sip.callID`` than CreateSIPParticipant's id, fall back to dial-digit rooms
    that are **new since the dial** (not in ``preexisting_rooms`` / created after
    ``created_after_ms``). Never latch onto a leftover dial-digit room.
    """
    from ..adapter import AgentJoinTimeout, LiveKitAdapter as _Adapter

    exclude = set(exclude_rooms) if exclude_rooms else set()
    preexisting = set(preexisting_rooms) if preexisting_rooms else set()
    needle, needle_digits = _dial_needles(dial_in)
    sip_needle = (sip_call_id or "").strip()
    timeout = (
        timeout_ms
        if timeout_ms is not None
        else adapter.cfg.livekit.agent_join_timeout_ms
    )
    deadline = asyncio.get_event_loop().time() + max(timeout, 1) / 1000

    while asyncio.get_event_loop().time() < deadline:
        rooms = await adapter.lkapi.room.list_rooms(api.ListRoomsRequest())
        fresh_hits: list[tuple[int, str, str]] = []  # (creation_ms, name, agent_id)

        for room in rooms.rooms:
            name = getattr(room, "name", "") or ""
            if not name or name in exclude:
                continue
            creation_ms = _room_creation_ms(room)
            res = await adapter.lkapi.room.list_participants(
                api.ListParticipantsRequest(room=name)
            )
            agent_id: str | None = None
            has_sip = False
            sip_attrs_blob = ""
            for p in res.participants:
                if agent_id is None and _Adapter._is_agent_participant(p):
                    agent_id = p.identity
                if _Adapter._is_sip_participant(p):
                    has_sip = True
                    sip_attrs_blob += " " + _Adapter._any_sip_attr(p)

            if sip_needle and agent_id is not None and sip_needle in sip_attrs_blob:
                return InboundRoomHit(
                    room=name, agent_identity=agent_id, phase="sip_call_id"
                )

            if agent_id is None or not has_sip:
                continue
            if not _name_matches_dial(name, needle, needle_digits):
                continue

            is_new_name = name not in preexisting
            is_new_time = (
                created_after_ms is not None and creation_ms >= created_after_ms
            )
            if not (is_new_name or is_new_time):
                continue
            fresh_hits.append((creation_ms, name, agent_id))

        if len(fresh_hits) == 1:
            _, name, agent_id = fresh_hits[0]
            return InboundRoomHit(
                room=name, agent_identity=agent_id, phase="new_dial_room"
            )
        if len(fresh_hits) > 1:
            fresh_hits.sort(key=lambda t: t[0], reverse=True)
            _, name, agent_id = fresh_hits[0]
            return InboundRoomHit(
                room=name, agent_identity=agent_id, phase="newest_dial_room"
            )

        await asyncio.sleep(poll_ms / 1000)

    raise AgentJoinTimeout(
        f"resolve_inbound_agent_room: no fresh agent+SIP room for dial_in={dial_in!r} "
        f"sip_call_id={sip_call_id!r} within {timeout}ms "
        f"(preexisting={len(preexisting)}). "
        f"Wipe leftover call-* rooms or set Telephony.agent_room / "
        f"agent_room_name_template for deterministic inbound resolution."
    )


async def resolve_by_sip_call_id(
    adapter: LiveKitAdapter,
    *,
    sip_call_id: str,
    exclude_rooms: set[str] | None = None,
    timeout_ms: int | None = None,
    poll_ms: int = 300,
    prefer_name_substr: str | None = None,
) -> tuple[str, str]:
    """B1 only: poll until a room has agent + SIP attrs containing ``sip_call_id``.

    ``prefer_name_substr`` is accepted for API compatibility but does not weaken
    the sip_call_id requirement (no dial-digit fallback here).
    """
    from ..adapter import AgentJoinTimeout, LiveKitAdapter as _Adapter

    _ = prefer_name_substr  # reserved — do not use as a weak latch
    if not sip_call_id:
        raise ValueError("resolve_by_sip_call_id requires a non-empty sip_call_id")

    exclude = set(exclude_rooms) if exclude_rooms else set()
    timeout = (
        timeout_ms
        if timeout_ms is not None
        else adapter.cfg.livekit.agent_join_timeout_ms
    )
    deadline = asyncio.get_event_loop().time() + max(timeout, 1) / 1000

    while asyncio.get_event_loop().time() < deadline:
        rooms = await adapter.lkapi.room.list_rooms(api.ListRoomsRequest())
        for room in rooms.rooms:
            name = getattr(room, "name", "") or ""
            if not name or name in exclude:
                continue
            res = await adapter.lkapi.room.list_participants(
                api.ListParticipantsRequest(room=name)
            )
            agent_id: str | None = None
            sip_blob = ""
            for p in res.participants:
                if agent_id is None and _Adapter._is_agent_participant(p):
                    agent_id = p.identity
                if _Adapter._is_sip_participant(p):
                    sip_blob += " " + _Adapter._any_sip_attr(p)
            if agent_id is not None and sip_call_id in sip_blob:
                return name, agent_id
        await asyncio.sleep(poll_ms / 1000)

    raise AgentJoinTimeout(
        f"resolve_by_sip_call_id: no room with agent and SIP call-id "
        f"{sip_call_id!r} found within {timeout}ms. "
        f"Set Telephony.agent_room or agent_room_name_template for "
        f"deterministic inbound room resolution (parallel-safe)."
    )


def wall_time_ms() -> int:
    return int(time.time() * 1000)
