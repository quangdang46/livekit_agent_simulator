"""E2E hard-boundary proof: an out-of-contract utterance never reaches the agent.

The epic invariant is "lock the intent, free the language, never let an
unvalidated utterance reach the agent". Unit tests cover each layer; this
file drives a WHOLE multi-behavior call through the real
``ContractCallerDriver`` with an adversarial language backend and asserts the
property at the delivery boundary:

  - every published utterance was validator-approved for its own behavior;
  - a backend that keeps drifting (financing under a price contract, a
    goodbye under an arrange_visit contract) never gets its text published,
    no matter how plausible it sounds;
  - exhausting the bounded retry stops the run with
    CALLER_BEHAVIOR_VIOLATION, never a "speak something anyway" fallback.

No network, no LiveKit: the backend is scripted and the sink is in-memory.
"""

from __future__ import annotations

from livekit_agent_simulator.caller_contract import EndedBy, FailureReason
from livekit_agent_simulator.caller_contract.driver import ContractCallerDriver
from livekit_agent_simulator.caller_contract.dsl import parse_steps
from livekit_agent_simulator.caller_contract.language_adapter import AILanguageAdapter
from livekit_agent_simulator.caller_contract.orchestrator import Orchestrator
from livekit_agent_simulator.caller_contract.semantic import RuleBasedSemanticVerifier
from livekit_agent_simulator.caller_contract.validator import ContractValidator


class _DriftingBackend:
    """Language backend that drifts for the first ``drift_attempts`` attempts
    OF A GIVEN BEHAVIOR, then produces an in-contract line.

    Keyed by behavior so the drift test cannot accidentally poison the
    scenario's earlier ``ask`` turn: only ``negotiate`` ever drifts here.
    ``drift_lines=[]`` makes every negotiate attempt drift, which is the
    "always off-contract" case.
    """

    def __init__(self, drift_lines: list[str], drift_attempts: int = 1):
        self._drift = list(drift_lines)
        self._drift_attempts = drift_attempts
        self._seen: dict[str, int] = {}
        self.calls: list[dict] = []

    def generate(self, context):
        behavior = context["current_behavior"]["act"]
        target = context["current_behavior"]["target"]
        index = self._seen.get(behavior, 0)
        self._seen[behavior] = index + 1

        if (
            behavior == "negotiate"
            and index < self._drift_attempts
            and self._drift
        ):
            payload = {
                "act": "negotiate",
                "target": "price",
                "slots": {},
                "utterance": self._drift[index % len(self._drift)],
            }
        elif behavior == "negotiate":
            payload = {
                "act": "negotiate",
                "target": "price",
                "slots": {"max_budget": 30000},
                "utterance": "Would you be able to come down to $30,000?",
            }
        elif behavior == "ask":
            payload = {
                "act": "ask",
                "target": target,
                "slots": {},
                "utterance": "What is the asking price?",
            }
        else:
            payload = {
                "act": behavior,
                "target": target,
                "slots": {},
                "utterance": "Let's go ahead with that.",
            }
        self.calls.append(payload)
        return payload


class _RecordingSink:
    """PublishSink stand-in: records every utterance that actually shipped."""

    def __init__(self, orch: Orchestrator):
        self.orch = orch
        self.labels: list[str] = []
        self.payloads: list[bytes] = []

    async def publish(self, pcm, identity, *, label, gain=1.0):
        if self.orch.is_stale(identity):
            return False
        self.labels.append(label)
        self.payloads.append(pcm)
        return True


class _ScriptedAgent:
    def __init__(self, replies: list[str]):
        self._replies = list(replies)

    async def wait_agent_turn(self, *, timeout_s: float = 30.0):
        return self._replies.pop(0) if self._replies else "Sure, that works for me."


def _driver(backend):
    orch = Orchestrator()
    driver = ContractCallerDriver(
        orchestrator=orch,
        validator=ContractValidator(semantic_verifier=RuleBasedSemanticVerifier()),
        adapter=AILanguageAdapter(backend=backend),
        synthesize=lambda text: f"PCM({text})".encode(),
    )
    return driver, orch


_MULTI_BEHAVIOR_STEPS = [
    {"say": "Hi, I'm calling about the 2022 Honda CR-V."},
    {"do": {"behavior": "ask", "target": "price", "constraints": {"max_turns": 2}}},
    {
        "do": {
            "behavior": "negotiate",
            "target": "price",
            "constraints": {
                "max_turns": 3,
                "max_budget": 30000,
                "forbidden_intents": ["financing", "vehicle_change"],
            },
        }
    },
    {"say": "Thanks, bye."},
    {"end": True},
]

# Two plausible lines that do NOT belong to the price-negotiation contract
# they are generated for. Neither contains a lexical forbidden keyword
# ("financing", "loan", "payment plan", "instead", ...), so the deterministic
# lexical guard cannot see them — the semantic layer is the only thing
# standing between these lines and the agent.
_DRIFT_UTTERANCES = [
    # financing drift, paraphrased
    "Could I spread this over a couple of years and pay it down gradually?",
    # vehicle_change drift — a different car, mid-negotiation
    "Sorry, I changed my mind about the model. Could you show me something else?",
]


