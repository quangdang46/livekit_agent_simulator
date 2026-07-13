"""SimLeg Strategy package — transport legs for WebRTC / inbound / outbound SIP.

Template Method: ``run_orchestrator`` owns the pipeline.
Strategy: each mode implements ``connect()`` → ``SimLegHandle``.
Factory: ``sim_leg_factory(mode)``.
"""

from .agent_dials import AgentDialsSimLeg
from .factory import sim_leg_factory
from .inbound import InboundSipSimLeg
from .outbound import OutboundSipSimLeg
from .protocol import SimLeg, SimLegContext, SimLegError, SimLegHandle
from .webrtc import WebRtcSimLeg

__all__ = [
    "AgentDialsSimLeg",
    "InboundSipSimLeg",
    "OutboundSipSimLeg",
    "SimLeg",
    "SimLegContext",
    "SimLegError",
    "SimLegHandle",
    "WebRtcSimLeg",
    "sim_leg_factory",
]
