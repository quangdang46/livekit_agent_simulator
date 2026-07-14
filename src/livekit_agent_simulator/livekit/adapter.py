"""LiveKit server-side plumbing: room create, agent dispatch, SIP dial, token, connect.

Pattern (WebRTC): create room → dispatch by `agent_name` → poll until agent joins.
Pattern (SIP): create_sip_participant via livekit-api SipService.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from livekit import api, rtc

from ..config import SimConfig

SIM_IDENTITY = "lk-sim-caller"
SIM_NAME = "Agent Simulator Caller"


class AgentJoinTimeout(Exception):
    pass


class SipParticipantTimeout(Exception):
    pass


@dataclass
class DispatchResult:
    room_name: str
    agent_identity: str
    dispatch_id: str | None


def room_name_for_run(run_id: str) -> str:
    return f"lk-sim-{run_id}"


class LiveKitAdapter:
    def __init__(self, cfg: SimConfig) -> None:
        self.cfg = cfg
        self._lkapi: api.LiveKitAPI | None = None

    async def __aenter__(self) -> "LiveKitAdapter":
        self._lkapi = api.LiveKitAPI(
            self.cfg.livekit.url, self.cfg.livekit.api_key, self.cfg.livekit.api_secret
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._lkapi is not None:
            await self._lkapi.aclose()
            self._lkapi = None

    @property
    def lkapi(self) -> api.LiveKitAPI:
        assert self._lkapi is not None, "use `async with LiveKitAdapter(cfg)`"
        return self._lkapi

    # ------------------------------------------------------------- rooms

    async def list_room_names(self) -> set[str]:
        """Snapshot current room names (inbound discover skips leftovers)."""
        rooms = await self.lkapi.room.list_rooms(api.ListRoomsRequest())
        out: set[str] = set()
        for room in rooms.rooms:
            name = getattr(room, "name", "") or ""
            if name:
                out.add(name)
        return out

    async def create_room(self, room_name: str) -> None:
        await self.lkapi.room.create_room(
            api.CreateRoomRequest(name=room_name, empty_timeout=300, max_participants=8)
        )
        if self.cfg.livekit.room_prepare_ms > 0:
            await asyncio.sleep(self.cfg.livekit.room_prepare_ms / 1000)

    async def create_room_and_dispatch(
        self, run_id: str, dispatch_metadata: str | None = None
    ) -> DispatchResult:
        room_name = room_name_for_run(run_id)
        await self.create_room(room_name)
        dispatch_id = await self.dispatch_agent(room_name, dispatch_metadata)
        return DispatchResult(
            room_name=room_name,
            agent_identity="",
            dispatch_id=dispatch_id,
        )

    async def dispatch_agent(
        self, room_name: str, dispatch_metadata: str | None = None
    ) -> str | None:
        metadata = (
            dispatch_metadata
            if dispatch_metadata is not None
            else self.cfg.livekit.dispatch_metadata
        )
        dispatch = await self.lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=self.cfg.livekit.agent_name,
                room=room_name,
                metadata=metadata or "",
            )
        )
        return getattr(dispatch, "id", None)

    # ----------------------------------------------------------- wait agent

    async def wait_for_agent(self, room_name: str, poll_ms: int = 500) -> str:
        """Poll participants until an agent participant joins. Returns its identity."""
        deadline = asyncio.get_event_loop().time() + self.cfg.livekit.agent_join_timeout_ms / 1000
        while True:
            res = await self.lkapi.room.list_participants(
                api.ListParticipantsRequest(room=room_name)
            )
            for p in res.participants:
                if self._is_agent_participant(p):
                    return p.identity
            if asyncio.get_event_loop().time() > deadline:
                raise AgentJoinTimeout(
                    f"Agent `{self.cfg.livekit.agent_name}` did not join room `{room_name}` within "
                    f"{self.cfg.livekit.agent_join_timeout_ms}ms. Is the agent process running and "
                    f"registered with that exact agent_name?"
                )
            await asyncio.sleep(poll_ms / 1000)

    async def find_agent_room(
        self,
        *,
        exclude_rooms: set[str] | None = None,
        timeout_ms: int | None = None,
        poll_ms: int = 500,
        require_sip: bool = False,
        prefer_name_substr: str | None = None,
        sip_call_id_substr: str | None = None,
    ) -> tuple[str, str]:
        """Discover a room that currently has the configured agent. Returns (room, identity).

        Strategy (A+B):
          1. [A] If ``prefer_name_substr`` matches a room name (deterministic inbound rule),
             wait for agent in that specific room (scoring, not scanning).
          2. [A] If ``sip_call_id_substr`` matches a SIP participant on an agent room (exact
             call identity — 100% parallel-safe; returned by ``CreateSIPParticipant``), pick it.
          3. [B] Otherwise fall back to first agent room (legacy, may race under --parallel).

        ``exclude_rooms``: rooms the caller already knows are wrong.
        ``require_sip``: When true, only consider rooms with at least one SIP participant
        (which is the correct agent-room for inbound hairpin; the room with the agent
        that the inbound call landed in).
        """
        exclude = exclude_rooms or set()
        timeout = timeout_ms if timeout_ms is not None else self.cfg.livekit.agent_join_timeout_ms
        deadline = asyncio.get_event_loop().time() + timeout / 1000
        needle = (prefer_name_substr or "").strip()
        needle_digits = "".join(ch for ch in needle if ch.isdigit())
        sip_needle = (sip_call_id_substr or "").strip()

        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() < deadline:
            rooms = await self.lkapi.room.list_rooms(api.ListRoomsRequest())

            # Phase 1 — sip_call_id B match (exact call identity, parallel-safe).
            if sip_needle:
                for room in rooms.rooms:
                    name = getattr(room, "name", "") or ""
                    if not name or name in exclude:
                        continue
                    res = await self.lkapi.room.list_participants(
                        api.ListParticipantsRequest(room=name)
                    )
                    agent_id: str | None = None
                    sip_blob = ""
                    for p in res.participants:
                        if agent_id is None and self._is_agent_participant(p):
                            agent_id = p.identity
                        if self._is_sip_participant(p):
                            sip_blob += " " + self._any_sip_attr(p)
                    if agent_id is not None and sip_needle in sip_blob:
                        return name, agent_id

            # Phase 2 — prefer name needle (deterministic inbound rule).
            # When sip_call_id is also set, the named room MUST carry that call-id
            # in SIP attrs — otherwise we would latch a leftover dial-digit room
            # (BUG-20260714-inbound-stale-agent-room). Use resolve_inbound_agent_room
            # for hairpin freshness when attrs do not share the outbound SCL_*.
            if needle or needle_digits:
                for room in rooms.rooms:
                    name = getattr(room, "name", "") or ""
                    if not name or name in exclude:
                        continue
                    name_digits = name.replace("+", "").replace("_", "")
                    if needle and needle not in name and (
                        not needle_digits or needle_digits not in name_digits
                    ):
                        continue
                    if not needle and needle_digits and needle_digits not in name_digits:
                        continue
                    res = await self.lkapi.room.list_participants(
                        api.ListParticipantsRequest(room=name)
                    )
                    agent_id = None
                    sip_blob = ""
                    for p in res.participants:
                        if agent_id is None and self._is_agent_participant(p):
                            agent_id = p.identity
                        if self._is_sip_participant(p):
                            sip_blob += " " + self._any_sip_attr(p)
                    if agent_id is None:
                        continue
                    if sip_needle and sip_needle not in sip_blob:
                        continue
                    return name, agent_id

            # When a sip_call_id needle was provided and neither Phase 1 nor a
            # name+sip match succeeded, do NOT fall through to "first SIP room"
            # — that is the stale-latch failure mode under leftover call-* rooms.
            if sip_needle:
                await asyncio.sleep(poll_ms / 1000)
                continue

            # Phase 3 — first agent room with SIP (if require_sip).
            if require_sip:
                candidates: list[tuple[str, str]] = []  # (name, agent_id)
                for room in rooms.rooms:
                    name = getattr(room, "name", "") or ""
                    if not name or name in exclude:
                        continue
                    res = await self.lkapi.room.list_participants(
                        api.ListParticipantsRequest(room=name)
                    )
                    agent_id = None
                    has_sip = False
                    for p in res.participants:
                        if agent_id is None and self._is_agent_participant(p):
                            agent_id = p.identity
                        if self._is_sip_participant(p):
                            has_sip = True
                    if agent_id and has_sip:
                        candidates.append((name, agent_id))
                if candidates:
                    candidates.sort(key=lambda x: x[0])
                    return candidates[0]
            else:
                # Phase 4 — absolute fallback: first room with agent (legacy).
                for room in rooms.rooms:
                    name = getattr(room, "name", "") or ""
                    if not name or name in exclude:
                        continue
                    res = await self.lkapi.room.list_participants(
                        api.ListParticipantsRequest(room=name)
                    )
                    agent_id = None
                    for p in res.participants:
                        if agent_id is None and self._is_agent_participant(p):
                            agent_id = p.identity
                    if agent_id is not None:
                        return name, agent_id

            await asyncio.sleep(poll_ms / 1000)

        extra = " with SIP participant" if require_sip else ""
        raise AgentJoinTimeout(
            f"No room with agent `{self.cfg.livekit.agent_name}`{extra} found within {timeout}ms. "
            f"Set Telephony.agent_room / telephony.agent_room_name_template for deterministic "
            f"inbound room resolution (parallel-safe)."
        )

    @staticmethod
    def _is_agent_participant(p: object) -> bool:
        kind = getattr(p, "kind", None)
        kind_name = ""
        try:
            from livekit.protocol.models import ParticipantInfo

            kind_name = ParticipantInfo.Kind.Name(kind) if kind is not None else ""
        except Exception:
            pass
        identity = getattr(p, "identity", "") or ""
        return kind_name == "AGENT" or identity.startswith("agent-")

    @staticmethod
    def _is_sip_participant(p: object) -> bool:
        kind = getattr(p, "kind", None)
        try:
            from livekit.protocol.models import ParticipantInfo

            kind_name = ParticipantInfo.Kind.Name(kind) if kind is not None else ""
            if kind_name == "SIP":
                return True
        except Exception:
            pass
        attrs = getattr(p, "attributes", None) or {}
        if isinstance(attrs, dict) and any(str(k).startswith("sip.") for k in attrs):
            return True
        return False

    @staticmethod
    def _sip_call_status(p: object) -> str | None:
        attrs = getattr(p, "attributes", None) or {}
        if isinstance(attrs, dict):
            return attrs.get("sip.callStatus") or attrs.get("sip.call_status")
        return None

    @staticmethod
    def _any_sip_attr(p: object) -> str:
        """Concatenate SIP-related participant attributes for call-id matching."""
        attrs = getattr(p, "attributes", None) or {}
        if not isinstance(attrs, dict):
            return ""
        parts: list[str] = []
        for k, v in attrs.items():
            if str(k).startswith("sip.") and v is not None:
                parts.append(str(v))
        identity = getattr(p, "identity", "") or ""
        if identity:
            parts.append(str(identity))
        return " ".join(parts)

    async def wait_for_sip_participant(
        self,
        room_name: str,
        *,
        timeout_ms: int = 30_000,
        poll_ms: int = 400,
        require_active: bool = False,
        identity_prefix: str | None = None,
    ) -> str:
        """Poll until a SIP participant is present (optionally sip.callStatus=active)."""
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        while True:
            res = await self.lkapi.room.list_participants(
                api.ListParticipantsRequest(room=room_name)
            )
            for p in res.participants:
                if not self._is_sip_participant(p):
                    continue
                identity = getattr(p, "identity", "") or ""
                if identity_prefix and not identity.startswith(identity_prefix):
                    continue
                if require_active:
                    status = self._sip_call_status(p)
                    if status and status != "active":
                        continue
                return identity
            if asyncio.get_event_loop().time() > deadline:
                raise SipParticipantTimeout(
                    f"No SIP participant in room `{room_name}` within {timeout_ms}ms"
                    + (" with sip.callStatus=active" if require_active else "")
                )
            await asyncio.sleep(poll_ms / 1000)

    # -------------------------------------------------------------- SIP

    async def create_sip_participant(
        self,
        *,
        room_name: str,
        sip_trunk_id: str,
        sip_call_to: str,
        participant_identity: str,
        participant_name: str = "SIP",
        wait_until_answered: bool = True,
        krisp_enabled: bool = False,
        timeout: float | None = None,
    ) -> Any:
        """Create an outbound SIP participant (livekit-api 1.1.1+)."""
        req = api.CreateSIPParticipantRequest(
            sip_trunk_id=sip_trunk_id,
            sip_call_to=sip_call_to,
            room_name=room_name,
            participant_identity=participant_identity,
            participant_name=participant_name,
            wait_until_answered=wait_until_answered,
            krisp_enabled=krisp_enabled,
        )
        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return await self.lkapi.sip.create_sip_participant(req, **kwargs)

    # -------------------------------------------------------------- room admin (mute / isolate)

    async def get_participant(self, room_name: str, identity: str) -> Any:
        return await self.lkapi.room.get_participant(
            api.RoomParticipantIdentity(room=room_name, identity=identity)
        )

    async def mute_published_track(
        self,
        *,
        room_name: str,
        identity: str,
        track_sid: str,
        muted: bool = True,
    ) -> Any:
        return await self.lkapi.room.mute_published_track(
            api.MuteRoomTrackRequest(
                room=room_name,
                identity=identity,
                track_sid=track_sid,
                muted=muted,
            )
        )

    async def update_subscriptions(
        self,
        *,
        room_name: str,
        identity: str,
        track_sids: list[str],
        subscribe: bool,
    ) -> Any:
        if not track_sids:
            return None
        return await self.lkapi.room.update_subscriptions(
            api.UpdateSubscriptionsRequest(
                room=room_name,
                identity=identity,
                track_sids=track_sids,
                subscribe=subscribe,
            )
        )

    async def update_participant_permission(
        self,
        *,
        room_name: str,
        identity: str,
        can_subscribe: bool,
        can_publish: bool = True,
        can_publish_data: bool = True,
    ) -> Any:
        """Permissions replace atomically — set all flags you intend to keep."""
        return await self.lkapi.room.update_participant(
            api.UpdateParticipantRequest(
                room=room_name,
                identity=identity,
                permission=api.ParticipantPermission(
                    can_subscribe=can_subscribe,
                    can_publish=can_publish,
                    can_publish_data=can_publish_data,
                ),
            )
        )

    async def remove_participant(self, room_name: str, identity: str) -> Any:
        return await self.lkapi.room.remove_participant(
            api.RoomParticipantIdentity(room=room_name, identity=identity)
        )

    async def isolate_sip_handset(
        self,
        *,
        room_name: str,
        sip_identity: str,
        isolation: str = "mute_and_unsubscribe",
    ) -> dict[str, Any]:
        """Reduce handset audio after human answer (outbound_human_pickup).

        ``mute_and_unsubscribe`` (default): mute SIP uplink + deny subscribe so the
        handset does not hear agent/Gemini. ``remove`` kicks the SIP leg (risky).
        """
        isolation = (isolation or "mute_and_unsubscribe").strip().lower()
        result: dict[str, Any] = {
            "isolation": isolation,
            "muted_track_sids": [],
            "unsubscribed_track_sids": [],
            "removed": False,
        }
        if isolation == "none":
            return result

        if isolation == "remove":
            await self.remove_participant(room_name, sip_identity)
            result["removed"] = True
            return result

        # mute uplink
        if isolation in ("mute_uplink", "mute_and_unsubscribe"):
            try:
                p = await self.get_participant(room_name, sip_identity)
            except Exception as e:
                result["mute_error"] = f"{type(e).__name__}: {e}"
                p = None
            if p is not None:
                for t in getattr(p, "tracks", None) or []:
                    if getattr(t, "type", None) != api.TrackType.AUDIO:
                        continue
                    sid = getattr(t, "sid", None)
                    if not sid:
                        continue
                    try:
                        await self.mute_published_track(
                            room_name=room_name,
                            identity=sip_identity,
                            track_sid=sid,
                            muted=True,
                        )
                        result["muted_track_sids"].append(sid)
                    except Exception as e:
                        result["mute_error"] = f"{type(e).__name__}: {e}"

        if isolation == "mute_and_unsubscribe":
            # Block handset from hearing anyone (future publishes included).
            try:
                await self.update_participant_permission(
                    room_name=room_name,
                    identity=sip_identity,
                    can_subscribe=False,
                    can_publish=True,
                    can_publish_data=True,
                )
                result["can_subscribe"] = False
            except Exception as e:
                result["permission_error"] = f"{type(e).__name__}: {e}"

            # Also unsubscribe from tracks already subscribed.
            try:
                res = await self.lkapi.room.list_participants(
                    api.ListParticipantsRequest(room=room_name)
                )
                block: list[str] = []
                for other in res.participants:
                    oid = getattr(other, "identity", "") or ""
                    if oid == sip_identity:
                        continue
                    for t in getattr(other, "tracks", None) or []:
                        if getattr(t, "type", None) == api.TrackType.AUDIO and getattr(t, "sid", None):
                            block.append(t.sid)
                if block:
                    await self.update_subscriptions(
                        room_name=room_name,
                        identity=sip_identity,
                        track_sids=block,
                        subscribe=False,
                    )
                    result["unsubscribed_track_sids"] = block
            except Exception as e:
                result["unsubscribe_error"] = f"{type(e).__name__}: {e}"

        return result

    # -------------------------------------------------------------- connect

    def build_token(
        self,
        room_name: str,
        *,
        identity: str = SIM_IDENTITY,
        name: str = SIM_NAME,
    ) -> str:
        return (
            api.AccessToken(self.cfg.livekit.api_key, self.cfg.livekit.api_secret)
            .with_identity(identity)
            .with_name(name)
            .with_grants(api.VideoGrants(room_join=True, room=room_name))
            .to_jwt()
        )

    async def connect_simulator(self, room_name: str) -> rtc.Room:
        return await self.connect_participant(room_name, identity=SIM_IDENTITY, name=SIM_NAME)

    async def connect_participant(
        self,
        room_name: str,
        *,
        identity: str,
        name: str,
    ) -> rtc.Room:
        room = rtc.Room()
        token = self.build_token(room_name, identity=identity, name=name)
        await room.connect(
            self.cfg.livekit.url,
            token,
            options=rtc.RoomOptions(auto_subscribe=True),
        )
        return room

    # -------------------------------------------------------------- cleanup

    async def delete_room(self, room_name: str) -> None:
        try:
            await self.lkapi.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception:
            pass
