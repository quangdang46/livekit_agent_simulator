"""WebRTC single-room SimLeg."""

from __future__ import annotations

from ..adapter import SIM_IDENTITY
from .protocol import SimLegContext, SimLegHandle

class WebRtcSimLeg:
    """Single-room: Gemini WebRTC + agent (default / backward compatible)."""

    async def connect(self, ctx: SimLegContext) -> SimLegHandle:
        adapter, writer = ctx.adapter, ctx.writer
        dispatch = await adapter.create_room_and_dispatch(ctx.run_id, ctx.dispatch_metadata)
        writer.emit(
            "dispatch.created",
            spec={
                "room": dispatch.room_name,
                "agent_name": ctx.cfg.livekit.agent_name,
                "dispatch_id": dispatch.dispatch_id,
                "metadata_set": bool(ctx.dispatch_metadata),
                "mode": "webrtc_sim",
            },
            include_dialogue=False,
        )
        agent_identity = await adapter.wait_for_agent(dispatch.room_name)
        writer.emit(
            "dispatch.agent_joined",
            spec={"identity": agent_identity, "mode": "webrtc_sim"},
            include_dialogue=False,
        )
        room = await adapter.connect_simulator(dispatch.room_name)
        writer.emit(
            "sim.connected",
            spec={"identity": SIM_IDENTITY, "room": dispatch.room_name, "mode": "webrtc_sim"},
            include_dialogue=False,
        )
        return SimLegHandle(
            agent_room=room,
            sim_room=room,
            agent_room_name=dispatch.room_name,
            sim_room_name=dispatch.room_name,
            sim_identity=SIM_IDENTITY,
            agent_identity=agent_identity,
            mode="webrtc_sim",
            gemini_listen_identity=agent_identity,
            gemini_listen_sip=False,
            rooms_to_delete=[dispatch.room_name],
            meta={"dispatch_id": dispatch.dispatch_id},
        )

