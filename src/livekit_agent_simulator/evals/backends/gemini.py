"""Native Gemini JSON judge backend."""

from __future__ import annotations

from google import genai
from google.genai import types


class GeminiBackend:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        temperature: float = 0.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._temperature = temperature

    async def complete_json(self, *, system: str, user: str) -> str:
        client = genai.Client(api_key=self._api_key)
        response = await client.aio.models.generate_content(
            model=self._model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=self._temperature,
                response_mime_type="application/json",
            ),
        )
        text = (response.text or "").strip()
        if not text:
            raise RuntimeError("Gemini judge returned empty content")
        return text
