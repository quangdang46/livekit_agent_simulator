"""OpenAI adapter for the decision-router port.

Raw ``urllib``, stdlib only, no ``openai`` SDK — matching
``caller_contract/text_backends.py`` and its module docstring ("stdlib only,
so no extra HTTP client dependency"). The vendor wire format lives here and
nowhere else, so the port in ``router.py`` stays vendor-neutral.

The request carries a RUNTIME-GENERATED schema under the provider's strict
structured-output mode. That is what makes an out-of-set response id
structurally impossible rather than merely unlikely: without ``strict``, a
schema missing ``required`` / ``additionalProperties`` is not Structured
Outputs at all, and the model is free to emit any label.

@see https://developers.openai.com/api/docs/guides/structured-outputs
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from .http_json import build_request, post_json
from .router import (
    RESPONSE_KEY,
    SCHEMA_NAME,
    ResponseRouter,
    RouteDecision,
    RouterError,
    RouterFault,
    parse_route_body,
    route_schema_for,
    should_retry,
)

# The model is given the agent's line and the SEMANTICS of each option, never
# the authored answer text — otherwise it pattern-matches on the answer instead
# of the question. The system entry's instruction is phrased as a selection
# RULE, not an action, because a model handed N imperatives has a strong prior
# to return one of them.
_SYSTEM = (
    "You route a simulated caller's next reply. The caller speaks for the "
    "company receiving the call; the agent is on the line. Read what the agent "
    "just said and choose the ONE response that answers it. Return only the "
    "responseId. Choose the system entry ONLY when nothing else fits."
)

# json.dumps of the same context shape the text backend uses, so the two
# prompts read the same way.
def _user_prompt(agent_transcript: str, catalog: Any) -> str:
    options = [
        {"responseId": spec.id, "intent": spec.intent, "instruction": spec.instruction}
        for spec in (catalog.responses[r] for r in catalog.offerable_ids())
    ]
    return json.dumps(
        {"agent_said": agent_transcript, "responses": options}, ensure_ascii=False
    )


class OpenAIResponseRouter:
    """``ResponseRouter`` over the OpenAI Chat Completions API.

    Note the model default is the ALIAS, not a version pin — the sibling
    ``GeminiTextBackend`` records (text_backends.py:155-157) that a pinned
    Gemini model 404s on this key, and the same discipline applies here.
    """

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-4.1-nano",
        base_url: str = "https://api.openai.com/v1",
        timeout_s: float = 10.0,
        temperature: float = 0.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._timeout_s = timeout_s
        self._temperature = temperature

    def _endpoint(self) -> str:
        return (
            self._base_url
            if self._base_url.endswith("/chat/completions")
            else f"{self._base_url}/chat/completions"
        )

    async def route(self, *, agent_transcript: str, catalog: Any) -> RouteDecision:
        options = list(catalog.offerable_ids())
        body: dict[str, Any] = {
            "model": self._model,
            "temperature": self._temperature,
            "stream": False,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _user_prompt(agent_transcript, catalog)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": SCHEMA_NAME,
                    "strict": True,
                    "schema": route_schema_for(catalog),
                },
            },
        }
        started = time.monotonic()
        payload = self._post(body)
        message = (payload.get("choices") or [{}])[0].get("message") or {}

        # A refusal is NOT a distinct status — it is HTTP 200 whose message
        # carries `refusal` instead of `content`. It must become a fault, never
        # a fabricated id: the caller would speak the invention out loud.
        if message.get("refusal") is not None:
            raise RouterFault(
                f"openai: model declined ({str(message['refusal'])[:200]})"
            )
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RouterFault("openai: empty content (refusal or truncated body)")

        try:
            raw = json.loads(content)
        except json.JSONDecodeError as e:
            raise RouterFault(f"openai: content was not JSON: {e}") from e

        decision = parse_route_body(raw, options=options, backend=self.name)
        # `confidence` MUST be forwarded. Dropping it here is what defeated the
        # entire abstention mechanism: parse_route_body validated it and the
        # dataclass default of None then made driver.py's
        # `if decision.confidence is not None and (...)` short-circuit, so
        # CONFIDENCE_FLOOR was never evaluated against a real number on any
        # run. Measured on the target repo, run 136: eight of eight decisions
        # recorded `confidence: null`.
        return RouteDecision(
            response_id=decision.response_id,
            confidence=decision.confidence,
            backend=self.name,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST with the port's shared retry policy: exactly one retry on
        transport failures and 429/5xx, NEVER on a 4xx.

        A 400 against our own runtime-generated schema is a builder bug, and
        retrying it only turns a loud failure into a slow one.

        The retry decision is `router.should_retry`, the single authority for
        that rule; the transport itself is shared with the text backends via
        `http_json.post_json`, which is why the two families can no longer
        drift apart without a test noticing.
        """
        return post_json(
            request=build_request(
                url=self._endpoint(), body=body, api_key=self._api_key
            ),
            timeout_s=self._timeout_s,
            error_factory=lambda detail: RouterError(f"openai router {detail}"),
            retry_policy=should_retry,
            max_attempts=2,
        )


__all__ = ["OpenAIResponseRouter"]
