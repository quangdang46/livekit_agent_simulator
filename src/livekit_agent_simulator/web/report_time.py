"""Shared time/event loading helpers for report player cue builders."""

from __future__ import annotations

import json
import wave
from pathlib import Path
from typing import Any

# Marker kinds exposed to the report player (stable API for the UI).
MARKER_BARGE_IN = "barge_in"
MARKER_SCRIPT_CUE = "script_cue"
MARKER_SILENCE_WAIT = "silence_wait"
MARKER_SILENCE = "silence"
MARKER_INTERRUPTION = "interruption"
MARKER_RECOVERY = "recovery"
MARKER_BACKCHANNEL = "backchannel"
MARKER_FALSE_INTERRUPT = "false_interrupt"
MARKER_DTMF = "dtmf"
MARKER_TOOL = "tool"
MARKER_TOOL_ERROR = "tool_error"

def _wav_duration_ms(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 1
            return int(frames * 1000 / rate)
    except Exception:
        return None


def _load_events(events_path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not events_path.exists():
        return events
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _resolve_audio_t0_ms(meta: dict[str, Any], events: list[dict[str, Any]]) -> int:
    audio = meta.get("audio") if isinstance(meta.get("audio"), dict) else {}
    if audio.get("t0_mono_ms") is not None:
        try:
            return max(0, int(audio["t0_mono_ms"]))
        except (TypeError, ValueError):
            pass
    # Fallback for older reports: first transcript-ish event.
    for e in events:
        kind = str(e.get("kind") or "")
        if kind.startswith("transcript.") or kind in (
            "sim.mic_published",
            "sim.gemini_connected",
        ):
            try:
                return max(0, int(e.get("ts_mono_ms") or 0))
            except (TypeError, ValueError):
                continue
    return 0


def _mono_to_audio_ms(mono: int, t0: int, duration_ms: int | None) -> int | None:
    start_ms = max(0, mono - t0)
    if duration_ms is not None and start_ms > duration_ms + 2000:
        return None
    return start_ms


def _clamp_end(start_ms: int, end_ms: int, duration_ms: int | None) -> int:
    end = max(start_ms + 120, end_ms)
    if duration_ms is not None:
        end = min(end, max(start_ms + 120, duration_ms))
    return end


