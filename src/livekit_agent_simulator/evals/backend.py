"""Judge backend Protocol + factory."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..config import JudgeConfig
from .resolve import ResolvedJudge, resolve_judge


@runtime_checkable
class JudgeBackend(Protocol):
    async def complete_json(self, *, system: str, user: str) -> str:
        """Return raw assistant text expected to be JSON."""
        ...


def backend_for(resolved: ResolvedJudge) -> JudgeBackend:
    if resolved.mode == "http":
        assert resolved.base_url and resolved.api_key
        if resolved.endpoint_type == "anthropic":
            from .backends.http_anthropic import HttpAnthropicBackend

            return HttpAnthropicBackend(
                base_url=resolved.base_url,
                api_key=resolved.api_key,
                model=resolved.model,
                temperature=resolved.temperature,
            )
        from .backends.http_openai import HttpOpenAIBackend

        return HttpOpenAIBackend(
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            model=resolved.model,
            temperature=resolved.temperature,
        )
    from .backends.gemini import GeminiBackend

    assert resolved.google_api_key
    return GeminiBackend(
        api_key=resolved.google_api_key,
        model=resolved.model,
        temperature=resolved.temperature,
    )


def backend_from_config(
    judge_cfg: JudgeConfig,
    google_api_key: str,
) -> JudgeBackend | None:
    resolved = resolve_judge(judge_cfg, google_api_key=google_api_key)
    if not resolved.ready:
        return None
    return backend_for(resolved)
