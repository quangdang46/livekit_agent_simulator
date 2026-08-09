"""Caller bridge providers — Gemini Live and OpenAI Realtime simulated caller.

Strategy + factory: consumers drive any bridge through ``CallerBridge``
(``callers/base.py``); ``callers/factory.py`` selects by ``simulator.provider``.
"""

from __future__ import annotations

from .base import CallerBridge
from .end_call import (
    END_CALL_TOKEN,
    contains_end_call_signal,
    contains_farewell_signal,
    should_end_call_on_turn,
    strip_end_call_signal,
    strip_farewell_signal,
)
from .factory import build_caller_bridge
from .gemini import GeminiCallerBridge
from .openai import OpenAICallerBridge

__all__ = [
    "CallerBridge",
    "END_CALL_TOKEN",
    "GeminiCallerBridge",
    "OpenAICallerBridge",
    "build_caller_bridge",
    "contains_end_call_signal",
    "contains_farewell_signal",
    "should_end_call_on_turn",
    "strip_end_call_signal",
    "strip_farewell_signal",
]
