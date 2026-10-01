"""The vendor-neutral decision-router port.

The router answers one question — given what the agent just said, which
authored response should the caller give next — and returns exactly one
``response_id`` from the catalog. It never writes the caller's words (that
is the text backend's job), never touches the room, and never invents a
response.

The whole abstraction is one method, so swapping GPT-4.1 Nano for Gemini or
a future System One model is an adapter change, not a format change.

Uses ``urllib`` (stdlib only, matching ``caller_contract/text_backends.py``)
so no extra HTTP client dependency is required, and no provider SDK is
imported here — the adapters own their vendor's wire format.

@see docs/plans/response-router.md  (contract §1, decision D5)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

# The enum must be WRAPPED. A bare top-level {"type":"string","enum":[...]} is
# invalid: the OpenAI docs pin the root of a Structured Outputs schema to an
# object, and forbid anyOf at the root. One shape therefore serves both
# providers, and the named key makes the reply self-describing — the adapter
# detects a refusal or a malformed body by the ABSENCE of the key rather than
# by inspecting an untyped scalar.
# https://developers.openai.com/api/docs/guides/structured-outputs
SCHEMA_NAME = "response_route"
RESPONSE_KEY = "responseId"
#: The model's own certainty, and REQUIRED in the schema. See
#: `build_route_schema` — under strict structured outputs a property the
#: schema omits can never be returned, which is why an earlier, schema-free
#: `confidence` was permanently None.
CONFIDENCE_KEY = "confidence"

#: Below this the pick is discarded and the system entry is used instead.
#:
#: MEASURED, and the measurement says this cannot be a safety net — on this
#: model, confidence carries NO information about correctness. Measured on the
#: target repo, runs 137-138, feat-03-barge-in (2026-09-30):
#:
#:     correct  (company_name for a company-name question)   0.900
#:     wrong    (nothing_else for an announcement)           0.950
#:     wrong    (acknowledgement for a question)           0.950
#:
#: The confidently-wrong answers scored HIGHER than the correct ones. A floor
#: can only reject what the model admits to being unsure about, and this model
#: is not unsure when it is wrong. Any threshold between 0.90 and 0.95 rejects
#: everything or nothing.
#:
#: So this is NOT a tunable value. See bead livekit-agent-simulator-0k7, closed
#: with that answer.
#:
#: MEASURED INERT on the model in use. A further run (feat-06-confidence-probe)
#: asked for the company name with `company_name` deliberately ABSENT from the
#: catalog — no correct answer existed — and confidence was 0.950, the same
#: value as the confident-and-wrong picks and HIGHER than the
#: confident-and-correct one. The signal does not move when the catalog cannot
#: answer, which is the one moment a floor could act on. The distribution is
#: effectively a point mass at ~0.95.
#:
#: CONSEQUENCE, stated rather than smoothed over: on this model this floor
#: cannot fire, so the abstention path beneath it never executes. That is
#: flagged to the owner as a candidate for REMOVAL rather than tuning — a knob
#: proven inert is the same failure as the dead `confidence` field this file
#: previously carried, and AGENTS.md's "delete half-features in the same
#: change" applies to it. It is kept here only until that decision lands, not
#: because it is expected to work.
CONFIDENCE_FLOOR = 0.4

# A degenerate router returns the same id three decisions running. That is a
# HARNESS defect, not a conversation: a green suite built on it would be a lie,
# so it raises and the driver turns the run red.
DEGENERATE_RUN_LENGTH = 3

# Retry policy, shared by every adapter. Exactly ONE retry on transport errors
# and 429/5xx; NEVER on 400 — a 400 against a runtime-generated schema is a
# builder bug, and retrying it only turns a loud failure into a slow one.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class RouterError(RuntimeError):
    """Transport or parse failure. Adapters raise; the driver fails the run."""


class RouterFault(RouterError):
    """A harness-side fault — the model declined, or the body did not satisfy
    the schema.

    Distinct from a plain transport error because it must NEVER be turned into
    a plausible-looking responseId. The router's contract is "always a valid
    catalog id", and a refusal is precisely the case where inventing one would
    be a lie the caller then speaks out loud.
    """


class RouterTerminal(RouterError):
    """The router returned the same response three decisions running.

    Raised from the routing loop; the driver catches it and fails the run with
    LANGUAGE_GENERATION_ERROR / EndedBy.ERROR. A degenerate router must never
    be a green suite.
    """


@dataclass(frozen=True)
class RouteDecision:
    """One routing outcome.

    **There is deliberately no ``confidence`` field.** An earlier draft
    carried one, documented as "telemetry only", parsed out of the provider
    body and recorded per-decision in the run summary. It could never be
    populated: the request schema is
    ``{"responseId": {...}}`` with ``additionalProperties: False`` under
    ``"strict": True``, so the provider is structurally forbidden from
    returning anything else. Every real run recorded ``confidence: null``.

    It was the most dangerous shape a dead knob can take — a docstring
    promising telemetry, a real consumer writing it into ``decisions[]``, and
    a test feeding ``parse_route_body`` a hand-made body the provider cannot
    send — so it read as alive while always being dead.

    Removing it also removes the temptation to threshold an unmeasurable
    number: a ``confidence < 0.4 -> no-match`` rule over an always-``None``
    field is a silent no-op, which looks like a working safety net.

    If calibrated routing confidence is ever wanted, the honest shape is
    ``probabilities`` over the enum, not a self-reported scalar — and that is
    a separate design decision, not a revival of this field.
    """

    response_id: str
    #: The model's own certainty. REQUIRED in the wire schema and CONSUMED by
    #: the driver, which discards a pick below ``CONFIDENCE_FLOOR``. This field
    #: existed once as documentation with no consumer and was permanently None
    #: on every run; it was removed rather than left as a lie. It is back only
    #: because something reads it now.
    confidence: float | None = None
    backend: str = ""
    latency_ms: int | None = None


class ResponseRouter(Protocol):
    """The port. One method — everything else is an implementation detail."""

    async def route(
        self, *, agent_transcript: str, catalog: Any
    ) -> RouteDecision:
        """Pick the response that answers ``agent_transcript``.

        ``catalog`` is a ``ResponseCatalog`` (duck-typed here so this module
        does not import responses.py, keeping the port independent of the
        catalog's representation).

        Raises ``RouterTerminal`` on a degenerate repeat, ``RouterFault`` on a
        refusal or a schema violation, ``RouterError`` on transport failure.
        """
        ...


# ------------------------------------------------------------------- schema

_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


def build_route_schema(response_ids: list[str]) -> dict[str, Any]:
    """The runtime schema, wrapped and strict.

    ``required`` is not decoration. OpenAI strict mode refuses to run without
    it, and on Gemini its absence is exactly what makes an empty ``{}``
    reachable, because every property is optional by default there.
    """
    if not response_ids:
        raise ValueError("build_route_schema called with no options")
    if len(set(response_ids)) != len(response_ids):
        raise ValueError("build_route_schema received duplicate option ids")
    return {
        "type": "object",
        "properties": {
            RESPONSE_KEY: {"type": "string", "enum": list(response_ids)},
            CONFIDENCE_KEY: {"type": "number"},
        },
        # BOTH required. That is the whole point of putting confidence in the
        # schema: strict structured outputs forbid any property outside it, so
        # a field the schema does not declare can never be returned. An earlier
        # draft of this file carried `confidence` as documentation only, which
        # is why it was permanently None on every run (measured 2026-09-30) —
        # the router had no way to express certainty and `off_script` could
        # only ever be a label on whatever it picked.
        #
        # With both required, `CONFIDENCE_FLOOR` in the driver turns "the model
        # is not sure" into a real decision: below the floor the pick is
        # discarded and the system entry is used instead, so an unmodelled
        # agent question is RECORDED rather than silently answered with the
        # nearest plausible entry.
        "required": [RESPONSE_KEY, CONFIDENCE_KEY],
        "additionalProperties": False,
    }


def route_schema_for(catalog: Any) -> dict[str, Any]:
    """Cached schema for the catalog's CURRENT option set.

    Cached on sha1 of the sorted option ids because OpenAI charges a one-time
    compile per schema, and the option set changes as non-reusable responses
    are spent. Recomputing per turn would pay that compile on every turn;
    cached, the worst case is one compile per SPEND rather than per turn, and a
    stable option set compiles once for the whole run.
    """
    ids = sorted(catalog.offerable_ids())
    sig = hashlib.sha1(",".join(ids).encode("utf-8")).hexdigest()
    cached = _SCHEMA_CACHE.get(sig)
    if cached is None:
        cached = build_route_schema(ids)
        _SCHEMA_CACHE[sig] = cached
    return cached


def clear_schema_cache() -> None:
    """Test seam — the cache is process-global by design."""
    _SCHEMA_CACHE.clear()


# --------------------------------------------------------------------- parse


@dataclass
class DegeneracyGuard:
    """Detects a router that keeps returning the same response.

    Deliberately stateful and deliberately small: the point is to fail the run,
    not to smooth anything over.

    ── DO NOT exempt abstentions. Measured 2026-09-30, and the exemption
    looks obviously right until you read the transcripts. ──

    A proposal was to skip this counter when the chosen entry is the
    `system: true` one — reasoning that "the model admitting uncertainty three
    times is the contract working, not degeneracy, and this guard kills 5 of
    25 runs". The frequency was right and the conclusion was wrong.

    Those five runs were not a healthy call that the guard interrupted. The
    agent was mid-utterance and never finished: the transcript accumulates
    fragments turn over turn (`. Next, could` -> `. Next, could you` ->
    `. Next, could you please`), no turn ever closes, and once the caller
    answers "sorry, could you repeat that" the agent restarts a fragment it
    has already begun. Exempting abstentions there converts a fast failure
    into an unbounded loop on a duplex call that bills by the minute.

    So the failure rate is a *symptom* of a conversation already dead, not
    damage done by the guard. The guard is the only thing that stops it.

    A non-consecutive cycle (A-B-A-B) is genuinely not caught here — only
    consecutive repeats count. That is a real limit. It was left alone
    because the one observed cycle was authored into existence by marking
    every entry `reusable: true`, i.e. a catalog fault rather than evidence
    that the guard is too weak. Loosening it to "3 of the last 5" would make
    the guard kill MORE runs, which is the opposite of what is wanted.
    """

    last: str | None = None
    run_length: int = 0

    def observe(self, response_id: str) -> None:
        if response_id == self.last:
            self.run_length += 1
        else:
            self.last = response_id
            self.run_length = 1
        if self.run_length >= DEGENERATE_RUN_LENGTH:
            raise RouterTerminal(
                f"router degenerate: {response_id!r} returned "
                f"{self.run_length} decisions running"
            )

    def reset(self) -> None:
        self.last = None
        self.run_length = 0


def parse_route_body(raw: Any, *, options: list[str], backend: str) -> RouteDecision:
    """Turn a provider body into a RouteDecision, or raise.

    An id outside ``options`` is a CONTRACT VIOLATION, never something to
    coerce or argmax over a loose parse — that would reintroduce exactly the
    out-of-set label the enum exists to prevent.
    """
    if not isinstance(raw, dict):
        raise RouterFault(f"{backend}: expected a JSON object, got {type(raw).__name__}")
    if RESPONSE_KEY not in raw:
        # The absent key is how a refusal or a truncated body shows up.
        raise RouterFault(f"{backend}: response has no {RESPONSE_KEY!r} (refusal or malformed body)")
    response_id = raw[RESPONSE_KEY]
    if not isinstance(response_id, str):
        raise RouterFault(f"{backend}: {RESPONSE_KEY!r} was {type(response_id).__name__}, not a string")
    if response_id not in options:
        raise RouterFault(
            f"{backend}: returned {response_id!r}, which is not one of the offered options"
        )
    confidence = raw.get(CONFIDENCE_KEY)
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise RouterFault(
            f"{backend}: {CONFIDENCE_KEY!r} was {type(confidence).__name__}, not a "
            "number. The schema marks it required, so its absence means a "
            "provider that is not honouring strict structured outputs."
        )
    return RouteDecision(
        response_id=response_id,
        confidence=float(confidence),
        backend=backend,
    )


def should_retry(status: int | None, exc: BaseException | None = None) -> bool:
    """One retry on transport errors and 429/5xx. Never on any 4xx.

    A 400 against a runtime-generated schema is a builder bug — retrying turns
    a loud, correct failure into a slow, misleading one.
    """
    if exc is not None and isinstance(exc, urllib_transport_errors()):
        return True
    if status is None:
        return True  # transport gave no status at all
    return status in RETRYABLE_STATUS


def urllib_transport_errors() -> tuple[type[BaseException], ...]:
    import urllib.error

    return (urllib.error.URLError, TimeoutError, ConnectionError, OSError)


def dump_schema(schema: dict[str, Any]) -> str:
    """Stable rendering, for logs and for a test that asserts the shape."""
    return json.dumps(schema, sort_keys=True, separators=(",", ":"))


__all__ = [
    "DEGENERATE_RUN_LENGTH",
    "DegeneracyGuard",
    "RESPONSE_KEY",
    "RETRYABLE_STATUS",
    "ResponseRouter",
    "RouteDecision",
    "RouterError",
    "RouterFault",
    "RouterTerminal",
    "SCHEMA_NAME",
    "build_route_schema",
    "clear_schema_cache",
    "dump_schema",
    "parse_route_body",
    "route_schema_for",
    "should_retry",
]
