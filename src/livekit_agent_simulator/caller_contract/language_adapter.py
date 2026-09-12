"""P0-5: AI Language Adapter — the HOW layer, REQUIRED for `do:` steps.

One stateless Realtime call per adaptive turn (report §19.3 "Approach B"):
this adapter builds a minimal structured ConversationContext, calls the
backend exactly once (plus bounded retries on failure), and returns a
CandidateUtterance. It holds no conversation memory between turns — all
memory lives in the Orchestrator state (P0-4) that feeds build_context().

`say:` steps NEVER reach this module (see should_invoke_adapter()) — they
are handled entirely by the DSL/Orchestrator/TTS path, bypassing AI.

Failure handling: backend timeout/429/malformed output triggers a bounded
retry, then FailureReason.LANGUAGE_GENERATION_ERROR (imported from P0-1,
never redefined here) — never a "just say something" fallback (report
§13.5/§28.3(8)).

Provider/model selection and credentials belong to runtime config (the
caller of this module), never to the scenario file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from . import (
    BehaviorContract,
    CandidateUtterance,
    FailureReason,
    GenerationIdentity,
)

# Recent-turns cap: the context passed to the backend is a MINIMAL
# structured JSON, never a full transcript dump (report §19.2).
DEFAULT_RECENT_TURNS_CAP = 6


class LanguageGenerationError(RuntimeError):
    """Raised when the backend fails after bounded retries. Always carries
    FailureReason.LANGUAGE_GENERATION_ERROR — callers must record this
    reason, never silently fall back to speaking an ungenerated line."""

    reason: FailureReason = FailureReason.LANGUAGE_GENERATION_ERROR

    def __init__(self, message: str, *, last_error: Exception | None = None) -> None:
        super().__init__(message)
        self.last_error = last_error


class LanguageBackendProtocol(Protocol):
    """One stateless request/response call — not a persistent streaming
    session. The backend receives only the structured context built by
    build_context() and returns a raw dict; this adapter validates and
    wraps it into a CandidateUtterance."""

    def generate(self, context: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class Turn:
    speaker: str  # "caller" | "agent"
    text: str


def build_context(
    *,
    contract: BehaviorContract,
    turn: int,
    agent_latest: str | None,
    relevant_facts: list[str],
    recent_turns: list[Turn],
    recent_turns_cap: int = DEFAULT_RECENT_TURNS_CAP,
) -> dict[str, Any]:
    """Minimal structured ConversationContext (report §19.2). Never a full
    transcript dump — recent_turns is capped at `recent_turns_cap`."""
    capped_turns = recent_turns[-recent_turns_cap:]
    return {
        "current_behavior": {
            "act": contract.behavior,
            "target": contract.target,
            "max_budget": contract.constraints.max_budget,
            "turn": turn,
            "max_turns": contract.constraints.max_turns,
        },
        "agent_latest": {"text": agent_latest} if agent_latest else None,
        "relevant_facts": list(relevant_facts),
        "recent_turns": [{"speaker": t.speaker, "text": t.text} for t in capped_turns],
    }


_REQUIRED_CANDIDATE_KEYS = ("act", "utterance")


def _parse_backend_response(raw: dict[str, Any], identity: GenerationIdentity) -> CandidateUtterance:
    if not isinstance(raw, dict):
        raise ValueError(f"backend response must be a dict, got {type(raw).__name__}")
    missing = [k for k in _REQUIRED_CANDIDATE_KEYS if k not in raw or not raw[k]]
    if missing:
        raise ValueError(f"backend response missing required field(s): {missing}")
    slots = raw.get("slots", {})
    if not isinstance(slots, dict):
        raise ValueError("backend response 'slots' must be a dict")
    candidate = CandidateUtterance(
        act=raw["act"],
        target=raw.get("target"),
        slots=slots,
        utterance=raw["utterance"],
        identity=identity,
    )
    candidate.validate()
    return candidate


@dataclass
class AILanguageAdapter:
    """HOW layer. Provider/model live in `backend`; this class holds no
    credentials and no cross-turn memory."""

    backend: LanguageBackendProtocol
    max_retries: int = 1

    def generate_candidate(
        self,
        contract: BehaviorContract,
        context: dict[str, Any],
        identity: GenerationIdentity,
    ) -> CandidateUtterance:
        """Exactly one logical adaptive-turn request, with bounded retries
        on failure. Raises LanguageGenerationError (never falls back to an
        unvalidated fabricated line) once retries are exhausted."""
        last_error: Exception | None = None
        for _attempt in range(self.max_retries + 1):
            try:
                raw = self.backend.generate(context)
                return _parse_backend_response(raw, identity)
            except LanguageGenerationError:
                # Already the terminal wrapper (re-raised retry-exhaustion
                # from a nested adapter call) — never wrap it again, and
                # never retry a retry-exhaustion as if it were transient.
                raise
            except Exception as exc:  # noqa: BLE001 — any backend failure is bounded-retried, then wrapped
                last_error = exc
                continue
        raise LanguageGenerationError(
            f"language backend failed after {self.max_retries + 1} attempt(s): {last_error}",
            last_error=last_error,
        )


def should_invoke_adapter(bypasses_ai_and_validator: bool) -> bool:
    """`say:` actions (bypasses_ai_and_validator=True, see dsl.CallerAction)
    must NEVER reach this adapter — only `do:` actions do."""
    return not bypasses_ai_and_validator


__all__ = [
    "DEFAULT_RECENT_TURNS_CAP",
    "AILanguageAdapter",
    "LanguageBackendProtocol",
    "LanguageGenerationError",
    "Turn",
    "build_context",
    "should_invoke_adapter",
]
