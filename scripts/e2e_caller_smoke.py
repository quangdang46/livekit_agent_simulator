#!/usr/bin/env python3
"""E2E smoke test for the new caller-architecture pipeline (P0-1..P0-7).

Runs a full MOCKED loop (no LiveKit credentials, no network) through:

    Scenario -> Behavior DSL -> Orchestrator (turn gate) -> AI Language
    Adapter (mocked backend) -> Caller Contract Validator (deterministic +
    semantic) -> Interaction Planner -> (simulated) TTS/publish -> Behavior
    Evaluator -> loop.

Exits non-zero with a clear log line on any failure, so this can be wired
into CI without a live agent.

--------------------------------------------------------------------------
Live-smoke recipe (documented, not run by this script — requires a real
target repo with .agent-sim/config.yaml and a running agent):

    lks preflight --root /path/to/target-repo
    lks execute <scenario-id> --root /path/to/target-repo

See docs/smoke-test.md for the full first-end-to-end-run walkthrough.
This script intentionally does not attempt that path: it has no
credentials, no LiveKit room, and no target repo to point at in CI.
--------------------------------------------------------------------------
"""

from __future__ import annotations

import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("e2e_caller_smoke")


def _mocked_backend_factory():
    """A LanguageBackendProtocol stub that returns one scripted, in-contract
    negotiation reply — enough to exercise the full pipeline without a real
    AI call."""

    class _MockedBackend:
        def generate(self, context):
            behavior = context["current_behavior"]["act"]
            target = context["current_behavior"]["target"]
            if behavior == "negotiate" and target == "price":
                return {
                    "act": "negotiate",
                    "target": "price",
                    "slots": {"max_budget": 30000},
                    "utterance": "Would you be able to come down to $30,000?",
                }
            if behavior == "ask":
                return {
                    "act": "ask",
                    "target": target,
                    "slots": {},
                    "utterance": "Could you tell me the asking price?",
                }
            # Fallback: still a real sentence so the semantic verifier has
            # something to classify (a bare "(behavior target)" tuple is not
            # natural language and would correctly be rejected as ambiguous).
            return {"act": behavior, "target": target, "slots": {}, "utterance": f"Let's talk about {behavior}."}

    return _MockedBackend()


def run_smoke() -> bool:
    from livekit_agent_simulator.caller_contract.dsl import parse_steps
    from livekit_agent_simulator.caller_contract.interaction_planner import CallerInteractionPlanner
    from livekit_agent_simulator.caller_contract.language_adapter import (
        AILanguageAdapter,
        build_context,
        should_invoke_adapter,
    )
    from livekit_agent_simulator.caller_contract.orchestrator import BehaviorEvaluator, Orchestrator
    from livekit_agent_simulator.caller_contract.semantic import RuleBasedSemanticVerifier
    from livekit_agent_simulator.caller_contract.validator import ContractValidator, validate_with_retry

    scenario_steps = [
        {"say": "Hi, I'm calling about the 2022 Honda CR-V."},
        {"do": "ask"},
        {
            "do": {
                "behavior": "negotiate",
                "target": "price",
                "constraints": {"max_turns": 3, "max_budget": 30000, "forbidden_intents": ["financing"]},
            }
        },
        {"say": "Thanks, bye."},
        {"end": True},
    ]

    logger.info("Parsing scenario (%d steps)...", len(scenario_steps))
    actions = parse_steps(scenario_steps, file="e2e_caller_smoke")

    orch = Orchestrator()
    validator = ContractValidator(semantic_verifier=RuleBasedSemanticVerifier())
    adapter = AILanguageAdapter(backend=_mocked_backend_factory())
    planner = CallerInteractionPlanner()
    evaluator = BehaviorEvaluator()

    for action in actions:
        orch.advance_caller_turn()  # every action, including say, crosses the turn gate

        if action.kind == "say":
            orch.gate_say(has_crossed_turn_gate=True)
            plan = planner.plan_speak(action.say_text, action.interaction)
            logger.info("say -> SPEAK tokens=%s", plan.tokens)
            continue

        if action.kind == "do":
            assert action.contract is not None
            orch.start_behavior()
            behavior_id = orch.current_identity().behavior_id
            logger.info("do[%s] behavior=%s target=%s", behavior_id, action.contract.behavior, action.contract.target)

            if not should_invoke_adapter(action.bypasses_ai_and_validator):
                logger.error("FAIL: do action unexpectedly bypassed the AI adapter")
                return False

            context = build_context(
                contract=action.contract, turn=0, agent_latest=None, relevant_facts=[], recent_turns=[]
            )

            def _generate():
                identity = orch.new_generation()
                return adapter.generate_candidate(action.contract, context, identity)

            outcome = validate_with_retry(validator, action.contract, _generate, max_retries=2)
            if not outcome.result.is_valid():
                logger.error(
                    "FAIL: behavior=%s produced no valid candidate after %d attempt(s): %s",
                    action.contract.behavior,
                    outcome.attempts,
                    outcome.result.details,
                )
                return False

            plan = planner.plan_speak(outcome.candidate.utterance, action.interaction)
            logger.info("do -> SPEAK tokens=%s (validated)", plan.tokens)

            simulated_agent_reply = "I could do $31,000."
            verdict = evaluator.evaluate(action.contract, simulated_agent_reply)
            logger.info("agent reply=%r -> BehaviorEvaluator verdict=%s", simulated_agent_reply, verdict)
            continue

        if action.kind == "end":
            logger.info("end -> call terminated")
            continue

        logger.info("action kind=%s (no-op in this smoke script)", action.kind)

    logger.info("PASS: full mocked loop completed with no unvalidated utterance reaching the agent")
    return True


def main() -> int:
    ok = run_smoke()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
