"""Authored caller responses — the catalog the response router selects from.

A response is a *response*, not a fact: it may inform, refuse, confirm,
re-ask, agree, or end the call. The router returns exactly one ``response_id``
from this catalog and never a value outside it, which is what makes
"always a valid responseId" a structural property rather than a hope.

The catalog is a transitional state, not a replacement: ``caller_steps`` stays
required and wins when both are present (see the migration plan's D13). This
module adds the vocabulary; deciding when it engages is the scenario schema's
and the driver's job.

@see docs/plans/response-router.md  (contract, §1; decisions D7, D8)
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Reserved id for the framework-owned system response. A leading underscore is
# outside the scenario-id character class (see the id regex in scenario.py), so
# it cannot collide with an authored id.
RESERVED_SYSTEM_ID = "__off_script__"

# Parse-time ceilings on catalog size, from the provider's OWN published
# structured-output limits (v2-1 research). They must be enforced at parse,
# with file:line, because a schema that breaches them is rejected mid-call
# with a 400 that would otherwise be attributed to the agent under test.
#
#   "A schema may have up to 1000 enum values across all enum properties."
#   "For a single enum property with string values, the total string length of
#    all enum values cannot exceed 15,000 characters when there are more than
#    250 enum values."
#
# The second rule usually binds FIRST, and only applies past 250 values — so a
# count-only check would wave a 400-id catalog straight through to a live 400.
# See https://developers.openai.com/api/docs/guides/structured-outputs
MAX_RESPONSES = 1000

# The joined-length rule is inert at or below this count (it is documented as
# applying to "more than 250" values).
ENUM_LENGTH_LIMIT_FROM = 250
MAX_ENUM_TOTAL_CHARS = 15_000


class ResponseCatalogError(ValueError):
    """A catalog that cannot satisfy the router contract.

    Carries the scenario file and line so the author sees WHERE, matching how
    every other parse failure in this package reports itself.
    """

    def __init__(self, msg: str, *, file: str | None = None, line: int | None = None) -> None:
        self.file = file
        self.line = line
        where = f"{file}:{line}" if file and line is not None else (file or "")
        super().__init__(f"{msg}{' at ' + where if where else ''}")


@dataclass(frozen=True)
class ResponseSpec:
    """One authored response.

    ``intent`` and ``instruction`` are what the ROUTER sees. ``text`` is
    ground truth the backend publishes. The router is never given ``text``:
    letting the model see the answer would let it pattern-match the answer
    instead of matching the question.
    """

    id: str
    intent: str
    instruction: str
    text: str
    system: bool = False
    reusable: bool = False

    def __post_init__(self) -> None:
        if not self.instruction.strip():
            raise ResponseCatalogError(
                f"response {self.id!r} has an empty `instruction:` — the router has nothing to match against"
            )
        if not self.text.strip():
            raise ResponseCatalogError(
                f"response {self.id!r} has an empty `text:` — there is nothing to publish"
            )


@dataclass
class ResponseCatalog:
    """The authored responses plus the bookkeeping the router needs.

    ``serve`` tracks what has been said. A non-reusable response is spent after
    its first use; a reusable one may answer the same question again (the agent
    repeating itself is a normal occurrence, not an error).

    When every non-system entry is spent the offerable option set collapses to
    the system response alone. That is a CATALOG AUTHORING fact — the scenario
    ran out of authored material — and not an agent deviation, so the router
    must not manufacture a false ``off_script`` for it. The collapse is handled
    in the arithmetic (``offered_ids``), not hidden.
    """

    responses: dict[str, ResponseSpec]
    file: str | None = None
    line: int | None = None
    _served: dict[str, int] = field(default_factory=dict)

    # ---------------------------------------------------------------- build

    @classmethod
    def from_dict(
        cls, raw: dict[str, object], *, file: str | None = None, line: int | None = None
    ) -> ResponseCatalog:
        if not raw:
            raise ResponseCatalogError("`responses:` is empty — author at least one response", file=file, line=line)
        if len(raw) > MAX_RESPONSES:
            raise ResponseCatalogError(
                f"`responses:` has {len(raw)} entries, over the {MAX_RESPONSES} enum ceiling "
                "(a provider rejects an enum this large mid-call, and that failure would be "
                "reported against the agent under test rather than against this scenario)",
                file=file,
                line=line,
            )
        # The joined-length rule binds before the count rule once a catalog gets
        # large, and it is INERT at or below ENUM_LENGTH_LIMIT_FROM — so it must
        # not be applied unconditionally.
        joined = sum(len(k) for k in raw)
        if len(raw) > ENUM_LENGTH_LIMIT_FROM and joined > MAX_ENUM_TOTAL_CHARS:
            raise ResponseCatalogError(
                f"`responses:` ids total {joined} characters across {len(raw)} entries, over the "
                f"{MAX_ENUM_TOTAL_CHARS}-character enum limit that applies above "
                f"{ENUM_LENGTH_LIMIT_FROM} values (a provider rejects this schema mid-call, and "
                "that failure would be reported against the agent under test). Shorten the ids.",
                file=file,
                line=line,
            )
        if RESERVED_SYSTEM_ID in raw:
            raise ResponseCatalogError(
                f"{RESERVED_SYSTEM_ID!r} is reserved for the framework-owned system response and "
                "cannot be authored",
                file=file,
                line=line,
            )

        specs: dict[str, ResponseSpec] = {}
        for key, value in raw.items():
            if not isinstance(value, dict):
                raise ResponseCatalogError(
                    f"response {key!r} must be a mapping with `intent`, `instruction` and `text`",
                    file=file,
                    line=line,
                )
            unknown = set(value) - {"intent", "instruction", "text", "system", "reusable"}
            if unknown:
                raise ResponseCatalogError(
                    f"response {key!r} has unknown key(s) {sorted(unknown)}; "
                    "allowed: intent, instruction, text, system, reusable",
                    file=file,
                    line=line,
                )
            for required in ("intent", "instruction", "text"):
                if required not in value:
                    raise ResponseCatalogError(
                        f"response {key!r} is missing `{required}:`", file=file, line=line
                    )
            specs[key] = ResponseSpec(
                id=key,
                intent=str(value["intent"]),
                instruction=str(value["instruction"]),
                text=str(value["text"]),
                system=bool(value.get("system", False)),
                reusable=bool(value.get("reusable", False)),
            )

        catalog = cls(responses=specs, file=file, line=line)
        catalog._validate_system_entry()
        catalog._validate_unique_intents()
        return catalog

    def _validate_system_entry(self) -> None:
        """The system entry is AUTHORED, never defaulted.

        A framework-supplied default would be a business string in ``src/``,
        which AGENTS.md forbids outright — a canned out-of-scope line baked
        into the package would be spoken by a simulated caller in a scenario
        that has nothing to do with it. Requiring it here is also what turns
        "always a valid responseId" into a PARSE-time guarantee the author can
        see in ``lks export`` (decision D7).
        """
        system = [r for r in self.responses.values() if r.system]
        if not system:
            raise ResponseCatalogError(
                "`responses:` must author a system response (`system: true`) — it is the entry the "
                "router returns when the agent asks something no authored response covers, and it is "
                "not defaulted here because src/ carries no business text",
                file=self.file,
                line=self.line,
            )
        if len(system) > 1:
            raise ResponseCatalogError(
                f"{len(system)} responses are marked `system: true`; exactly one is allowed",
                file=self.file,
                line=self.line,
            )

    def _validate_unique_intents(self) -> None:
        """Run AFTER the system entry is known, so a colliding intent is caught.

        Two ids sharing an intent would make the judge unable to attribute which
        response was served.
        """
        seen: dict[str, str] = {}
        for spec in self.responses.values():
            if spec.intent in seen:
                raise ResponseCatalogError(
                    f"responses {seen[spec.intent]!r} and {spec.id!r} share intent "
                    f"{spec.intent!r}; intents must be unique so a served response can be attributed",
                    file=self.file,
                    line=self.line,
                )
            seen[spec.intent] = spec.id

    # -------------------------------------------------------------- queries

    @property
    def system_id(self) -> str:
        return next(r.id for r in self.responses.values() if r.system)

    def get(self, response_id: str) -> ResponseSpec:
        try:
            return self.responses[response_id]
        except KeyError:
            raise ResponseCatalogError(
                f"response {response_id!r} is not in this catalog (have: {sorted(self.responses)})",
                file=self.file,
                line=self.line,
            ) from None

    def offerable_ids(self) -> list[str]:
        """Ids the router may still choose from, in authored order.

        The system response is always offerable, including when every other
        entry is spent. A schema built from this list is therefore never empty,
        which is what lets the router's contract be "always a valid id".
        """
        sys_id = self.system_id
        return [
            r.id
            for r in self.responses.values()
            if r.system or r.reusable or self._served.get(r.id, 0) == 0
        ]

    # ------------------------------------------------------------- serve

    def serve(self, response_id: str) -> None:
        """Record that ``response_id`` was used.

        Non-reusable responses are spent after one use. A spent response simply
        leaves ``offerable_ids``; it is not an error to have spent it, and the
        router never has to invent a fallback.
        """
        self.get(response_id)
        self._served[response_id] = self._served.get(response_id, 0) + 1

    def served_counts(self) -> dict[str, int]:
        return dict(self._served)

    def is_spent(self, response_id: str) -> bool:
        spec = self.get(response_id)
        return not spec.reusable and self._served.get(response_id, 0) > 0


__all__ = [
    "ENUM_LENGTH_LIMIT_FROM",
    "MAX_ENUM_TOTAL_CHARS",
    "MAX_RESPONSES",
    "RESERVED_SYSTEM_ID",
    "ResponseCatalog",
    "ResponseCatalogError",
    "ResponseSpec",
]
