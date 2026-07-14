"""Factory: Caller.mode → SimLeg strategy."""

from __future__ import annotations

from .agent_dials import AgentDialsSimLeg
from .inbound import InboundSipSimLeg
from .outbound import OutboundSipSimLeg
from .protocol import SimLeg, SimLegError
from .sim_callee import OutboundSimCalleeSimLeg
from .webrtc import WebRtcSimLeg


def sim_leg_factory(mode: str) -> SimLeg:
    """Map ``Caller.mode`` → strategy instance."""
    m = (mode or "webrtc_sim").strip().lower()
    if m == "webrtc_sim":
        return WebRtcSimLeg()
    if m == "outbound_sip":
        return OutboundSipSimLeg()
    if m == "outbound_sim_callee":
        return OutboundSimCalleeSimLeg()
    if m == "inbound_sip":
        return InboundSipSimLeg()
    if m == "agent_dials":
        return AgentDialsSimLeg()
    raise SimLegError(
        f"Unknown Caller.mode {mode!r}. "
        f"Expected webrtc_sim | inbound_sip | outbound_sip | outbound_sim_callee | agent_dials."
    )