async def test_drifted_utterances_are_never_published():
    """The caller's attempt to change topic is rejected and retried; only the
    in-contract sentence reaches the sink."""
    # Drift for the first 2 negotiate attempts (one per drift line), then
    # recover — so both drifts are exercised and the run still completes.
    backend = _DriftingBackend(_DRIFT_UTTERANCES, drift_attempts=2)
    driver, orch = _driver(backend)
    sink = _RecordingSink(orch)
    actions = parse_steps(_MULTI_BEHAVIOR_STEPS, file="e2e_hard_boundary")

    result = await driver.run(
        actions, sink, _ScriptedAgent(["We're asking $32,000.", "I can do $30,000."])
    )

    assert result.failure is None
    assert result.ended_by == EndedBy.SCENARIO

    shipped = [p.decode() for p in sink.payloads]
    # Every drifted line must be absent from EVERY published payload.
    assert shipped, "nothing was published; the assertion below would be vacuous"
    for drifted in _DRIFT_UTTERANCES:
        assert all(drifted not in text for text in shipped), (
            f"out-of-contract utterance reached the agent: {drifted!r} in {shipped}"
        )
        # ...and it was actually attempted (otherwise the test proves nothing).
        assert any(drifted == call["utterance"] for call in backend.calls)


async def test_exhausted_retry_fails_loudly_never_speaks_anyway():
    """An always-drifting backend must stop the run with
    CALLER_BEHAVIOR_VIOLATION — there is no "say something anyway" fallback."""
    # 99 drift attempts: more than any max_retries budget in this scenario.
    backend = _DriftingBackend(_DRIFT_UTTERANCES, drift_attempts=99)
    driver, orch = _driver(backend)
    sink = _RecordingSink(orch)
    actions = parse_steps(_MULTI_BEHAVIOR_STEPS, file="e2e_hard_boundary")

    result = await driver.run(
        actions, sink, _ScriptedAgent(["We're asking $32,000.", "I can do $30,000."])
    )

    assert result.failure is not None
    assert result.failure.reason == FailureReason.CALLER_BEHAVIOR_VIOLATION
    # Nothing from the drifting behavior was published. Only do: publishes
    # can carry AI-generated text — say: steps are authored, never validated.
    do_payloads = [
        p.decode()
        for label, p in zip(sink.labels, sink.payloads)
        if label.startswith("do:")
    ]
    attempted = [call["utterance"] for call in backend.calls if call["act"] == "negotiate"]
    assert attempted, "the drifting behavior never reached the backend"
    for drifted in attempted:
        assert all(drifted not in text for text in do_payloads), (
            f"out-of-contract utterance reached the agent: {drifted!r} in {do_payloads}"
        )


async def test_every_published_utterance_is_contract_approved():
    """Positive control: re-validate every shipped utterance against the
    behavior that produced it — if any shipped line would fail the validator,
    the delivery boundary is leaky."""
    backend = _DriftingBackend(_DRIFT_UTTERANCES, drift_attempts=2)
    driver, orch = _driver(backend)
    sink = _RecordingSink(orch)
    actions = parse_steps(_MULTI_BEHAVIOR_STEPS, file="e2e_hard_boundary")

    result = await driver.run(
        actions, sink, _ScriptedAgent(["We're asking $32,000.", "I can do $30,000."])
    )
    assert result.failure is None

    validator = ContractValidator(semantic_verifier=RuleBasedSemanticVerifier())
    contracts = [a.contract for a in actions if a.kind == "do" and a.contract is not None]
    assert contracts, "scenario must exercise at least one do: behavior"

    checked = 0
    for label, payload in zip(sink.labels, sink.payloads):
        if not label.startswith("do:"):
            continue  # say:/interrupt: steps are authored text, never AI-generated
        behavior = label.split(":", 1)[1]
        # The driver synthesizes exactly the validated string; the recording
        # synthesizer wraps it so the text is recoverable here.
        text = payload.decode()
        assert text.startswith("PCM(") and text.endswith(")")
        utterance = text[4:-1]

        matched = [
            contract
            for contract in contracts
            if contract.behavior == behavior
            and validator.validate(_candidate_for(utterance, contract), contract).is_valid()
        ]
        assert matched, (
            f"published utterance {utterance!r} is not valid under its own "
            f"{behavior!r} contract — the hard boundary leaked"
        )
        checked += 1

    assert checked > 0, "no do: turn was published; the control proved nothing"


def _candidate_for(text: str, contract):
    from livekit_agent_simulator.caller_contract import CandidateUtterance, GenerationIdentity

    return CandidateUtterance(
        act=contract.behavior,
        target=contract.target,
        slots={},
        utterance=text,
        identity=GenerationIdentity(behavior_id="verify", turn_id=0, generation_id=0),
    )
