"""P1.E — preflight telephony.outbound_sim_callee guidance (portable)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from livekit_agent_simulator.preflight import PreflightResult


def _cfg(**tel_kw):
    tel = SimpleNamespace(
        outbound_trunk_id=tel_kw.get("outbound_trunk_id"),
        dial_in=tel_kw.get("dial_in"),
        sim_inbound_number=tel_kw.get("sim_inbound_number"),
    )
    return SimpleNamespace(
        telephony=tel,
        livekit=SimpleNamespace(url="wss://x", api_key="k", api_secret="s", agent_name="a"),
        observe=SimpleNamespace(timezone="UTC"),
        simulator=SimpleNamespace(google_api_key="x" * 30),
        reports_dir=None,
        scenarios_dir=None,
        dot_dir=None,
    )


@pytest.mark.asyncio
async def test_preflight_warns_trunk_without_sim_inbound(tmp_path, monkeypatch):
    from livekit_agent_simulator import preflight as pf

    # Minimal load_config bypass: call the telephony block via run_preflight with mocked load
    def fake_load(root):
        cfg = SimpleNamespace(
            telephony=SimpleNamespace(
                outbound_trunk_id="ST_x",
                dial_in=None,
                sim_inbound_number=None,
            ),
            livekit=SimpleNamespace(
                url="wss://example.livekit.cloud",
                api_key="k",
                api_secret="s" * 20,
                agent_name="agent",
            ),
            observe=SimpleNamespace(timezone="UTC"),
            simulator=SimpleNamespace(google_api_key="A" * 30),
            reports_dir=tmp_path / "reports",
            scenarios_dir=tmp_path / "scenarios",
            dot_dir=tmp_path,
        )
        cfg.reports_dir.mkdir(exist_ok=True)
        cfg.scenarios_dir.mkdir(exist_ok=True)
        return cfg

    monkeypatch.setattr(pf, "load_config", fake_load)

    async def no_api(cfg, result):
        result.add("livekit.api", "pass", "skipped")

    monkeypatch.setattr(pf, "_check_livekit_api", no_api)
    result, _ = await pf.run_preflight(tmp_path, connectivity=True)
    names = {c["name"]: c for c in result.checks}
    assert names["telephony"]["status"] in ("pass", "warn")
    assert "telephony.outbound_sim_callee" in names
    assert names["telephony.outbound_sim_callee"]["status"] == "warn"
    assert "sim_inbound" in names["telephony.outbound_sim_callee"]["detail"]


@pytest.mark.asyncio
async def test_preflight_pass_when_trunk_and_sim_inbound(tmp_path, monkeypatch):
    from livekit_agent_simulator import preflight as pf

    def fake_load(root):
        cfg = SimpleNamespace(
            telephony=SimpleNamespace(
                outbound_trunk_id="ST_x",
                dial_in=None,
                sim_inbound_number="+15551212",
            ),
            livekit=SimpleNamespace(
                url="wss://example.livekit.cloud",
                api_key="k",
                api_secret="s" * 20,
                agent_name="agent",
            ),
            observe=SimpleNamespace(timezone="UTC"),
            simulator=SimpleNamespace(google_api_key="A" * 30),
            reports_dir=tmp_path / "reports",
            scenarios_dir=tmp_path / "scenarios",
            dot_dir=tmp_path,
        )
        cfg.reports_dir.mkdir(exist_ok=True)
        cfg.scenarios_dir.mkdir(exist_ok=True)
        return cfg

    monkeypatch.setattr(pf, "load_config", fake_load)

    async def no_api(cfg, result):
        result.add("livekit.api", "pass", "skipped")

    monkeypatch.setattr(pf, "_check_livekit_api", no_api)
    result, _ = await pf.run_preflight(tmp_path, connectivity=True)
    names = {c["name"]: c for c in result.checks}
    assert names["telephony.outbound_sim_callee"]["status"] == "pass"
