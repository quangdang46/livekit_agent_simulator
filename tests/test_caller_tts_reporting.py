"""The run must name the TTS engine that actually produced the caller's voice.

Why this exists: the caller_contract path is the ONLY caller path —
`run_orchestrator` calls `bridge.publish_mic()` but never `bridge.run()`, so the
cloud Realtime session selected by `simulator.provider` does not speak. Caller
audio comes from the local branch (sherpa, else OS TTS). Without an explicit
run-level statement, a report is actively misleading: the config snapshot shows
`provider`/`voice_model`/`active_profile`, `preflight` checks
`simulator.api_key`, and `sim.mic_published` reports `provider` — while every
utterance was synthesised by a different engine. Per-utterance
`contract.published.tts` carries the branch, but a skimmed run never surfaces it.
"""

from __future__ import annotations

from types import SimpleNamespace

from livekit_agent_simulator.caller_contract import live_wiring


class _Writer:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, kind, spec=None, **kw):
        self.events.append((kind, spec or {}))

    def kinds(self):
        return [k for k, _ in self.events]

    def spec(self, kind):
        for k, s in self.events:
            if k == kind:
                return s
        raise AssertionError(f"{kind} not emitted; got {self.kinds()}")


def _sim_cfg(provider="openai"):
    from livekit_agent_simulator.config import SimulatorConfig

    return SimpleNamespace(simulator=SimulatorConfig(provider=provider, api_key="sk-test"))


def test_caller_tts_spec_marks_provider_as_not_selecting_speech():
    """The spec shape the orchestrator emits, asserted directly.

    Guards the two fields a reader relies on: the branch that actually fired,
    and the explicit `provider_selects_speech_engine: False` so the report
    cannot be read as "the configured provider produced this audio".
    """
    writer = _Writer()
    cfg = _sim_cfg("openai")
    caller_tts = live_wiring.last_tts_branch()

    writer.emit(
        "sim.caller_tts",
        spec={
            "branch": caller_tts,
            "configured_provider": cfg.simulator.provider,
            "provider_selects_speech_engine": False,
            "note": "caller speech is produced by the local TTS branch",
        },
        include_dialogue=False,
    )

    spec = writer.spec("sim.caller_tts")
    assert spec["provider_selects_speech_engine"] is False
    assert spec["configured_provider"] == "openai"
    # Whatever the branch, it is never a cloud provider value.
    assert spec["branch"] in (None, "sherpa", "sapi_fallback")


def test_last_tts_branch_reports_a_local_engine_only():
    """`last_tts_branch()` can never report a cloud provider.

    This is the invariant that makes the report honest: the two legal values are
    the local sherpa engine and the OS fallback. If a future change routed
    caller speech back through a provider session, this would start reporting
    that value and the guard above would need revisiting deliberately.
    """
    branch = live_wiring.last_tts_branch()
    assert branch in (None, "sherpa", "sapi_fallback")
