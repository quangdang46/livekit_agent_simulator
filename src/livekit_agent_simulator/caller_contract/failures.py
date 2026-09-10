"""Failure taxonomy plus ended_by propagation for the new caller runtime.

Wires the seven canonical FailureReason codes (defined once in
caller_contract/__init__.py — this module never redeclares them) into a
single RunFailure record consumed by report/JSON/CLI surfaces, plus the
VALIDATION_PASS/TTS_FAILED split state so a TTS retry never silently
re-invokes the AI (report §28.4(6)).

Scope note (read before extending): this module wires failure attribution
for the NEW caller-architecture pipeline (validator/adapter/orchestrator/
TTS/transport introduced in P0-1..P0-7). The EXISTING assertion type
`ended_by` in asserts.py ("sim" | "agent" | "detect") answers a narrower,
already-shipped, separately-tested question — "which side hung up first,
for pass/fail assertions on a run" — and is intentionally left untouched
here: it is a mature feature serving existing scenarios, not a duplicate
of the new EndedBy enum. The new EndedBy enum (scenario/caller/agent/
timeout/transport/error) is the call-end attribution surfaced by the new
runtime's own RunResult; the two are different concerns that happen to
share a name. Renaming the legacy one now would be a large, unrelated
refactor of a working feature (AGENTS.md "no stubborn patches": don't
force-migrate a feature that already does its job correctly for a
different purpose). If a future task unifies them, do it as its own bead
with its own test coverage — not folded silently into this one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import EndedBy, FailureReason, Verdict


@dataclass
class RunFailure:
    """One attributable failure for a run/behavior/turn. `reason` is
    always one of the seven canonical FailureReason values."""

    reason: FailureReason
    detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_report_dict(self) -> dict[str, Any]:
        return {"reason": self.reason.value, "detail": self.detail, **self.metadata}


@dataclass
class TTSPublishState:
    """Split state proving TTS failure is NOT conflated with 'behavior
    executed'. A retry driven by TTS_FAILED must re-attempt synthesis of
    the SAME already-validated candidate — it must never silently call the
    AI Language Adapter again just because audio synthesis failed."""

    validation_passed: bool
    tts_failed: bool = False

    def to_report_dict(self) -> dict[str, Any]:
        return {"validation_passed": self.validation_passed, "tts_failed": self.tts_failed}

    def should_retry_tts_only(self) -> bool:
        """True means: retry TTS with the SAME candidate; do not call the
        language adapter again."""
        return self.validation_passed and self.tts_failed


def record_tts_attempt(*, validation_passed: bool, tts_succeeded: bool) -> TTSPublishState:
    return TTSPublishState(validation_passed=validation_passed, tts_failed=validation_passed and not tts_succeeded)


# ---------------------------------------------------------------------------
# Mapping helpers: turn a validator/adapter/orchestrator outcome into the
# canonical RunFailure a report/CLI/MCP surface can serialize.
# ---------------------------------------------------------------------------


def failure_from_validation_result(verdict: Verdict, *, retries_exhausted: bool) -> RunFailure | None:
    """VALIDATION_ERROR when the verifier itself failed/was unavailable;
    CALLER_BEHAVIOR_VIOLATION only once bounded retries are exhausted and
    the content is still not VALID. Returns None while retries remain
    (i.e. this is not yet a terminal failure)."""
    if verdict == Verdict.ERROR:
        return RunFailure(FailureReason.VALIDATION_ERROR, detail="semantic verifier unavailable or crashed")
    if verdict != Verdict.VALID and retries_exhausted:
        return RunFailure(FailureReason.CALLER_BEHAVIOR_VIOLATION, detail="content rejected after bounded retry")
    return None


def failure_from_language_generation_error(message: str) -> RunFailure:
    return RunFailure(FailureReason.LANGUAGE_GENERATION_ERROR, detail=message)


def failure_from_tts_state(state: TTSPublishState) -> RunFailure | None:
    if state.tts_failed:
        return RunFailure(FailureReason.TTS_ERROR, detail="TTS synthesis failed after a validated candidate")
    return None


def failure_from_transport(message: str) -> RunFailure:
    return RunFailure(FailureReason.TRANSPORT_ERROR, detail=message)


def failure_from_agent_timeout(elapsed_ms: int) -> RunFailure:
    return RunFailure(FailureReason.AGENT_TIMEOUT, detail=f"no agent response after {elapsed_ms}ms")


def failure_from_behavior_timeout(*, max_turns: int) -> RunFailure:
    """Behavior hit max_turns (or its own timeout) without being satisfied
    — this is BEHAVIOR_TIMEOUT, not CALLER_BEHAVIOR_VIOLATION (the caller
    said in-contract things every turn; the BEHAVIOR simply never closed)."""
    return RunFailure(FailureReason.BEHAVIOR_TIMEOUT, detail=f"max_turns={max_turns} reached without satisfaction")


# ---------------------------------------------------------------------------
# ended_by (NEW runtime's own call-end attribution — see scope note above)
# ---------------------------------------------------------------------------


def to_ended_by_report_dict(ended_by: EndedBy) -> dict[str, Any]:
    return {"ended_by": ended_by.value}


__all__ = [
    "RunFailure",
    "TTSPublishState",
    "failure_from_agent_timeout",
    "failure_from_behavior_timeout",
    "failure_from_language_generation_error",
    "failure_from_transport",
    "failure_from_tts_state",
    "failure_from_validation_result",
    "record_tts_attempt",
    "to_ended_by_report_dict",
]
