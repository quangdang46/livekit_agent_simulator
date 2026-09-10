"""Canonical Caller Contract data model (P0-1).

Shared primitives for the caller-architecture rewrite: BehaviorContract locks
WHAT, CandidateUtterance is the generator claim, ValidationResult is the
validator verdict, ObservedAct is the verifier evidence. This module also owns
the shared FailureReason / EndedBy enums and the is_current() staleness
predicate so downstream modules import them instead of redefining.

Single source of truth for both lks (Python) and lksr (Rust, via the JSON
Schema export + golden fixtures under tests/fixtures/caller_contract/).

Absolute invariants enforced downstream (see epic):
  1. unvalidated utterance never reaches LiveKit.
  2. stale generations (behavior_id/turn_id/generation_id mismatch) never execute.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    """Validator verdict. UNKNOWN rejects, ERROR fails the run (never PASS)."""

    VALID = "VALID"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class FailureReason(str, Enum):
    """Canonical failure taxonomy. Defined once here; all modules import."""

    CALLER_BEHAVIOR_VIOLATION = "CALLER_BEHAVIOR_VIOLATION"
    LANGUAGE_GENERATION_ERROR = "LANGUAGE_GENERATION_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    TTS_ERROR = "TTS_ERROR"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    BEHAVIOR_TIMEOUT = "BEHAVIOR_TIMEOUT"


class EndedBy(str, Enum):
    """Canonical call-end attribution. Replaces ad-hoc string comparisons."""

    SCENARIO = "scenario"
    CALLER = "caller"
    AGENT = "agent"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    ERROR = "error"


class EvaluatorVerdict(str, Enum):
    """Behavior Evaluator verdict (agent side). Uppercase by convention."""

    SATISFIED = "SATISFIED"
    PARTIAL = "PARTIAL"
    NOT_SATISFIED = "NOT_SATISFIED"


@dataclass(frozen=True)
class GenerationIdentity:
    """Version triple carried by every candidate/audio/action.

    context_version tracks ConversationContext freshness and is NOT part of
    the staleness triple; is_current() compares behavior/turn/generation only.
    """

    behavior_id: str
    turn_id: int
    generation_id: int
    context_version: int = 0

    def validate(self) -> None:
        if not self.behavior_id or not str(self.behavior_id).strip():
            raise ValueError("behavior_id must be a non-empty string")
        if self.turn_id < 0:
            raise ValueError("turn_id must be >= 0")
        if self.generation_id < 0:
            raise ValueError("generation_id must be >= 0")
        if self.context_version < 0:
            raise ValueError("context_version must be >= 0")


def is_current(identity: GenerationIdentity, current: GenerationIdentity) -> bool:
    """True iff the triple (behavior/turn/generation) still matches current state."""
    return (
        identity.behavior_id == current.behavior_id
        and identity.turn_id == current.turn_id
        and identity.generation_id == current.generation_id
    )


@dataclass
class ContractConstraints:
    """Action-space constraints. Generic keys; no business vocabulary in core."""

    max_turns: int = 3
    max_budget: float | None = None
    max_words: int | None = None
    max_duration_s: float | None = None
    forbidden_intents: list[str] = field(default_factory=list)
    must_not: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if self.max_turns < 1:
            raise ValueError("max_turns must be >= 1")
        if self.max_words is not None and self.max_words < 1:
            raise ValueError("max_words must be >= 1")
        if self.max_duration_s is not None and self.max_duration_s <= 0:
            raise ValueError("max_duration_s must be > 0")


@dataclass
class BehaviorContract:
    """WHAT the caller may do. behavior/target/slots are caller-defined keys."""

    behavior: str
    target: str | None = None
    constraints: ContractConstraints = field(default_factory=ContractConstraints)

    def validate(self) -> None:
        if not self.behavior or not str(self.behavior).strip():
            raise ValueError("behavior must be a non-empty string")
        self.constraints.validate()


@dataclass
class CandidateUtterance:
    """Generator claim. act/target/slots are claims, NOT evidence.

    Evidence comes from the semantic verifier re-deriving ObservedAct from
    the utterance text alone (never trusting these fields).
    """

    act: str
    target: str | None
    slots: dict[str, Any]
    utterance: str
    identity: GenerationIdentity

    def validate(self) -> None:
        if not self.act or not str(self.act).strip():
            raise ValueError("act must be a non-empty string")
        if not self.utterance or not str(self.utterance).strip():
            raise ValueError("utterance must be a non-empty string")
        if not isinstance(self.slots, dict):
            raise ValueError("slots must be a dict")
        self.identity.validate()


@dataclass
class ObservedAct:
    """Verifier evidence: what the utterance actually performs.

    Multi-label: primary act plus every other detected act, so nested
    forbidden intents inside otherwise-valid sentences are caught.
    """

    act: str
    target: str | None
    slots: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    all_acts: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.act or not str(self.act).strip():
            raise ValueError("act must be a non-empty string")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0.0, 1.0]")


@dataclass(frozen=True)
class ValidationResult:
    """Validator verdict. UNKNOWN -> reject, ERROR -> fail the run."""

    verdict: Verdict
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def is_valid(self) -> bool:
        return self.verdict == Verdict.VALID


def contract_json_schema() -> dict[str, Any]:
    """Language-neutral JSON Schema for BehaviorContract + CandidateUtterance.

    Exported so lksr (Rust) consumes fixtures without importing Python.
    """
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "CallerContract",
        "type": "object",
        "definitions": {
            "constraints": {
                "type": "object",
                "properties": {
                    "max_turns": {"type": "integer", "minimum": 1},
                    "max_budget": {"type": ["number", "null"]},
                    "max_words": {"type": ["integer", "null"], "minimum": 1},
                    "max_duration_s": {"type": ["number", "null"], "exclusiveMinimum": 0},
                    "forbidden_intents": {"type": "array", "items": {"type": "string"}},
                    "must_not": {"type": "array", "items": {"type": "string"}},
                },
            },
            "identity": {
                "type": "object",
                "required": ["behavior_id", "turn_id", "generation_id"],
                "properties": {
                    "behavior_id": {"type": "string", "minLength": 1},
                    "turn_id": {"type": "integer", "minimum": 0},
                    "generation_id": {"type": "integer", "minimum": 0},
                    "context_version": {"type": "integer", "minimum": 0},
                },
            },
        },
        "properties": {
            "behavior": {"type": "string", "minLength": 1},
            "target": {"type": ["string", "null"]},
            "constraints": {"$ref": "#/definitions/constraints"},
            "candidate": {
                "type": "object",
                "required": ["act", "utterance", "identity"],
                "properties": {
                    "act": {"type": "string", "minLength": 1},
                    "target": {"type": ["string", "null"]},
                    "slots": {"type": "object"},
                    "utterance": {"type": "string", "minLength": 1},
                    "identity": {"$ref": "#/definitions/identity"},
                },
            },
        },
    }


def to_dict(obj: Any) -> Any:
    """Dataclass/enum-aware conversion for fixture serialization."""
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    return obj


__all__ = [
    "BehaviorContract",
    "CandidateUtterance",
    "ContractConstraints",
    "EndedBy",
    "EvaluatorVerdict",
    "FailureReason",
    "GenerationIdentity",
    "ObservedAct",
    "ValidationResult",
    "Verdict",
    "contract_json_schema",
    "is_current",
    "to_dict",
]
