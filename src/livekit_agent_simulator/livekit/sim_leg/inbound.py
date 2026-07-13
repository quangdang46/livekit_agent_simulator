"""Inbound SIP SimLeg — Gemini dials as caller (Cloud hairpin)."""

from __future__ import annotations

import time

from ..adapter import SIM_IDENTITY
from .errors import sip_error_spec
from .protocol import SimLegContext, SimLegError, SimLegHandle

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

        sip_identity = f"sip-in-{ctx.run_id[:12]}"
        writer.emit(
            "inbound.dial_started",
            spec={
                "dial_in": tel.dial_in,
                "room": sim_room_name,
                "participant_identity": sip_identity,
                "wait_until_answered": tel.wait_until_answered,
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
        writer.emit(
            "inbound.answered",
            spec={
                "dial_in": tel.dial_in,
                "dial_ms": dial_ms,
                "participant_identity": getattr(sip_info, "participant_identity", sip_identity),
                "sip_call_id": getattr(sip_info, "sip_call_id", None),
            },
            include_dialogue=False,
        )

        # Resolve agent-room (A+B):
        #   A. Explicit Telephony.agent_room / agent_room_name_template (deterministic rule).
        #   B. Correlate via sip_call_id (safe under --parallel).
        #   C. Fallback: name + SIP heuristics.
        agent_room_name = tel.agent_room
        if not agent_room_name and tel.agent_room_name_template:
            agent_room_name = tel.agent_room_name_template.replace("{run_id}", ctx.run_id)
            if tel.dial_in:
                agent_room_name = agent_room_name.replace("{dial_in}", tel.dial_in.strip())
            from string import digits
            num_digits = "".join(ch for ch in (tel.dial_in or "") if ch.isdigit())
            if num_digits:
                agent_room_name = agent_room_name.replace("{number}", num_digits)

        if agent_room_name:
            # Deterministic A — dispatch + wait for agent in known room.
            try:
                await adapter.dispatch_agent(agent_room_name, ctx.dispatch_metadata)
            except Exception:
                pass
            agent_identity = await adapter.wait_for_agent(agent_room_name)
            writer.emit(
                "inbound.agent_room_deterministic",
                spec={"room": agent_room_name, "agent_identity": agent_identity},
                include_dialogue=False,
            )
        else:
            # B + C — correlate via sip_call_id (returned by create_sip_participant) or heuristics.
            sip_call_id = getattr(sip_info, "sip_call_id", None)
            writer.emit(
                "inbound.agent_room_discover",
                spec={
                    "dial_in": tel.dial_in,
                    "sip_call_id": sip_call_id,
                    "require_sip": True,
                    "note": "A+B: prefer sip_call_id match; then name digits; "
                    "then SIP room (no legacy first-agent under --parallel)",
                },
                include_dialogue=False,
            )
            agent_room_name, agent_identity = await adapter.find_agent_room(
                exclude_rooms={sim_room_name},
                timeout_ms=ctx.cfg.livekit.agent_join_timeout_ms,
                require_sip=True,
                prefer_name_substr=tel.dial_in,
                sip_call_id_substr=sip_call_id,
            )

        writer.emit(
            "dispatch.agent_joined",
            spec={
                "identity": agent_identity,
                "room": agent_room_name,
                "mode": "inbound_sip",
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

        # Human side from agent POV = SIP participant in agent-room (caller).
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
                "sip_call_id": getattr(sip_info, "sip_call_id", None),
            },
        )
