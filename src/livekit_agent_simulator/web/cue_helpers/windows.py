"""Cue window estimation: active-speaker windows, interim starts, text-length estimate."""

from __future__ import annotations

from typing import Any

from .source_priority import source_rank
from ..report_time import _mono_to_audio_ms


def estimate_utterance_ms(text: str, *, role: str) -> int:
    t = (text or "").strip()
    if not t:
        return 800 if role == "agent" else 600
    words = [w for w in t.replace("\n", " ").split(" ") if w]
    units = max(len(words), max(1, len(t) // 4))
    ms = int(units * (95 if role == "agent" else 85))
    lo, hi = (700, 22_000) if role == "agent" else (500, 14_000)
    return max(lo, min(hi, ms))


def collect_interim_starts(
    events: list[dict[str, Any]],
    t0: int,
    duration_ms: int | None,
) -> list[tuple[str, int, str, str]]:
    out: list[tuple[str, int, str, str]] = []
    for e in events:
        kind = str(e.get("kind") or "")
        if not kind.startswith("transcript.") or not kind.endswith(".interim"):
            continue
        if "agent" in kind:
            role = "agent"
        elif "user" in kind:
            role = "user"
        else:
            continue
        text = ((e.get("spec") or {}).get("text") or "").strip()
        if not text:
            continue
        try:
            mono = int(e.get("ts_mono_ms") or 0)
        except (TypeError, ValueError):
            continue
        ms = _mono_to_audio_ms(mono, t0, duration_ms)
        if ms is None:
            continue
        out.append((role, ms, text, str(e.get("source") or "")))
    return out


def collect_agent_active_windows(
    events: list[dict[str, Any]],
    t0: int,
    duration_ms: int | None,
) -> list[tuple[int, int]]:
    points: list[tuple[int, bool]] = []
    for e in events:
        if str(e.get("kind") or "") != "room.active_speakers":
            continue
        ids = (e.get("spec") or {}).get("identities") or []
        agent_on = any(
            str(i).startswith("agent-") or "agent" in str(i).lower() for i in ids
        )
        try:
            mono = int(e.get("ts_mono_ms") or 0)
        except (TypeError, ValueError):
            continue
        ms = _mono_to_audio_ms(mono, t0, duration_ms)
        if ms is None:
            continue
        points.append((ms, agent_on))
    points.sort()
    windows: list[tuple[int, int]] = []
    start: int | None = None
    last_on: int | None = None
    gap_close_ms = 2800
    for ms, on in points:
        if on:
            if start is None:
                start = ms
            elif last_on is not None and ms - last_on > gap_close_ms:
                windows.append((start, last_on + 600))
                start = ms
            last_on = ms
        else:
            if start is not None and last_on is not None:
                windows.append((start, last_on + 600))
            start = None
            last_on = None
    if start is not None and last_on is not None:
        end = last_on + 600
        if duration_ms is not None:
            end = min(end, int(duration_ms))
        windows.append((start, end))
    if not windows:
        return windows
    windows.sort()
    merged = [windows[0]]
    for w0, w1 in windows[1:]:
        p0, p1 = merged[-1]
        if w0 <= p1 + 1500:
            merged[-1] = (p0, max(p1, w1))
        else:
            merged.append((w0, w1))
    return merged


def best_interim_start(
    interims: list[tuple[str, int, str, str]],
    *,
    role: str,
    final_ms: int,
    text: str,
    est_ms: int,
    prefer_source: str | None = None,
) -> int | None:
    window_lo = max(0, final_ms - est_ms - 3000)
    window_hi = final_ms - 500
    if window_hi <= window_lo:
        return None
    final_l = text.lower().strip()
    candidates: list[tuple[int, int]] = []
    for r, ms, itext, src in interims:
        if r != role:
            continue
        if ms < window_lo or ms > window_hi:
            continue
        il = itext.lower().strip()
        if not il:
            continue
        if not (
            il in final_l
            or final_l.startswith(il[: min(12, len(il))])
            or il.startswith(final_l[: min(12, len(final_l))])
        ):
            continue
        rank = source_rank(src, role)
        if prefer_source and src == prefer_source:
            rank = -1
        candidates.append((rank, ms))
    if not candidates:
        return None
    candidates.sort()
    best_rank = candidates[0][0]
    return min(ms for rank, ms in candidates if rank == best_rank)


def best_active_window(
    windows: list[tuple[int, int]],
    *,
    final_ms: int,
    est_ms: int,
) -> tuple[int, int] | None:
    best: tuple[int, int, int] | None = None
    for w0, w1 in windows:
        if final_ms < w0 - 300:
            continue
        if final_ms > w1 + 2000:
            continue
        span = max(1, w1 - w0)
        score = abs(w1 - final_ms) + abs(span - est_ms) // 3
        if best is None or score < best[0]:
            best = (score, w0, w1)
    if best is None:
        return None
    return best[1], best[2]
