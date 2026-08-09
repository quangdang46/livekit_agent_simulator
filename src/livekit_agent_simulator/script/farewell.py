"""Portable spoken farewells for Script hang_up (human-caller fidelity)."""

from __future__ import annotations

# Keep short — Gemini Live mic TTS + room hear a natural close before disconnect.
_DEFAULTS: dict[str, str] = {
    "en": "Okay, thanks. Bye.",
    "en-us": "Okay, thanks. Bye.",
    "en-gb": "Okay, thanks. Bye.",
    "vi": "Cảm ơn bạn. Tạm biệt.",
    "vi-vn": "Cảm ơn bạn. Tạm biệt.",
    "ja": "ありがとうございます。失礼します。",
    "ja-jp": "ありがとうございます。失礼します。",
}


def default_hangup_farewell(language: str | None) -> str:
    """Return a short goodbye for empty hang_up.say (never silent disconnect)."""
    raw = (language or "en-US").strip().lower().replace("_", "-")
    if raw in _DEFAULTS:
        return _DEFAULTS[raw]
    base = raw.split("-", 1)[0]
    return _DEFAULTS.get(base, _DEFAULTS["en"])
