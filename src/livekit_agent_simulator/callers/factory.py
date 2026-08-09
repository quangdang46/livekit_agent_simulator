"""Provider factory — selects the caller bridge from ``simulator.provider``.

Strategy + factory (same pattern as SimLeg / CallerPolicy / judge backend).
``simulator.provider`` is a simulator capability, never a scenario-mode
override (design lock). Adding a provider = one concrete bridge + one factory
branch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from livekit import rtc

if TYPE_CHECKING:
    from ..audio.local_recorder import LocalConversationRecorder
    from ..config import SimConfig
    from ..livekit.observer import Observer
    from ..logging.event_writer import EventWriter
    from .base import CallerBridge


def build_caller_bridge(
    *,
    cfg: SimConfig,
    room: rtc.Room,
    observer: Observer,
    writer: EventWriter,
    persona_system_prompt: str,
    first_speaker: str,
    recorder: LocalConversationRecorder | None = None,
    midcall_cues: list | None = None,
    voice_gain: float = 1.0,
    silent_mode: bool = False,
) -> CallerBridge:
    """Construct the caller bridge for ``cfg.simulator.provider``.

    Keyword-only args mirror ``GeminiCallerBridge.__init__`` so every provider
    receives the same context. Raises ``ConfigError`` for unknown providers
    (config already validates, so this is a backstop).
    """
    from ..config import ConfigError

    provider = cfg.simulator.provider
    if provider == "google":
        from .gemini import GeminiCallerBridge

        return GeminiCallerBridge(
            cfg,
            room,
            observer,
            writer,
            persona_system_prompt=persona_system_prompt,
            first_speaker=first_speaker,
            recorder=recorder,
            midcall_cues=midcall_cues,
            voice_gain=voice_gain,
            silent_mode=silent_mode,
        )
    if provider == "openai":
        from .openai import OpenAICallerBridge

        return OpenAICallerBridge(
            cfg,
            room,
            observer,
            writer,
            persona_system_prompt=persona_system_prompt,
            first_speaker=first_speaker,
            recorder=recorder,
            midcall_cues=midcall_cues,
            voice_gain=voice_gain,
            silent_mode=silent_mode,
        )
    raise ConfigError(f"Unknown simulator.provider {provider!r}")
