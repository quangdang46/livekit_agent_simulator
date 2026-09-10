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
    """Tier-3 (LLM judge) when ``judge:`` is configured and ready; otherwise
    the Tier-1 rule-based baseline — see module docstring."""
    sim_api_key = getattr(cfg.simulator, "api_key", None)
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
    pcm = synthesize_pcm16_mono(text, rate=TARGET_RATE)
    return pcm or b""


async def run_contract_driver_path(
    scenario: "Scenario",
    run: "SimulatorSpec",
    observer: "Observer",
    bridge: "CallerBridge",
    writer: "EventWriter",
    cfg: Any,
    *,
    drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
) -> str:
    """Drive ``scenario.caller_actions`` end to end. Returns the end_reason
    string on success; raises ``ContractDriverFailure`` on any failure.

    ``drain_timeout_s`` bounds how long ``PublishSink.publish()`` waits for
    caller audio to finish playing out before the driver advances to the
    next action — exposed (not hardcoded) so tests can exercise the
    stuck-mixer -> TRANSPORT_ERROR path without a slow real timeout.
    """
    _ = run  # reserved: max_turns/timeout_s already live on each contract
    orch = Orchestrator()
    validator = ContractValidator(semantic_verifier=_build_semantic_verifier(cfg))
    adapter = AILanguageAdapter(backend=_build_text_backend(cfg))
    driver = ContractCallerDriver(
        orchestrator=orch,
        validator=validator,
        adapter=adapter,
        synthesize=_synthesize,
    )
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

    # Greeting wait reuses the scenario's own timeout (run_spec.timeout_s):
    # no second timeout semantics — an agent that never greets fails the
    # same way a run that never progresses does.
    # NOTE (migration bridge): silent_mode is still read from the legacy
    # persona speech_conditions via behavior_compile. The contract-native
    # end-state is a scenario/execution-level property; until that exists
    # this stays a deliberate bridge, not a permanent persona dependency.
    greeting_timeout_s = float(getattr(run_spec, "timeout_s", 30.0) or 30.0)

    result = await driver.run(
        scenario.caller_actions,
        sink,
        agent,
        emit=_emit,
        first_speaker=first_speaker,
        silent_mode=silent_mode_enabled(persona),
        greeting_timeout_s=greeting_timeout_s,
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
