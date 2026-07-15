"""Resolve JudgeConfig fields: yaml literal → JUDGE_* → Gemini key."""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..config import DEFAULT_JUDGE_MODEL, JudgeConfig


@dataclass(frozen=True)
class ResolvedJudge:
    model: str
    temperature: float
    base_url: str | None
    api_key: str | None
    google_api_key: str | None
    mode: str  # http | gemini
    endpoint_type: str  # openai | anthropic (http wire only)
    ready: bool
    skip_reason: str = ""


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _resolve_endpoint_type(judge_cfg: JudgeConfig) -> str:
    raw = (
        (judge_cfg.endpoint_type or "").strip().lower()
        or _env("JUDGE_ENDPOINT_TYPE")
        or "openai"
    )
    if raw not in ("openai", "anthropic"):
        return "openai"
    return raw


def resolve_judge(
    judge_cfg: JudgeConfig | None,
    *,
    google_api_key: str | None = None,
) -> ResolvedJudge:
    if judge_cfg is None:
        return ResolvedJudge(
            model=DEFAULT_JUDGE_MODEL,
            temperature=0.0,
            base_url=None,
            api_key=None,
            google_api_key=None,
            mode="gemini",
            endpoint_type="openai",
            ready=False,
            skip_reason="No judge: block in config.",
        )

    model = (judge_cfg.model or "").strip() or _env("JUDGE_MODEL") or DEFAULT_JUDGE_MODEL
    temperature = float(judge_cfg.temperature)
    base_url = (judge_cfg.base_url or "").strip() or _env("JUDGE_BASE_URL")
    api_key = (judge_cfg.api_key or "").strip() or _env("JUDGE_API_KEY")
    endpoint_type = _resolve_endpoint_type(judge_cfg)
    gkey = (google_api_key or "").strip() or None

    if base_url:
        if not api_key:
            return ResolvedJudge(
                model=model,
                temperature=temperature,
                base_url=base_url,
                api_key=None,
                google_api_key=gkey,
                mode="http",
                endpoint_type=endpoint_type,
                ready=False,
                skip_reason="HTTP judge needs judge.api_key or JUDGE_API_KEY.",
            )
        return ResolvedJudge(
            model=model,
            temperature=temperature,
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            google_api_key=gkey,
            mode="http",
            endpoint_type=endpoint_type,
            ready=True,
        )

    if not gkey:
        return ResolvedJudge(
            model=model,
            temperature=temperature,
            base_url=None,
            api_key=None,
            google_api_key=None,
            mode="gemini",
            endpoint_type=endpoint_type,
            ready=False,
            skip_reason="Gemini judge needs simulator.google_api_key (no base_url).",
        )
    return ResolvedJudge(
        model=model,
        temperature=temperature,
        base_url=None,
        api_key=None,
        google_api_key=gkey,
        mode="gemini",
        endpoint_type=endpoint_type,
        ready=True,
    )
