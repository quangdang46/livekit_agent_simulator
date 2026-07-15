"""OpenAI-compatible chat completions judge backend (any OpenAI-wire HTTP gateway)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class HttpOpenAIBackend:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.0,
        timeout_s: float = 90.0,
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

    async def complete_json(self, *, system: str, user: str) -> str:
        import asyncio

        return await asyncio.to_thread(self._post, system, user)

    def _post(self, system: str, user: str) -> str:
        body: dict[str, Any] = {
            "model": self._model,
            "temperature": self._temperature,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
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
            raise RuntimeError(f"HTTP judge {e.code}: {err_body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"HTTP judge unreachable: {e}") from e

        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError(f"HTTP judge empty choices: {str(payload)[:300]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            # Some gateways return content parts
            texts = [
                str(p.get("text") or "")
                for p in content
                if isinstance(p, dict) and p.get("type") in (None, "text")
            ]
            content = "".join(texts)
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("HTTP judge returned empty content")
        return content
