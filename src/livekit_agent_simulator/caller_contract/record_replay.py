"""P0-7: Record and Replay for the new caller-architecture runtime.

Records EVERY candidate generation attempt — including rejections, not
just the final passing one (report §28.5(22)) — so a failing run can be
replayed to the identical failure without re-invoking any AI backend.

Record schema is versioned (RECORD_FORMAT_VERSION) so future fields never
break old replay files.

Scope note: this module provides the record/replay PRIMITIVES (RunRecord,
Recorder, ReplayLanguageBackend) used together with AILanguageAdapter
(P0-5). Wiring `--record`/`--replay` CLI flags onto the existing `lks
execute` / MCP execute path (run_orchestrator.py) is a separate,
substantially larger integration task against the live run loop and is
intentionally out of scope here — these primitives are what that future
integration will call into, and are independently tested against a
mocked/no-network language backend below.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import CandidateUtterance, GenerationIdentity, ValidationResult, Verdict, to_dict
from .language_adapter import LanguageBackendProtocol

RECORD_FORMAT_VERSION = 2

# v1 records (candidate/verdict/reason/retry_index only) still read: the
# semantic evidence + outcome fields default to None (see from_dict).
RECORD_FORMAT_V1 = 1


class ReplayMismatchError(RuntimeError):
    """Raised when a replayed candidate's validator verdict does not match
    the verdict recorded during the original run — this must fail loudly,
    never silently diverge (report §28.5(22))."""


@dataclass
class RecordedAttempt:
    """One generate+validate attempt for a single turn, success or not.

    v2 adds the semantic verifier's observed evidence (act/target/
    confidence/all_acts as classified at record time) plus the run outcome
    (failure reason + ended_by + behavior/turn counters at record end).
    Replay asserts BOTH the validator verdict and the observed evidence,
    so an LLM semantic verifier is never re-invoked during replay.
    """

    candidate: dict[str, Any]
    verdict: str
    reason: str | None
    retry_index: int
    observed: dict[str, Any] | None = None
    outcome_failure: str | None = None
    outcome_ended_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate,
            "verdict": self.verdict,
            "reason": self.reason,
            "retry_index": self.retry_index,
            "observed": self.observed,
            "outcome_failure": self.outcome_failure,
            "outcome_ended_by": self.outcome_ended_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecordedAttempt":
        return cls(
            candidate=data["candidate"],
            verdict=data["verdict"],
            reason=data.get("reason"),
            retry_index=data["retry_index"],
            observed=data.get("observed"),
            outcome_failure=data.get("outcome_failure"),
            outcome_ended_by=data.get("outcome_ended_by"),
        )


@dataclass
class RunRecord:
    """Versioned record of an entire adaptive run: every attempt for every
    turn, in order, including rejections."""

    scenario_id: str
    seed: int
    attempts: list[RecordedAttempt] = field(default_factory=list)
    format_version: int = RECORD_FORMAT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "scenario_id": self.scenario_id,
            "seed": self.seed,
            "attempts": [a.to_dict() for a in self.attempts],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def write(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunRecord":
        version = data.get("format_version")
        if version not in (RECORD_FORMAT_V1, RECORD_FORMAT_VERSION):
            raise ValueError(
                f"unsupported record_format version {version!r}, "
                f"expected {RECORD_FORMAT_VERSION} (v{RECORD_FORMAT_V1} also reads)"
            )
        return cls(
            scenario_id=data["scenario_id"],
            seed=data["seed"],
            attempts=[RecordedAttempt.from_dict(a) for a in data["attempts"]],
            format_version=int(version),
        )

    @classmethod
    def from_json(cls, raw: str) -> "RunRecord":
        return cls.from_dict(json.loads(raw))

    @classmethod
    def read(cls, path: str | Path) -> "RunRecord":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


@dataclass
class Recorder:
    """Accumulates RecordedAttempt entries as a run progresses. Call
    `record_attempt()` after every validate() call, success or failure —
    never only on the final pass."""

    scenario_id: str
    seed: int
    _attempts: list[RecordedAttempt] = field(default_factory=list, repr=False)

    def record_attempt(
        self,
        candidate: CandidateUtterance,
        result: ValidationResult,
        *,
        retry_index: int,
        observed: dict[str, Any] | None = None,
    ) -> None:
        self._attempts.append(
            RecordedAttempt(
                candidate=to_dict(candidate),
                verdict=result.verdict.value,
                reason=result.reason,
                retry_index=retry_index,
                observed=dict(observed) if observed is not None else None,
            )
        )

    def record_outcome(
        self,
        *,
        failure_reason: str | None,
        ended_by: str,
    ) -> None:
        """Stamp the terminal run outcome onto every attempt (replay asserts
        the LAST attempt's outcome matches the replayed run's outcome)."""
        for attempt in self._attempts:
            attempt.outcome_failure = failure_reason
            attempt.outcome_ended_by = ended_by

    def finalize(self) -> RunRecord:
        return RunRecord(scenario_id=self.scenario_id, seed=self.seed, attempts=list(self._attempts))


class RecordedSemanticVerifier:
    """A SemanticVerifierProtocol that replays recorded observed evidence
    instead of calling any verifier backend (rule-based OR LLM).

    This is what makes replay zero-AI: the validator's semantic step reads
    the recorded ObservedAct rather than re-classifying the utterance. Any
    divergence between the recorded evidence and a fresh classification is
    impossible by construction (no fresh classification happens); the
    verdict-level assert in ReplayLanguageBackend remains the loud
    divergence gate.
    """

    def __init__(self, record: RunRecord) -> None:
        self._observed = [a.observed for a in record.attempts]
        self._cursor = 0

    def classify(self, utterance: str, contract) -> Any:  # noqa: ANN001,ANN201
        from . import ObservedAct

        if self._cursor >= len(self._observed):
            raise RuntimeError("replay exhausted: no more recorded semantic evidence")
        raw = self._observed[self._cursor]
        self._cursor += 1
        if raw is None:
            # Recorded before the semantic step was evidence-capturing (v1)
            # or the attempt never reached it: fall back to a neutral
            # low-confidence observation (forces UNKNOWN, never a false PASS).
            return ObservedAct(act="", target=None, confidence=0.0, all_acts=[])
        return ObservedAct(
            act=str(raw.get("act") or ""),
            target=raw.get("target"),
            slots=dict(raw.get("slots") or {}),
            confidence=float(raw.get("confidence", 0.0)),
            all_acts=list(raw.get("all_acts") or []),
        )


class ReplayLanguageBackend(LanguageBackendProtocol):
    """A LanguageBackendProtocol implementation that replays recorded
    candidates in order instead of calling any real AI backend — used so
    CI replay never touches the network.

    After each replayed candidate is validated by the caller, the caller
    MUST call `assert_verdict(actual_verdict)` so a divergence from the
    recorded verdict raises ReplayMismatchError immediately rather than
    silently producing a different result. When the record carries v2
    outcome stamps, the caller SHOULD also call `assert_outcome()` at run
    end so a divergent terminal state fails loudly too.
    """

    def __init__(self, record: RunRecord) -> None:
        self._attempts = iter(record.attempts)
        self._record = record
        self._last_attempt: RecordedAttempt | None = None

    def generate(self, context: dict[str, Any]) -> dict[str, Any]:
        try:
            self._last_attempt = next(self._attempts)
        except StopIteration as exc:
            # Run 063: the replayed LIVE run legitimately needs MORE
            # generations than the record holds (the agent answered
            # differently, so the driver keeps generating past the
            # recorded attempt count). The old code raised a bare
            # RuntimeError, which the adapter wrapped as
            # LANGUAGE_GENERATION_ERROR — a misleading failure that hides
            # the real story (agent divergence, not transport). Raise the
            # terminal wrapper directly so the driver maps it to a
            # REPLAY_DIVERGENCE-shaped LANGUAGE_GENERATION_ERROR with the
            # counts attached, instead of a bare "replay exhausted".
            from .language_adapter import LanguageGenerationError

            raise LanguageGenerationError(
                f"replay divergence: live run needed more generations than "
                f"the record holds (record has "
                f"{len(self._record.attempts)} attempts; the agent's live "
                f"answers diverged from the recorded run, so the driver "
                f"kept generating)"
            ) from exc
        return dict(self._last_attempt.candidate)

    def assert_verdict(self, actual_verdict: Verdict) -> None:
        if self._last_attempt is None:
            raise RuntimeError("assert_verdict() called before any generate() call")
        if actual_verdict.value != self._last_attempt.verdict:
            raise ReplayMismatchError(
                f"replay verdict mismatch: recorded {self._last_attempt.verdict!r}, "
                f"replayed run produced {actual_verdict.value!r}"
            )

    def assert_outcome(
        self, *, failure_reason: str | None, ended_by: str
    ) -> None:
        """Fail loudly when the replayed run's terminal outcome differs
        from the recording (v1 records without outcome stamps skip)."""
        attempts = self._record.attempts
        if not attempts:
            return
        recorded = attempts[-1]
        if recorded.outcome_failure is None and recorded.outcome_ended_by is None:
            return  # v1 record: no outcome evidence to assert
        if (
            recorded.outcome_failure != failure_reason
            or recorded.outcome_ended_by != ended_by
        ):
            raise ReplayMismatchError(
                f"replay outcome mismatch: recorded "
                f"({recorded.outcome_failure!r}, {recorded.outcome_ended_by!r}), "
                f"replayed run produced ({failure_reason!r}, {ended_by!r})"
            )


def rebuild_identity_from_candidate(candidate: dict[str, Any]) -> GenerationIdentity:
    """Convenience helper: reconstruct a GenerationIdentity from a recorded
    candidate dict (as produced by to_dict(CandidateUtterance))."""
    ident = candidate["identity"]
    return GenerationIdentity(
        behavior_id=ident["behavior_id"],
        turn_id=ident["turn_id"],
        generation_id=ident["generation_id"],
        context_version=ident.get("context_version", 0),
    )


__all__ = [
    "RECORD_FORMAT_VERSION",
    "RecordedAttempt",
    "RecordedSemanticVerifier",
    "Recorder",
    "ReplayLanguageBackend",
    "ReplayMismatchError",
    "RunRecord",
    "rebuild_identity_from_candidate",
]
