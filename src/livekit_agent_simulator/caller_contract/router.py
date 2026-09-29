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

    ``confidence`` is TELEMETRY ONLY. It is carried so a run can compare
    backends and so a low-confidence case can be found when a prompt needs
    work. Nothing branches on it: the runtime reads ``response_id`` and
    nothing else.
    """

    response_id: str
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
        },
        "required": [RESPONSE_KEY],
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
    confidence = raw.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        confidence = None
    return RouteDecision(
        response_id=response_id,
        confidence=float(confidence) if confidence is not None else None,
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
