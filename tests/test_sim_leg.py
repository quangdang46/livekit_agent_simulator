"""SimLeg factory + WebRTC leg smoke (mocked adapter)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from livekit_agent_simulator.config import (
    LiveKitConfig,
    ObserveConfig,
    SimConfig,
    SimulatorConfig,
    SimulatorVoiceConfig,
    TelephonyConfig,
)
from livekit_agent_simulator.livekit.sim_leg import (
    SimLegContext,
    SimLegError,
    WebRtcSimLeg,
    sim_leg_factory,
)
from livekit_agent_simulator.scenario import CallerSpec, Scenario, TelephonySpec


def make_cfg(tmp_path, **tel):
    return SimConfig(
        project_root=tmp_path,
        livekit=LiveKitConfig(
            url="wss://demo.livekit.cloud",
            api_key="APIkey",
            api_secret="secret",
            agent_name="my-agent",
            room_prepare_ms=0,
            agent_join_timeout_ms=800,
        ),
        simulator=SimulatorConfig(google_api_key="AIzaTest", voice=SimulatorVoiceConfig()),
        observe=ObserveConfig(),
        telephony=TelephonyConfig(**tel) if tel else TelephonyConfig(),
    )


def test_factory_modes():
    assert type(sim_leg_factory("webrtc_sim")).__name__ == "WebRtcSimLeg"
    assert type(sim_leg_factory("outbound_sip")).__name__ == "OutboundSipSimLeg"
    assert type(sim_leg_factory("outbound_sim_callee")).__name__ == "OutboundSimCalleeSimLeg"
    assert type(sim_leg_factory("inbound_sip")).__name__ == "InboundSipSimLeg"
    assert type(sim_leg_factory("agent_dials")).__name__ == "AgentDialsSimLeg"
    with pytest.raises(SimLegError, match="Unknown"):
        sim_leg_factory("fax")


async def test_outbound_sip_human_pickup_connect(tmp_path):
    cfg = make_cfg(tmp_path, outbound_trunk_id="ST_x")
    scenario = Scenario(
        id="s",
        path=tmp_path / "s.jsonl",
        persona={"brief": "x"},
        caller=CallerSpec(mode="outbound_sip"),
        telephony=TelephonySpec(call_to="+15551112222"),
    )
    writer = MagicMock()
    writer.emit = MagicMock()

    room = MagicMock(name="room")
    adapter = MagicMock()
    adapter.create_room_and_dispatch = AsyncMock(
        return_value=SimpleNamespace(room_name="lk-sim-r1", dispatch_id="d1", agent_identity="")
    )
    adapter.wait_for_agent = AsyncMock(return_value="agent-xyz")
    adapter.create_sip_participant = AsyncMock(
        return_value=SimpleNamespace(participant_identity="sip-out-abc", sip_call_id="SC_1")
    )
    adapter.connect_simulator = AsyncMock(return_value=room)
    adapter.isolate_sip_handset = AsyncMock(
        return_value={"isolation": "mute_and_unsubscribe", "muted_track_sids": ["TR_1"]}
    )

    from livekit_agent_simulator.livekit.sim_leg.outbound import OutboundSipSimLeg

    handle = await OutboundSipSimLeg().connect(
        SimLegContext(
            adapter=adapter,
            cfg=cfg,
            scenario=scenario,
            writer=writer,
            run_id="r1abcdefghij",
            dispatch_metadata=None,
            first_speaker="agent",
        )
    )
    assert handle.mode == "outbound_sip"
    assert handle.agent_room is room
    assert handle.sim_room is room
    assert handle.sim_identity == "lk-sim-caller"
    assert handle.gemini_listen_identity == "agent-xyz"
    assert handle.rooms_to_delete == ["lk-sim-r1"]
    adapter.isolate_sip_handset.assert_awaited()
    kinds = [c.args[0] for c in writer.emit.call_args_list]
    assert "outbound.dial_answered" in kinds
    assert "outbound.handset_isolated" in kinds
