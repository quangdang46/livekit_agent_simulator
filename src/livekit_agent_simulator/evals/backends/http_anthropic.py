"""Anthropic Messages API judge backend (OpenAI-compatible gateway `/messages`)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class HttpAnthropicBackend:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        timeout_s: float = 90.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout_s = timeout_s

    def _endpoint(self) -> str:
        base = self._base_url
        if base.endswith("/messages"):
            return base
        return f"{base}/messages"

    async def complete_json(self, *, system: str, user: str) -> str:
        import asyncio

        return await asyncio.to_thread(self._post, system, user)

    def _post(self, system: str, user: str) -> str:
        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "stream": False,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint(),
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"HTTP anthropic judge {e.code}: {err_body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"HTTP anthropic judge unreachable: {e}") from e

        return _extract_anthropic_text(payload)


def _extract_anthropic_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            # Skip thinking blocks; take text
            if part.get("type") in (None, "text") and part.get("text"):
                texts.append(str(part["text"]))
        joined = "".join(texts).strip()
        if joined:
            return joined
    raise RuntimeError(f"Anthropic judge empty content: {str(payload)[:300]}")
