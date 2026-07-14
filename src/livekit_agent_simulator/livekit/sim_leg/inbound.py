"""Inbound SIP SimLeg — Gemini dials as caller (Cloud hairpin)."""

from __future__ import annotations

import time

from ..adapter import SIM_IDENTITY
from .errors import sip_error_spec
from .protocol import SimLegContext, SimLegError, SimLegHandle
from .room_resolve import (
    resolve_deterministic,
    resolve_inbound_agent_room,
    wall_time_ms,
)


class InboundSipSimLeg:
    """Gemini in sim-room dials agent ``dial_in`` (Cloud hairpin)."""

    async def connect(self, ctx: SimLegContext) -> SimLegHandle:
        from ...scenario import effective_telephony

        tel = effective_telephony(ctx.scenario, ctx.cfg)
        if not tel.outbound_trunk_id:
            raise SimLegError(
                "inbound_sip requires telephony.outbound_trunk_id "
                "(config) or Telephony.sip_trunk_id (scenario) to place the call."
            )
        if not tel.dial_in:
            raise SimLegError(
                "inbound_sip requires Telephony.dial_in or config telephony.dial_in "
                "(agent-side inbound DID)."
            )

        adapter, writer = ctx.adapter, ctx.writer
        sim_room_name = f"lk-sim-sip-{ctx.run_id}"
        await adapter.create_room(sim_room_name)

        sim_room = await adapter.connect_participant(
            sim_room_name, identity=SIM_IDENTITY, name="Agent Simulator Caller"
        )
        writer.emit(
            "sim.connected",
            spec={"identity": SIM_IDENTITY, "room": sim_room_name, "mode": "inbound_sip"},
            include_dialogue=False,
        )

        # Snapshot before dial so leftover call-+DID-* rooms cannot win discovery.
        preexisting_rooms = await adapter.list_room_names()
        dial_started_ms = wall_time_ms()

        sip_identity = f"sip-in-{ctx.run_id[:12]}"
        writer.emit(
            "inbound.dial_started",
            spec={
                "dial_in": tel.dial_in,
                "room": sim_room_name,
                "participant_identity": sip_identity,
                "wait_until_answered": tel.wait_until_answered,
                "preexisting_rooms": len(preexisting_rooms),
            },
            include_dialogue=False,
        )
        t0 = time.monotonic()
        try:
            sip_info = await adapter.create_sip_participant(
                room_name=sim_room_name,
                sip_trunk_id=tel.outbound_trunk_id,
                sip_call_to=tel.dial_in,
                participant_identity=sip_identity,
                participant_name="Simulated Caller",
                wait_until_answered=tel.wait_until_answered,
                krisp_enabled=tel.krisp_enabled,
            )
        except Exception as e:
            writer.emit(
                "inbound.dial_failed",
                spec=sip_error_spec(e, call_to=tel.dial_in),
                include_dialogue=False,
            )
            raise SimLegError(f"inbound dial failed: {e}") from e

        dial_ms = int((time.monotonic() - t0) * 1000)
        sip_call_id = getattr(sip_info, "sip_call_id", None)
        writer.emit(
            "inbound.answered",
            spec={
                "dial_in": tel.dial_in,
                "dial_ms": dial_ms,
                "participant_identity": getattr(sip_info, "participant_identity", sip_identity),
                "sip_call_id": sip_call_id,
            },
            include_dialogue=False,
        )

        # Resolve agent-room:
        #   A. Explicit Telephony.agent_room / agent_room_name_template.
        #   B. sip_call_id attr match, else fresh dial-digit room (not preexisting).
        agent_room_name = tel.agent_room
        if not agent_room_name and tel.agent_room_name_template:
            agent_room_name = tel.agent_room_name_template.replace("{run_id}", ctx.run_id)
            if tel.dial_in:
                agent_room_name = agent_room_name.replace("{dial_in}", tel.dial_in.strip())
            num_digits = "".join(ch for ch in (tel.dial_in or "") if ch.isdigit())
            if num_digits:
                agent_room_name = agent_room_name.replace("{number}", num_digits)

        if agent_room_name:
            hit = await resolve_deterministic(
                adapter,
                room_name=agent_room_name,
                dispatch_metadata=ctx.dispatch_metadata,
                timeout_ms=ctx.cfg.livekit.agent_join_timeout_ms,
            )
            writer.emit(
                "inbound.agent_room_deterministic",
                spec={
                    "room": hit.room,
                    "agent_identity": hit.agent_identity,
                    "phase": hit.phase,
                },
                include_dialogue=False,
            )
        else:
            writer.emit(
                "inbound.agent_room_discover",
                spec={
                    "dial_in": tel.dial_in,
                    "sip_call_id": sip_call_id,
                    "preexisting_rooms": len(preexisting_rooms),
                    "created_after_ms": dial_started_ms,
                    "require_sip": True,
                    "note": "sip_call_id match, else fresh dial-digit room "
                    "(never latch leftover call-* rooms)",
                },
                include_dialogue=False,
            )
            hit = await resolve_inbound_agent_room(
                adapter,
                sip_call_id=sip_call_id,
                dial_in=tel.dial_in,
                exclude_rooms={sim_room_name},
                preexisting_rooms=preexisting_rooms,
                created_after_ms=dial_started_ms,
                timeout_ms=ctx.cfg.livekit.agent_join_timeout_ms,
            )
            writer.emit(
                "inbound.agent_room_resolved",
                spec={
                    "room": hit.room,
                    "agent_identity": hit.agent_identity,
                    "phase": hit.phase,
                    "sip_call_id": sip_call_id,
                },
                include_dialogue=False,
            )

        agent_room_name = hit.room
        agent_identity = hit.agent_identity

        writer.emit(
            "dispatch.agent_joined",
            spec={
                "identity": agent_identity,
                "room": agent_room_name,
                "mode": "inbound_sip",
                "phase": hit.phase,
            },
            include_dialogue=False,
        )
        writer.emit(
            "sip.participant_connected",
            spec={
                "identity": sip_identity,
                "room": sim_room_name,
                "role": "inbound_caller_leg",
            },
            include_dialogue=False,
        )

        agent_room = await adapter.connect_participant(
            agent_room_name,
            identity=f"lk-sim-obs-{ctx.run_id[:8]}",
            name="Agent Simulator Observer",
        )

        try:
            human_sip = await adapter.wait_for_sip_participant(
                agent_room_name, timeout_ms=10_000
            )
        except Exception:
            human_sip = sip_identity

        return SimLegHandle(
            agent_room=agent_room,
            sim_room=sim_room,
            agent_room_name=agent_room_name,
            sim_room_name=sim_room_name,
            sim_identity=human_sip,
            agent_identity=agent_identity,
            mode="inbound_sip",
            gemini_listen_identity=None,
            gemini_listen_sip=True,
            gemini_listen_agent_room=True,
            rooms_to_delete=[agent_room_name, sim_room_name],
            meta={
                "dial_ms": dial_ms,
                "dial_in": tel.dial_in,
                "sip_call_id": sip_call_id,
                "resolve_phase": hit.phase,
            },
        )
