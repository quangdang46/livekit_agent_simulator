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

RECORD_FORMAT_VERSION = 1


class ReplayMismatchError(RuntimeError):
    """Raised when a replayed candidate's validator verdict does not match
    the verdict recorded during the original run — this must fail loudly,
    never silently diverge (report §28.5(22))."""


@dataclass
class RecordedAttempt:
    """One generate+validate attempt for a single turn, success or not."""

    candidate: dict[str, Any]
    verdict: str
    reason: str | None
    retry_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate,
            "verdict": self.verdict,
            "reason": self.reason,
            "retry_index": self.retry_index,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecordedAttempt":
        return cls(
            candidate=data["candidate"],
            verdict=data["verdict"],
            reason=data.get("reason"),
            retry_index=data["retry_index"],
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
        if data.get("format_version") != RECORD_FORMAT_VERSION:
            raise ValueError(
                f"unsupported record_format version {data.get('format_version')!r}, "
                f"expected {RECORD_FORMAT_VERSION}"
            )
        return cls(
            scenario_id=data["scenario_id"],
            seed=data["seed"],
            attempts=[RecordedAttempt.from_dict(a) for a in data["attempts"]],
            format_version=data["format_version"],
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
    ) -> None:
        self._attempts.append(
            RecordedAttempt(
                candidate=to_dict(candidate),
                verdict=result.verdict.value,
                reason=result.reason,
                retry_index=retry_index,
            )
        )

    def finalize(self) -> RunRecord:
        return RunRecord(scenario_id=self.scenario_id, seed=self.seed, attempts=list(self._attempts))


class ReplayLanguageBackend(LanguageBackendProtocol):
    """A LanguageBackendProtocol implementation that replays recorded
    candidates in order instead of calling any real AI backend — used so
    CI replay never touches the network.

    After each replayed candidate is validated by the caller, the caller
    MUST call `assert_verdict(actual_verdict)` so a divergence from the
    recorded verdict raises ReplayMismatchError immediately rather than
    silently producing a different result.
    """

    def __init__(self, record: RunRecord) -> None:
        self._attempts = iter(record.attempts)
        self._last_attempt: RecordedAttempt | None = None

    def generate(self, context: dict[str, Any]) -> dict[str, Any]:
        try:
            self._last_attempt = next(self._attempts)
        except StopIteration as exc:
            raise RuntimeError("replay exhausted: no more recorded attempts") from exc
        return dict(self._last_attempt.candidate)

    def assert_verdict(self, actual_verdict: Verdict) -> None:
        if self._last_attempt is None:
            raise RuntimeError("assert_verdict() called before any generate() call")
        if actual_verdict.value != self._last_attempt.verdict:
            raise ReplayMismatchError(
                f"replay verdict mismatch: recorded {self._last_attempt.verdict!r}, "
                f"replayed run produced {actual_verdict.value!r}"
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
    "Recorder",
    "ReplayLanguageBackend",
    "ReplayMismatchError",
    "RunRecord",
    "rebuild_identity_from_candidate",
]
