"""Gemini adapter for the decision-router port.

Raw ``urllib``, stdlib only, no ``google-genai`` — the sibling
``GeminiTextBackend`` uses the same mechanism, and this repo already depends
on that SDK only for ``evals/backends/gemini.py``.

Uses the WRAPPED ``application/json`` + ``responseSchema`` form rather than
``text/x.enum``. The bare enum form is real and marginally cheaper, but it
returns a raw unquoted string that must be hand-parsed, has no room for a
second field and no ``required`` semantics, and would give the two adapters
different response shapes for the same job. One shape, one parse path.

Two Gemini-specific rules from the vendor docs are load-bearing here:

  * ``required`` must be pinned. Every property is optional by default, and
    that is exactly what makes an empty ``{}`` reachable — ``{}`` is the
    degenerate body this adapter must reject rather than parse.
  * Do NOT restate the schema in the prompt. "Only specify the schema in the
    schema object. Don't also specify the schema in the prompt. Doing both
    can reduce performance."

@see https://ai.google.dev/gemini-api/docs/structured-output
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from .router import (
    RESPONSE_KEY,
    ResponseRouter,
    RouteDecision,
    RouterError,
    RouterFault,
    parse_route_body,
    route_schema_for,
    should_retry,
)

_SYSTEM = (
    "You route a simulated caller's next reply. The caller speaks for the "
    "company receiving the call; the agent is on the line. Read what the agent "
    "just said and choose the ONE response that answers it. Return only the "
    "responseId. Choose the system entry ONLY when nothing else fits."
)


def _user_prompt(agent_transcript: str, catalog: Any) -> str:
    """Context only — never the authored answer text, and never the schema.

    Gemini specifically penalises a schema restated in the prompt, and leaking
    the answer would turn the router into a guesser.
    """
    options = [
        {"responseId": spec.id, "intent": spec.intent, "instruction": spec.instruction}
        for spec in (catalog.responses[r] for r in catalog.offerable_ids())
    ]
    return json.dumps(
        {"agent_said": agent_transcript, "responses": options}, ensure_ascii=False
    )


class GeminiResponseRouter:
    """``ResponseRouter`` over the Generative Language REST API.

    Model default is the ALIAS: ``GeminiTextBackend`` records
    (text_backends.py:155-157) that the pinned ``gemini-2.0-flash`` returns
    HTTP 404 on this key, so the alias is the working form.
    """

    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-flash-latest",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout_s: float = 20.0,
        temperature: float = 0.0,
        # Set explicitly so thinking tokens cannot consume the whole budget and
        # return a candidate with zero text parts. A short classification does
        # not need a large output allowance, and a budget eaten by thinking is
        # indistinguishable from a routing failure.
        max_output_tokens: int = 256,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens

    def _endpoint(self) -> str:
        return f"{self._base_url}/models/{self._model}:generateContent?key={self._api_key}"

    async def route(self, *, agent_transcript: str, catalog: Any) -> RouteDecision:
        options = list(catalog.offerable_ids())
        body: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": _SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": _user_prompt(agent_transcript, catalog)}]}],
            "generationConfig": {
                "temperature": self._temperature,
                "responseMimeType": "application/json",
                "responseSchema": route_schema_for(catalog),
                "maxOutputTokens": self._max_output_tokens,
            },
        }
        started = time.monotonic()
        payload = self._post(body)

        # A blocked prompt is its own class: promptFeedback.blockReason, not an
        # empty candidate. Reporting it as a generic backend error would send
        # whoever reads the report looking in the wrong place.
        blocked = (payload.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            raise RouterFault(f"gemini: prompt blocked ({blocked})")

        candidates = payload.get("candidates") or []
        if not candidates:
            raise RouterFault(f"gemini: empty candidates: {str(payload)[:300]}")

        first = candidates[0]
        finish = first.get("finishReason")
        parts = ((first.get("content") or {}).get("parts")) or []
        text = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))
        if not text.strip():
            # Three degenerate shapes land here and they are NOT the same
            # failure: {} (required omitted upstream), a safety block, and a
            # budget consumed by thinking. Name the reason rather than guessing.
            reason = f" (finishReason={finish})" if finish else ""
            raise RouterFault(f"gemini: no text content{reason}")

        try:
            raw = json.loads(text)
        except json.JSONDecodeError as e:
            raise RouterFault(f"gemini: text was not JSON: {e}") from e

        decision = parse_route_body(raw, options=options, backend=self.name)
        return RouteDecision(
            response_id=decision.response_id,
            backend=self.name,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST with the port's shared retry policy: one retry on transport and
        429/5xx, never on 4xx. A 400 here is a schema-composition bug, not a
        flake, and retrying only delays the report of it."""
        data = json.dumps(body).encode("utf-8")
        last: RouterError | None = None
        for _ in range(2):  # one try, one retry
            req = urllib.request.Request(
                self._endpoint(),
                data=data,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")[:500]
                err: RouterError = RouterError(f"gemini router HTTP {e.code}: {detail}")
                if not should_retry(e.code):
                    raise err from e
                last = err
            except urllib.error.URLError as e:
                last = RouterError(f"gemini router unreachable: {e}")
        raise last or RouterError("gemini router unreachable")


__all__ = ["GeminiResponseRouter"]
