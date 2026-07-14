"""Outbound sim-callee SimLeg — Gemini answers as callee (Cloud hairpin)."""

from __future__ import annotations

import asyncio
import time

from ..adapter import SIM_IDENTITY, room_name_for_run
from .errors import sip_error_spec
from .protocol import SimLegContext, SimLegError, SimLegHandle

MODE = "outbound_sim_callee"


class OutboundSimCalleeSimLeg:
    """Agent-room dials ``call_to`` (sim DID); Gemini answers on sim-room (Cloud hairpin)."""

    async def connect(self, ctx: SimLegContext) -> SimLegHandle:
        from ...scenario import effective_telephony

        tel = effective_telephony(ctx.scenario, ctx.cfg)
        if not tel.outbound_trunk_id:
            raise SimLegError(
                f"{MODE} requires telephony.outbound_trunk_id "
                "(config) or Telephony.sip_trunk_id (scenario)."
            )
        if not tel.call_to:
            raise SimLegError(
                f"{MODE} requires Telephony.call_to or config telephony.sim_inbound_number "
                "(DID/number Gemini answers)."
            )

        adapter, writer = ctx.adapter, ctx.writer
        sim_room_name = f"lk-sim-sip-{ctx.run_id}"
        agent_room_name = room_name_for_run(ctx.run_id)

        await adapter.create_room(sim_room_name)
        await adapter.create_room(agent_room_name)
        writer.emit(
            "dispatch.created",
            spec={
                "room": agent_room_name,
                "sim_room": sim_room_name,
                "agent_name": ctx.cfg.livekit.agent_name,
                "mode": MODE,
                "metadata_set": bool(ctx.dispatch_metadata),
            },
            include_dialogue=False,
        )

        # Gemini joins sim-room first so it is ready when the SIP leg lands.
        sim_room = await adapter.connect_participant(
            sim_room_name, identity=SIM_IDENTITY, name="Agent Simulator Caller"
        )
        writer.emit(
            "sim.connected",
            spec={"identity": SIM_IDENTITY, "room": sim_room_name, "mode": MODE},
            include_dialogue=False,
        )

        dispatch_id = await adapter.dispatch_agent(agent_room_name, ctx.dispatch_metadata)
        agent_identity = await adapter.wait_for_agent(agent_room_name)
        writer.emit(
            "dispatch.agent_joined",
            spec={
                "identity": agent_identity,
                "mode": MODE,
                "dispatch_id": dispatch_id,
            },
            include_dialogue=False,
        )

        prepare_ms = tel.prepare_ms
        if prepare_ms > 0:
            writer.emit(
                "outbound.prepare",
                spec={"prepare_ms": prepare_ms},
                include_dialogue=False,
            )
            await asyncio.sleep(prepare_ms / 1000)

        # Join agent-room as observer *before* dial so we subscribe to agent audio
        # from the first greeting. LiveKit does not buffer — late join misses PCM
        # (see livekit#2827 / agents#5932: subscriber must be ready before publisher).
        agent_room = await adapter.connect_participant(
            agent_room_name,
            identity=f"lk-sim-obs-{ctx.run_id[:8]}",
            name="Agent Simulator Observer",
        )
        writer.emit(
            "sim.observer_joined",
            spec={
                "room": agent_room_name,
                "mode": MODE,
                "note": "observer joined before dial to capture agent greeting",
            },
            include_dialogue=False,
        )
        await asyncio.sleep(0.25)

        sip_identity = f"sip-out-{ctx.run_id[:12]}"
        writer.emit(
            "outbound.dial_started",
            spec={
                "call_to": tel.call_to,
                "trunk_id_set": True,
                "room": agent_room_name,
                "participant_identity": sip_identity,
                "wait_until_answered": tel.wait_until_answered,
                "mode": MODE,
            },
            include_dialogue=False,
        )
        t0 = time.monotonic()
        try:
            sip_info = await adapter.create_sip_participant(
                room_name=agent_room_name,
                sip_trunk_id=tel.outbound_trunk_id,
                sip_call_to=tel.call_to,
                participant_identity=sip_identity,
                participant_name="Simulated Callee",
                wait_until_answered=tel.wait_until_answered,
                krisp_enabled=tel.krisp_enabled,
            )
        except Exception as e:
            writer.emit(
                "outbound.dial_failed",
                spec=sip_error_spec(e, call_to=tel.call_to),
                include_dialogue=False,
            )
            raise SimLegError(f"outbound_sim_callee dial failed: {e}") from e

        dial_ms = int((time.monotonic() - t0) * 1000)
        sip_part_id = getattr(sip_info, "participant_identity", None) or sip_identity
        writer.emit(
            "outbound.dial_answered",
            spec={
                "call_to": tel.call_to,
                "dial_ms": dial_ms,
                "participant_identity": sip_part_id,
                "sip_call_id": getattr(sip_info, "sip_call_id", None),
                "mode": MODE,
            },
            include_dialogue=False,
        )
        writer.emit(
            "sip.participant_connected",
            spec={
                "identity": sip_part_id,
                "room": agent_room_name,
                "role": "outbound_leg",
            },
            include_dialogue=False,
        )

        # Wait for inbound SIP leg on sim-room (hairpin answer side), best-effort.
        try:
            sim_sip_id = await adapter.wait_for_sip_participant(
                sim_room_name, timeout_ms=min(3_000, max(1_500, tel.prepare_ms))
            )
            writer.emit(
                "sip.participant_connected",
                spec={"identity": sim_sip_id, "room": sim_room_name, "role": "sim_inbound_leg"},
                include_dialogue=False,
            )
        except Exception as e:
            writer.emit(
                "sip.wait_sim_leg",
                spec={
                    "status": "timeout_or_missing",
                    "detail": f"{type(e).__name__}: {e}",
                    "note": "Ensure sim inbound DID dispatch rule targets this sim-room "
                    f"({sim_room_name}) for full hairpin audio. Gemini/record use agent-room.",
                },
                include_dialogue=False,
            )

        return SimLegHandle(
            agent_room=agent_room,
            sim_room=sim_room,
            agent_room_name=agent_room_name,
            sim_room_name=sim_room_name,
            sim_identity=sip_part_id,
            agent_identity=agent_identity,
            mode=MODE,
            gemini_listen_identity=None,
            gemini_listen_sip=True,
            gemini_listen_agent_room=True,
            rooms_to_delete=[agent_room_name, sim_room_name],
            meta={
                "dial_ms": dial_ms,
                "call_to": tel.call_to,
                "sip_call_id": getattr(sip_info, "sip_call_id", None),
                "dispatch_id": dispatch_id,
            },
        )
