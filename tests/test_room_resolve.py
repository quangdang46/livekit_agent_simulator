"""Room discovery A+B unit tests (mock LiveKit list_rooms + list_participants)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from livekit_agent_simulator.config import (
    LiveKitConfig,
    ObserveConfig,
    SimConfig,
    SimulatorConfig,
    SimulatorVoiceConfig,
)
from livekit_agent_simulator.livekit.adapter import AgentJoinTimeout, LiveKitAdapter


def _make_cfg(tmp_path, *, join_timeout_ms=1000):
    return SimConfig(
        project_root=tmp_path,
        livekit=LiveKitConfig(
            url="wss://demo.livekit.cloud",
            api_key="APIkey",
            api_secret="secret",
            agent_name="test-agent",
            room_prepare_ms=0,
            agent_join_timeout_ms=join_timeout_ms,
        ),
        simulator=SimulatorConfig(google_api_key="AIzaTest", voice=SimulatorVoiceConfig()),
        observe=ObserveConfig(),
    )


def _mk_participant(identity, kind, *, sip_attrs=None):
    attrs = {}
    if sip_attrs:
        attrs.update(sip_attrs)
    return SimpleNamespace(identity=identity, kind=kind, attributes=attrs)


def _make_adapter(cfg, rooms_participants):
    """rooms_participants: dict {room_name: [participants]}."""
    adapter = LiveKitAdapter(cfg)
    lkapi = MagicMock()

    async def list_rooms(_req):
        return SimpleNamespace(
            rooms=[SimpleNamespace(name=k) for k in rooms_participants]
        )

    async def list_participants(req):
        name = req.room
        parts = rooms_participants.get(name, [])
        return SimpleNamespace(participants=parts)

    lkapi.room.list_rooms = AsyncMock(side_effect=list_rooms)
    lkapi.room.list_participants = AsyncMock(side_effect=list_participants)
    adapter._lkapi = lkapi
    return adapter


# ── Phase A: deterministic name match ─────────────────────────────────────────


async def test_phase_a_exact_room_name(tmp_path):
    cfg = _make_cfg(tmp_path)
    adapter = _make_adapter(
        cfg,
        {"inbound-room": [_mk_participant("agent-AJ_x", 4)]},
    )
    name, aid = await adapter.find_agent_room(
        prefer_name_substr="inbound-room",
        timeout_ms=500,
        poll_ms=50,
        require_sip=False,
    )
    assert name == "inbound-room"
    assert aid == "agent-AJ_x"


async def test_phase_a_prefers_named_over_first(tmp_path):
    cfg = _make_cfg(tmp_path)
    adapter = _make_adapter(
        cfg,
        {
            "lk-sim-other": [_mk_participant("agent-AJ_a", 4)],
            "inbound-x": [_mk_participant("agent-AJ_b", 4)],
        },
    )
    name, aid = await adapter.find_agent_room(
        prefer_name_substr="inbound-x",
        timeout_ms=500,
        poll_ms=50,
        require_sip=False,
    )
    assert name == "inbound-x"


# ── Phase B: sip_call_id match ────────────────────────────────────────────────


async def test_phase_b_sip_call_id(tmp_path):
    cfg = _make_cfg(tmp_path)
    adapter = _make_adapter(
        cfg,
        {
            "call-_+617_zNiM": [
                _mk_participant("agent-AJ_x", 4),
                _mk_participant("sip-leg", 3, sip_attrs={"sip.callID": "SCL_abc123"}),
            ],
            "lk-sim-smoke": [_mk_participant("agent-AJ_y", 4)],
        },
    )
    name, aid = await adapter.find_agent_room(
        sip_call_id_substr="SCL_abc123",
        timeout_ms=500,
        poll_ms=50,
        require_sip=False,
    )
    assert name == "call-_+617_zNiM"


async def test_phase_b_prefers_sip_call_id_over_wrong_name(tmp_path):
    """sip_call_id match should win over first agent room (parallel scenario)."""
    cfg = _make_cfg(tmp_path)
    adapter = _make_adapter(
        cfg,
        {
            "call-_+617_zNiM": [
                _mk_participant("agent-AJ_x", 4),
                _mk_participant("sip-in", 3, sip_attrs={"sip.callID": "SCL_match"}),
            ],
            "lk-sim-smoke-hello-abc": [
                _mk_participant("agent-AJ_y", 4),
                _mk_participant("lk-sim-caller", 0),
            ],
        },
    )
    name, aid = await adapter.find_agent_room(
        sip_call_id_substr="SCL_match",
        timeout_ms=500,
        poll_ms=50,
    )
    assert name == "call-_+617_zNiM"


async def test_phase_b_no_sip_needle_still_finds_first(tmp_path):
    """Without sip_call_id or name preference, return first agent room (legacy)."""
    cfg = _make_cfg(tmp_path)
    adapter = _make_adapter(
        cfg,
        {
            "lk-sim-smoke-hello-abc": [_mk_participant("agent-AJ_first", 4)],
            "some-other": [_mk_participant("agent-AJ_second", 4)],
        },
    )
    name, aid = await adapter.find_agent_room(timeout_ms=500, poll_ms=50)
    assert aid == "agent-AJ_first"


async def test_requires_sip_filters_non_sip_rooms(tmp_path):
    """With require_sip, skip rooms without any SIP participant."""
    cfg = _make_cfg(tmp_path)
    adapter = _make_adapter(
        cfg,
        {
            "lk-sim-smoke-hello": [
                _mk_participant("agent-AJ_first", 4),
                _mk_participant("lk-sim-caller", 0),
            ],
            "inbound-call-room": [
                _mk_participant("agent-AJ_x", 4),
                _mk_participant("sip-leg", 3),
            ],
        },
    )
    name, aid = await adapter.find_agent_room(
        timeout_ms=500,
        poll_ms=50,
        require_sip=True,
    )
    assert name == "inbound-call-room"


async def test_timeout_raises_error(tmp_path):
    cfg = _make_cfg(tmp_path, join_timeout_ms=100)
    adapter = _make_adapter(cfg, {})
    with pytest.raises(AgentJoinTimeout, match="test-agent"):
        await adapter.find_agent_room(timeout_ms=100, poll_ms=30)
