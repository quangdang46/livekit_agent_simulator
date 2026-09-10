"""Live assembly of ContractCallerDriver for a real LiveKit run.

Called from run_orchestrator.py ONLY when ``scenario.caller_actions`` is
non-empty — the single execution path for such scenarios (no legacy
persona+ScriptRunner/freestyle-bridge code runs alongside it; see
docs/contract-caller-wiring.md). Legacy scenarios (empty caller_actions)
never reach this module.

Text backend selection mirrors ``cfg.simulator.provider`` (google|openai) so
the same config key already used for the legacy bridge selects the do:
generation backend too — no new config surface for this slice.

Raises ``ContractDriverFailure`` (never returns normally) when the driver
reports a failure, so run_scenario_instance's existing ``except Exception``
handler marks the run failed exactly like any other hard error — no separate
failure-handling branch needed there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..audio.sapi_tts import TARGET_RATE, synthesize_pcm16_mono
from . import EndedBy
from .agent_wait import ObserverAgentWait
from .driver import ContractCallerDriver, DriverResult
from .language_adapter import AILanguageAdapter
from .orchestrator import Orchestrator
from .publish_sink import DEFAULT_DRAIN_TIMEOUT_S, BridgePublishSink
from .semantic import RuleBasedSemanticVerifier
from .text_backends import GeminiTextBackend, OpenAITextBackend
from .validator import ContractValidator

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
    validator = ContractValidator(semantic_verifier=RuleBasedSemanticVerifier())
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

    result = await driver.run(scenario.caller_actions, sink, agent, emit=_emit)
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
