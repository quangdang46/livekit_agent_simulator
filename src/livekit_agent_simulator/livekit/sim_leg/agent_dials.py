"""Pattern B agent_dials SimLeg — agent places the outbound SIP call."""

from __future__ import annotations

from ..adapter import SIM_IDENTITY, room_name_for_run
from .protocol import SimLegContext, SimLegHandle

class AgentDialsSimLeg:
    """Dispatch only; wait for SIP participant the agent creates (cooperative agent)."""

    async def connect(self, ctx: SimLegContext) -> SimLegHandle:
        from ...scenario import effective_telephony

        tel = effective_telephony(ctx.scenario, ctx.cfg)
        adapter, writer = ctx.adapter, ctx.writer

        # Optional sim-room if call_to / sim inbound is provisioned for Gemini answer.
        sim_room_name = f"lk-sim-sip-{ctx.run_id}"
        agent_room_name = room_name_for_run(ctx.run_id)
        await adapter.create_room(sim_room_name)
        await adapter.create_room(agent_room_name)

        sim_room = await adapter.connect_participant(
            sim_room_name, identity=SIM_IDENTITY, name="Agent Simulator Caller"
        )
        writer.emit(
            "sim.connected",
            spec={"identity": SIM_IDENTITY, "room": sim_room_name, "mode": "agent_dials"},
            include_dialogue=False,
        )

        dispatch_id = await adapter.dispatch_agent(agent_room_name, ctx.dispatch_metadata)
        agent_identity = await adapter.wait_for_agent(agent_room_name)
        writer.emit(
            "dispatch.agent_joined",
            spec={"identity": agent_identity, "mode": "agent_dials", "dispatch_id": dispatch_id},
            include_dialogue=False,
        )
        writer.emit(
            "outbound.wait_agent_dial",
            spec={"room": agent_room_name, "note": "waiting for agent to create SIP participant"},
            include_dialogue=False,
        )

        sip_id = await adapter.wait_for_sip_participant(
            agent_room_name,
            timeout_ms=max(60_000, ctx.cfg.livekit.agent_join_timeout_ms),
            require_active=True,
        )
        writer.emit(
            "sip.participant_connected",
            spec={"identity": sip_id, "room": agent_room_name, "role": "agent_dials"},
            include_dialogue=False,
        )
        writer.emit(
            "outbound.dial_answered",
            spec={"participant_identity": sip_id, "mode": "agent_dials"},
            include_dialogue=False,
        )

        agent_room = await adapter.connect_participant(
            agent_room_name,
            identity=f"lk-sim-obs-{ctx.run_id[:8]}",
            name="Agent Simulator Observer",
        )
        return SimLegHandle(
            agent_room=agent_room,
            sim_room=sim_room,
            agent_room_name=agent_room_name,
            sim_room_name=sim_room_name,
            sim_identity=sip_id,
            agent_identity=agent_identity,
            mode="agent_dials",
            gemini_listen_sip=True,
            rooms_to_delete=[agent_room_name, sim_room_name],
            meta={"dispatch_id": dispatch_id, "call_to": tel.call_to},
        )
