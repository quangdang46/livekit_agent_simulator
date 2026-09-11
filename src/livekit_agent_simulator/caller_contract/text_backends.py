"""P0-5 concrete backends: stateless TEXT-only LLM calls for `do:` generation.

Hard boundary (design lock, see docs/contract-caller-wiring.md): these
backends implement ONLY ``LanguageBackendProtocol.generate(context) -> dict``.
They never touch a Realtime/audio session, never publish, never pick the next
behavior, never decide to hang up. Fusing generation with audio-session
publish is exactly the root cause of the role-flip/recap bug class this
redesign eliminates (bead livekit-agent-simulator-rma) — a text backend
structurally cannot reproduce that bug because it has no publish path at all.

Each backend is a single stateless HTTP request/response (matches the
"Approach B" in language_adapter.py's module docstring): no conversation
memory here, all memory lives in the ConversationContext passed in by
build_context() / the Orchestrator.

Uses ``urllib`` (stdlib only, same as ``evals/backends/http_openai.py`` and
``http_anthropic.py``) so no extra HTTP client dependency is required.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class LanguageBackendError(RuntimeError):
    """Transport/parse failure — AILanguageAdapter wraps this into bounded
    retries, then LanguageGenerationError. Never swallowed silently."""


_SYSTEM_PROMPT = (
    "You are generating ONE line of dialogue for a simulated phone caller. "
    "You do not decide what to do next, when to speak, or when to end the "
    "call — you only phrase the CURRENT behavior naturally in first person. "
    "Respond with a single JSON object, no prose, no markdown fences: "
    '{"act": "<same as current_behavior.act>", "target": <same target or null>, '
    '"slots": {}, "utterance": "<one natural sentence>"}. '
    "The utterance must stay strictly on-topic for current_behavior and must "
    "never mention anything in forbidden context, never ask about unrelated "
    "topics, and never say goodbye/end the call unless current_behavior.act "
    "itself is an end/hangup behavior. "
    # Run 025 (Phase D): the model paraphrased ask/price into "I'm asking
    # about the 2022 Honda CR-V." — no price word at all — and the validator
    # correctly failed it closed as TARGET_UNVERIFIED (the utterance is about
    # the car, not the price). The context already carries
    # current_behavior.target, but nothing told the model the target must be
    # LEXICALLY present. So: when current_behavior.target is set, the
    # utterance MUST contain a word for that topic (e.g. for target price:
    # "price", "cost", or "$"). A paraphrase that drops the topic word is not
    # a valid phrasing of the behavior, however natural it sounds.
    "When current_behavior.target is set, the utterance MUST name that topic "
    "in words — do not paraphrase the topic away."
)


def _build_user_prompt(context: dict[str, Any]) -> str:
    return json.dumps(context, ensure_ascii=False)


def _parse_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    # Strip a stray ```json ... ``` fence some models add despite instructions.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LanguageBackendError(f"backend did not return valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LanguageBackendError(f"backend JSON must be an object, got {type(parsed).__name__}")
    return parsed


class OpenAITextBackend:
    """LanguageBackendProtocol via OpenAI-compatible chat completions.

    Text-only (`gpt-4o-mini` / `gpt-4.1-mini` class models) — NOT the
    Realtime API. No audio session; ``generate()`` is a single stateless
    HTTP round trip returning the raw candidate dict.
    """

    def __init__(
        self,
        *,
        base_url: str = "https://api.openai.com/v1",
        api_key: str,
        model: str = "gpt-4o-mini",
        temperature: float = 0.4,
        timeout_s: float = 20.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        self._timeout_s = timeout_s

    def _endpoint(self) -> str:
        base = self._base_url
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def generate(self, context: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "temperature": self._temperature,
            "stream": False,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(context)},
            ],
            "response_format": {"type": "json_object"},
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint(),
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            raise LanguageBackendError(f"OpenAI text backend HTTP {e.code}: {err_body}") from e
        except urllib.error.URLError as e:
            raise LanguageBackendError(f"OpenAI text backend unreachable: {e}") from e

        choices = payload.get("choices") or []
        if not choices:
            raise LanguageBackendError(f"OpenAI text backend empty choices: {str(payload)[:300]}")
        content = (choices[0].get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise LanguageBackendError("OpenAI text backend returned empty content")
        return _parse_json_object(content)


class GeminiTextBackend:
    """LanguageBackendProtocol via Google Generative Language REST API.

    Text-only ``generateContent`` — NOT the Live/Realtime audio API. Single
    stateless HTTP round trip; no session, no publish.
    """

    # NOTE 2026-09-11 (run 011 production evidence): gemini-2.0-flash returns
    # HTTP 404 on this key ("no longer available"); gemini-flash-latest is the
    # working alias (same fix as semantic_llm.py LLMSemanticVerifier default).
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-flash-latest",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        temperature: float = 0.4,
        timeout_s: float = 20.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._temperature = temperature
        self._timeout_s = timeout_s

    def _endpoint(self) -> str:
        return f"{self._base_url}/models/{self._model}:generateContent?key={self._api_key}"

    def generate(self, context: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": _build_user_prompt(context)}]}],
            "generationConfig": {
                "temperature": self._temperature,
                "responseMimeType": "application/json",
            },
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint(),
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            raise LanguageBackendError(f"Gemini text backend HTTP {e.code}: {err_body}") from e
        except urllib.error.URLError as e:
            raise LanguageBackendError(f"Gemini text backend unreachable: {e}") from e

        candidates = payload.get("candidates") or []
        if not candidates:
            raise LanguageBackendError(f"Gemini text backend empty candidates: {str(payload)[:300]}")
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))
        if not text.strip():
            raise LanguageBackendError("Gemini text backend returned empty content")
        return _parse_json_object(text)


__all__ = ["GeminiTextBackend", "LanguageBackendError", "OpenAITextBackend"]
