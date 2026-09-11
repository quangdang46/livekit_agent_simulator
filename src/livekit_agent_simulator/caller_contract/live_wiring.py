"""Live assembly of ContractCallerDriver for a real LiveKit run.

Called from run_orchestrator.py ONLY when ``scenario.caller_actions`` is
non-empty — the single execution path for such scenarios (no legacy
persona+ScriptRunner/freestyle-bridge code runs alongside it; see
docs/contract-caller-wiring.md). Legacy scenarios (empty caller_actions)
never reach this module.

Text backend selection mirrors ``cfg.simulator.provider`` (google|openai) so
the same config key already used for the legacy bridge selects the do:
generation backend too — no new config surface for this slice.

Semantic verifier selection (PLAN-20260910 slice 3): reuses the EXISTING
``judge:`` config block (already used by PassCriteria/asserts LLM judging,
see evals/resolve.py) as the opt-in fallback flag — no new config surface.
When ``cfg.judge`` resolves to a ready backend, ``LLMSemanticVerifier``
(Tier-3) is wired in; otherwise ``ContractValidator`` falls back to its own
default (``RuleBasedSemanticVerifier``, Tier-1) with zero behavior change for
targets that never configured a judge. Swapping the backend touches ONLY
this function — driver.py/orchestrator.py/validator.py/publish_sink.py are
unchanged (validator.py already had the honest-target contract in place
since ``02553c3``).

Raises ``ContractDriverFailure`` (never returns normally) when the driver
reports a failure, so run_scenario_instance's existing ``except Exception``
handler marks the run failed exactly like any other hard error — no separate
failure-handling branch needed there.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..audio.sapi_tts import TARGET_RATE, synthesize_pcm16_mono
from ..evals.resolve import resolve_judge
from . import EndedBy
from .agent_wait import ObserverAgentWait
from .driver import ContractCallerDriver, DriverResult
from .language_adapter import AILanguageAdapter
from .orchestrator import Orchestrator
from .publish_sink import DEFAULT_DRAIN_TIMEOUT_S, BridgePublishSink
from .semantic import RuleBasedSemanticVerifier
from .semantic_llm import LLMSemanticVerifier
from .text_backends import GeminiTextBackend, OpenAITextBackend
from .validator import ContractValidator, SemanticVerifierProtocol

if TYPE_CHECKING:
    from ..callers.base import CallerBridge
    from ..livekit.observer import Observer
    from ..logging.event_writer import EventWriter
    from ..scenario import Scenario, SimulatorSpec


class ContractDriverFailure(RuntimeError):
    """Wraps a failed DriverResult so it surfaces through the normal
    run-failure path (see module docstring)."""

    def __init__(self, result: DriverResult) -> None:
        self.result = result
        failure = result.failure
        reason = failure.reason.value if failure is not None else "UNKNOWN"
        detail = failure.detail if failure is not None else "no detail"
        super().__init__(f"{reason}: {detail}")


def _build_text_backend(cfg: Any) -> Any:
    provider = getattr(cfg.simulator, "provider", "openai")
    api_key = cfg.simulator.api_key
    if provider == "google":
        return GeminiTextBackend(api_key=api_key)
    return OpenAITextBackend(api_key=api_key)


def _build_semantic_verifier(cfg: Any) -> SemanticVerifierProtocol:
    """Tier-3 (LLM judge) when a working judge credential exists; otherwise
    the Tier-1 rule-based baseline — see module docstring.

    Provider-aware: the ``judge:`` HTTP block serves PassCriteria/asserts
    judging and may point at a proxy that cannot serve the synchronous
    classify() path (run 010: HTTP 200 with empty content on every call).
    When the simulator provider is google, the Gemini key already in hand
    (same key the Live caller bridge uses) drives the judge directly via
    generateContent — no dependency on the asserts-judge proxy. The HTTP
    path is used only when the simulator provider is NOT google.
    """
    sim_api_key = getattr(cfg.simulator, "api_key", None)
    sim_provider = getattr(cfg.simulator, "provider", "openai")
    if sim_provider == "google" and (sim_api_key or "").strip():
        return LLMSemanticVerifier(
            api_key=sim_api_key.strip(),
            provider="gemini",
            model="gemini-flash-latest",
        )
    resolved = resolve_judge(getattr(cfg, "judge", None), sim_api_key=sim_api_key)
    if not resolved.ready:
        return RuleBasedSemanticVerifier()
    if resolved.mode == "http":
        assert resolved.base_url and resolved.api_key
        return LLMSemanticVerifier(
            api_key=resolved.api_key,
            provider=resolved.endpoint_type,
            model=resolved.model,
            base_url=resolved.base_url,
            temperature=resolved.temperature,
        )
    assert resolved.sim_api_key
    return LLMSemanticVerifier(
        api_key=resolved.sim_api_key,
        provider="gemini",
        model=resolved.model,
        temperature=resolved.temperature,
    )


def _synthesize(text: str) -> bytes:
    """Contract-path TTS: sherpa-onnx offline engine first, OS TTS fallback.

    Tries the pinned sherpa model (cached PCM per utterance via TtsCache);
    any sherpa failure (not installed, no model, corrupt download, backend
    unwired) falls back to ``synthesize_pcm16_mono`` (SAPI/say). The
    fallback keeps the contract path audible on machines without the
    ``tts-sherpa`` extra — determinism (same voice everywhere) is a
    sherpa-installed property, not a hard run requirement.

    Sherpa is attempted at most once per process: a failed attempt latches
    ``_SHERPA_DEAD`` so no later utterance pays for another model download
    attempt (and mocked-urllib tests that patch urlopen for the AI backend
    never see sherpa traffic after the first fallback).
    """
    global _SHERPA_DEAD
    if not _SHERPA_DEAD:
        try:
            from ..audio.tts_engine import TtsCache

            engine, cache, voice, language = _sherpa_engine()
            pcm, _hit = cache.synthesize_cached(
                engine, text, voice=voice, language=language
            )
            if pcm:
                return bytes(pcm)
        except Exception:  # noqa: BLE001 — any sherpa failure falls back to OS TTS
            pass
        _SHERPA_DEAD = True
    pcm = synthesize_pcm16_mono(text, rate=TARGET_RATE)
    return pcm or b""


_SHERPA_DEAD = False


def _sherpa_engine():
    """Build (or reuse) the pinned sherpa engine + utterance cache.

    Raises on any problem (missing package, missing model, corrupt file) —
    the caller (``_synthesize``) treats every raise as "fall back to SAPI".
    Model registry lives in ``audio/sherpa_models.py`` (pinned bundle URL +
    per-file SHAs, verified by ``ensure_model_dir``).
    """
    from pathlib import Path

    from ..audio.sherpa_models import default_model_bundle, default_voice, ensure_model_dir
    from ..audio.sherpa_tts import SherpaOnnxTtsEngine
    from ..audio.tts_engine import TtsCache

    bundle = default_model_bundle()
    cache_dir = Path.home() / ".cache" / "lks" / "tts-models"
    # Network fetch only when the pinned files are absent; tests inject a
    # fake downloader via ensure_model_dir directly. Live runs use
    # a minimal urllib downloader here (stdlib only, same as text_backends).
    import urllib.request

    def _download(url: str, dest: Path) -> None:
        with urllib.request.urlopen(url, timeout=300) as resp, open(dest, "wb") as fh:
            fh.write(resp.read())

    model_dir = ensure_model_dir(bundle, cache_dir, downloader=_download)
    utterance_cache = TtsCache(cache_dir=Path.home() / ".cache" / "lks" / "tts-pcm")
    voice, language = default_voice()
    engine = SherpaOnnxTtsEngine(
        model_id=bundle.model_id, model_path=model_dir
    )
    return engine, utterance_cache, voice, language


def _seed_from_id(scenario_id: str) -> int:
    """Stable non-negative seed from a scenario id (migration bridge for
    legacy persona interruption rates, which carry no seed)."""
    import hashlib

    digest = hashlib.sha256(str(scenario_id).encode()).digest()
    return int.from_bytes(digest[:4], "big")


@dataclass
class BridgeAssetPlayer:
    """Concrete AudioAssetPlayer: resolves asset refs and pushes beds onto
    the bridge mixer's parallel noise layer (never the speech path).

    Duck-typed bridge: uses ``_mixer.push_noise`` when present (both Gemini
    and OpenAI bridges own a ParallelMicMixer with a noise layer), else
    ``inject_cue(delivery="room_pcm")`` as fallback. Asset bytes come from
    ``audio.pcm_cue.resolve_cue_asset`` (builtin: + target overrides).
    """

    bridge: Any
    writer: Any = None
    scenario_dir: Any = None

    async def play_asset(
        self, asset: str, *, gain: float, loop: bool, label: str
    ) -> bool:
        from ..audio.pcm_cue import load_wav_pcm, resolve_cue_asset

        try:
            wav_path = resolve_cue_asset(
                asset,
                scenario_dir=self.scenario_dir,
            )
            pcm, _rate, channels = load_wav_pcm(wav_path)
        except Exception as exc:  # noqa: BLE001 — resolution failure is a refuse, surfaced by the driver
            self._emit(
                "contract.audio_refused",
                {"asset": asset, "label": label, "error": f"{type(exc).__name__}: {exc}"},
            )
            return False
        if channels != 1:
            self._emit(
                "contract.audio_refused",
                {"asset": asset, "label": label, "error": "only mono assets supported"},
            )
            return False
        mixer = getattr(self.bridge, "_mixer", None)
        push_noise = getattr(mixer, "push_noise", None) if mixer is not None else None
        if push_noise is not None:
            push_noise(pcm, gain=gain, loop=loop)
            return True
        inject = getattr(self.bridge, "inject_cue", None)
        if inject is not None:
            result = inject(
                "",
                label=label,
                delivery="room_pcm",
                asset=asset,
                scenario_dir=self.scenario_dir,
                gain=gain,
                loop=loop,
            )
            if asyncio.iscoroutine(result):
                await result
            return True
        return False

    def _emit(self, kind: str, spec: dict[str, Any]) -> None:
        if self.writer is not None:
            self.writer.emit(kind, spec=spec, source="sim.contract", include_dialogue=False)


async def run_contract_driver_path(
    scenario: "Scenario",
    run: "SimulatorSpec",
    observer: "Observer",
    bridge: "CallerBridge",
    writer: "EventWriter",
    cfg: Any,
    *,
    drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
    record_path: Any = None,
    replay_path: Any = None,
) -> str:
    """Drive ``scenario.caller_actions`` end to end. Returns the end_reason
    string on success; raises ``ContractDriverFailure`` on any failure.

    ``drain_timeout_s`` bounds how long ``PublishSink.publish()`` waits for
    caller audio to finish playing out before the driver advances to the
    next action — exposed (not hardcoded) so tests can exercise the
    stuck-mixer -> TRANSPORT_ERROR path without a slow real timeout.

    ``record_path``: write a versioned RunRecord (every generate+validate
    attempt) for later ``replay_path`` runs. ``replay_path``: serve recorded
    candidates instead of calling the AI backend (no network) and fail
    loudly on verdict divergence. Mutually exclusive.
    """
    if record_path is not None and replay_path is not None:
        raise ValueError("record_path and replay_path are mutually exclusive")
    _ = run  # reserved: max_turns/timeout_s already live on each contract
    orch = Orchestrator()
    replay_record = None
    if replay_path is not None:
        from .record_replay import (
            RecordedSemanticVerifier,
            ReplayLanguageBackend,
            RunRecord,
        )

        # Zero-AI replay: BOTH the generation backend AND the semantic
        # verifier serve recorded evidence. Building the LLM judge backend
        # here would defeat replay (network calls inside validation), so
        # the recorded-evidence verifier replaces it unconditionally.
        replay_record = RunRecord.read(replay_path)
        replay_backend = ReplayLanguageBackend(replay_record)
        adapter = AILanguageAdapter(backend=replay_backend)
        validator = ContractValidator(
            semantic_verifier=RecordedSemanticVerifier(replay_record)
        )
    else:
        validator = ContractValidator(semantic_verifier=_build_semantic_verifier(cfg))
        adapter = AILanguageAdapter(backend=_build_text_backend(cfg))
    driver = ContractCallerDriver(
        orchestrator=orch,
        validator=validator,
        adapter=adapter,
        synthesize=_synthesize,
    )
    recorder = None
    if record_path is not None:
        from .record_replay import Recorder

        recorder = Recorder(
            scenario_id=str(getattr(scenario, "id", "")),
            seed=_seed_from_id(str(getattr(scenario, "id", ""))),
        )
        driver.recorder = recorder
    sink = BridgePublishSink(
        bridge=bridge, orchestrator=orch, writer=writer, drain_timeout_s=drain_timeout_s
    )
    agent = ObserverAgentWait(observer=observer)

    def _emit(kind: str, spec: dict[str, Any] | None = None) -> None:
        writer.emit(kind, spec=spec or {}, source="sim.contract", include_dialogue=False)

    from ..behavior_compile import silent_mode_enabled

    # SimpleNamespace test doubles carry only caller_actions: default to the
    # immediate-start ("user") path with silent mode off.
    run_spec = getattr(scenario, "run_spec", None)
    first_speaker = getattr(run_spec, "first_speaker", "user") or "user"
    persona = getattr(scenario, "persona", None)

    # Compatibility bridge (DEPRECATED, removal gate: no caller_actions
    # scenario may rely on it silently — every use emits contract.compat_bridge
    # so remaining users are visible in reports; delete once zero runs emit it):
    # persona speech_conditions.interruption_* still fills a missing
    # per-action interaction on do: steps. Explicit interaction wins.
    # silent_mode below has the same status.
    persona_interrupt: dict[str, Any] = {}
    sc = (persona or {}).get("speech_conditions") or {}
    if isinstance(sc, dict):
        legacy_rate = sc.get("interruption_rate", sc.get("interrupt_rate"))
        if legacy_rate is not None:
            persona_interrupt["interruption_rate"] = legacy_rate
            if sc.get("interruption_interval_ms") is not None:
                persona_interrupt["interruption_interval_ms"] = sc[
                    "interruption_interval_ms"
                ]
            persona_interrupt["interruption_seed"] = _seed_from_id(
                getattr(scenario, "id", "")
            )
    if persona_interrupt:
        from .dsl import _parse_interaction

        bridged = 0
        for action in scenario.caller_actions:
            if getattr(action, "kind", None) != "do" or action.interaction is not None:
                continue
            try:
                action.interaction = _parse_interaction(
                    persona_interrupt, file=None, line=getattr(action, "line_no", 0)
                )
                bridged += 1
            except Exception:  # noqa: BLE001 — invalid legacy rate fails at parse of persona, not here
                pass
        if bridged:
            _emit(
                "contract.compat_bridge",
                {
                    "bridge": "persona.interruption_rate",
                    "actions": bridged,
                    "note": "DEPRECATED: author interaction: on the do: step instead",
                },
            )
    driver._scenario_id = str(getattr(scenario, "id", ""))

    # Greeting wait reuses the scenario's own timeout (run_spec.timeout_s):
    # no second timeout semantics — an agent that never greets fails the
    # same way a run that never progresses does.
    # Compatibility bridge (DEPRECATED, same status as interruption_rate
    # above): silent_mode is still read from persona speech_conditions.
    # Removal gate: no template may rely on it silently — the compat event
    # below makes every use visible; delete the bridge once templates
    # author wait/end-only caller_steps without persona silent_mode.
    greeting_timeout_s = float(getattr(run_spec, "timeout_s", 30.0) or 30.0)
    _silent = silent_mode_enabled(persona)
    if _silent:
        _emit(
            "contract.compat_bridge",
            {
                "bridge": "persona.silent_mode",
                "note": "DEPRECATED: author wait/end-only caller_steps instead",
            },
        )

    assets = BridgeAssetPlayer(
        bridge=bridge,
        writer=writer,
        scenario_dir=getattr(scenario, "path", None),
    )

    # Hold timeout (contract equivalent of the legacy hold_music_timeout_s
    # loop): agent dead-air watchdog armed once the agent has spoken. Fires
    # a real bridge hang-up (ended_by=sim downstream), like the legacy path.
    hold_timeout_s = None
    hold_probe = getattr(scenario, "hold_music_timeout_s", None)
    if callable(hold_probe):
        try:
            hold_timeout_s = hold_probe()
        except Exception:  # noqa: BLE001 — unparsable hold config means off
            hold_timeout_s = None

    def _on_hold_timeout() -> None:
        hangup = getattr(bridge, "sim_hang_up", None)
        if callable(hangup):
            hangup()

    # Record even failing runs: the whole point of --record is reproducing
    # failures, and a ContractDriverFailure raise must not skip finalize().
    # Outcome stamps let replay assert the terminal state too. An unexpected
    # exception inside driver.run (never a DriverResult failure — those
    # return normally) still finalizes with an ERROR outcome stamp.
    result: DriverResult | None = None
    try:
        result = await driver.run(
            scenario.caller_actions,
            sink,
            agent,
            emit=_emit,
            first_speaker=first_speaker,
            silent_mode=_silent,
            greeting_timeout_s=greeting_timeout_s,
            assets=assets,
            hold_timeout_s=hold_timeout_s,
            on_hold_timeout=_on_hold_timeout,
        )
    finally:
        if recorder is not None and record_path is not None:
            failure = result.failure if result is not None else None
            recorder.record_outcome(
                failure_reason=(
                    failure.reason.value if failure is not None else "ERROR"
                ),
                ended_by=(
                    result.ended_by.value
                    if result is not None
                    else EndedBy.ERROR.value
                ),
            )
            recorder.finalize().write(record_path)
            _emit("contract.record_written", {"path": str(record_path)})
    assert result is not None  # driver.run always returns (failures are values, not raises)

    if replay_record is not None:
        replay_backend.assert_outcome(
            failure_reason=(
                result.failure.reason.value if result.failure is not None else None
            ),
            ended_by=result.ended_by.value,
        )

    if result.failure is not None:
        raise ContractDriverFailure(result)

    return {
        EndedBy.SCENARIO: "contract_scenario_end",
        EndedBy.CALLER: "contract_caller_end",
        EndedBy.AGENT: "contract_agent_end",
        EndedBy.TIMEOUT: "contract_timeout",
        EndedBy.TRANSPORT: "contract_transport_error",
        EndedBy.ERROR: "contract_error",
    }.get(result.ended_by, "contract_end")


__all__ = ["ContractDriverFailure", "run_contract_driver_path"]
