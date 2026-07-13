"""Source ranking and ghost-STT filtering for player cue building."""

from __future__ import annotations

from typing import Any

_USER_SOURCE_RANK = {
    "sim.gemini": 0,
    "data": 2,
    "lk.transcription": 3,
}
_AGENT_SOURCE_RANK = {
    "data": 0,
    "lk.transcription": 1,
    "sim.gemini": 2,
}


def source_rank(source: str | None, role: str) -> int:
    s = (source or "").strip()
    table = _USER_SOURCE_RANK if role == "user" else _AGENT_SOURCE_RANK
    if s in table:
        return table[s]
    if s and s not in ("sim.gemini", "lk.transcription"):
        return 1 if role == "user" else 0
    return 9


def texts_similar(a: str, b: str) -> bool:
    a = a.lower().strip()
    b = b.lower().strip()
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    return inter >= max(2, min(len(ta), len(tb)) * 0.5)
