"""Concrete AgentTurnWait: observes the existing Observer for agent turns.

No new LiveKit plumbing — the Observer already tracks agent activity; this
module only polls those fields. Returns ``None`` on timeout (never raises)
so the driver can distinguish "agent slow" from "caller violation" (see
``driver.AgentTurnWait`` docstring).

Polling tiers (checked in order, no extra condition waits — one poll loop):

  1. ``last_agent_final_*``: LiveKit final transcript (lk.transcription) or
     a session-protocol transcript folded in by the Observer
     (``on_transcript(role=agent, final=True)``) — the BEST evidence: an
     actual agent sentence.
  2. Session snapshot: the Observer's RemoteSession ``chat_history`` digest
     (``AgentSessionObserver``) is fetched once at deadline entry; if it
     already contains a fresh agent message, that wins immediately without
     waiting out the timeout.
  3. Audio-activity floor: ``agent_has_spoken`` (active-speaker energy, real
     audio frames on the agent track) — the WORST evidence allowed: proves
     the agent DID talk but its words were never transcribed. Returned as a
     ``"[untranscribed agent speech]"`` marker (never an empty string, never
     a guess at the content) so BehaviorEvaluator can still distinguish
     "agent answered, words unknown" from "agent said nothing".

This tiering is why the run 011 regression class (agent answered audibly on
voice_ai.transcript/topic + R-channel PCM, but no ``transcript.agent.final``
landed because ``first_speaker=user`` + ``_user_has_spoken=False`` made the
Observer tag it ``transcript.agent.preamble``) can never recur as
AGENT_TIMEOUT.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

_UNTRANSCRIBED_MARKER = "[untranscribed agent speech]"


@dataclass
class ObserverAgentWait:
    """Polls the Observer for agent-turn evidence (tiers 1–3, see docstring)."""

    observer: Any
    poll_s: float = 0.1
    # Snapshot fetch is attempted lazily once, only if tier-1 evidence has
    # not appeared by this fraction of the timeout (0.5 = halfway). Kept as
    # a field so tests can force the timing deterministically.
    snapshot_fraction: float = 0.5
    _snapshot_attempted: bool = field(default=False, repr=False)

    def is_agent_speaking_now(self) -> bool:
        """Realtime agent-speech signal for trigger gating (Slice 4).

        One-line delegate to the Observer's active-speaker flag — no new
        thread, no new subscription. Used by the driver's trigger-wait
        helpers (agent_speaking/silence), never by the blocking
        ``wait_agent_turn`` path.
        """
        return bool(getattr(self.observer, "agent_is_active_speaker", False))

    def last_speech_at_ms(self) -> float | None:
        """Monotonic-ms timestamp of the latest agent-speech evidence, or
        None when the agent has never demonstrably spoken.

        An ACTIVE speaker is ongoing speech, not dead air: while the flag
        is up this returns "now" regardless of when the last transcript
        final landed (a long utterance's stale final must never read as
        silence — otherwise the hold watchdog could hang up mid-sentence).
        Otherwise the transcript final timestamp (edge-triggered, precise).
        """
        if bool(getattr(self.observer, "agent_is_active_speaker", False)):
            return time.monotonic() * 1000.0
        mono = getattr(self.observer, "last_agent_final_mono", None)
        if isinstance(mono, (int, float)):
            return float(mono) * 1000.0
        return None

    async def wait_agent_turn(self, *, timeout_s: float) -> str | None:
        deadline = time.monotonic() + timeout_s
        seen_at_start = getattr(self.observer, "last_agent_final_mono", None)
        # A final that already exists at call time (preamble-shape: the agent
        # answered before wait_agent_turn was even entered, e.g. run 011)
        # is FRESH evidence, not stale history — accept it immediately.
        preexisting_mono = seen_at_start
        preexisting_text = getattr(self.observer, "last_agent_final_text", None)
        still_speaking_at_start = bool(
            getattr(self.observer, "agent_is_active_speaker", False)
        )
        if (
            preexisting_mono is not None
            and preexisting_text
            and not still_speaking_at_start
        ):
            return str(preexisting_text)
        saw_audio = False
        while time.monotonic() < deadline:
            # Tier 1: final transcript (best evidence).
            final_mono = getattr(self.observer, "last_agent_final_mono", None)
            final_text = getattr(self.observer, "last_agent_final_text", None)
            still_speaking = bool(getattr(self.observer, "agent_is_active_speaker", False))
            if (
                final_mono is not None
                and final_mono != seen_at_start
                and final_text
                and not still_speaking
            ):
                return str(final_text)

            # Tier 3 latch: remember that the agent demonstrably talked.
            if getattr(self.observer, "agent_has_spoken", False):
                saw_audio = True

            # Tier 2: session snapshot, attempted once past snapshot_fraction
            # of the timeout (and only if tier 1 still has nothing).
            elapsed = timeout_s - (deadline - time.monotonic())
            if not self._snapshot_attempted and elapsed >= timeout_s * self.snapshot_fraction:
                self._snapshot_attempted = True
                snapshot_text = await self._latest_session_agent_text(
                    exclude_before=seen_at_start
                )
                if snapshot_text:
                    return snapshot_text

            await asyncio.sleep(self.poll_s)

        # Timeout: tier-3 floor — agent provably talked, words unknown.
        if saw_audio:
            return _UNTRANSCRIBED_MARKER
        return None

    async def _latest_session_agent_text(self, *, exclude_before: Any) -> str | None:
        """Best-effort RemoteSession chat_history digest (tier 2).

        Never raises: the session may be absent (unit-test fakes), detached,
        or mid-teardown — any failure simply means "no snapshot evidence",
        which is NOT a timeout yet; the poll loop keeps waiting for tiers
        1/3 until the deadline.
        """
        try:
            session = getattr(self.observer, "_agent_session", None)
            if session is None:
                return None
            fetch = getattr(session, "fetch_session_snapshot", None)
            if fetch is None:
                return None
            result = fetch()
            if asyncio.iscoroutine(result):
                await result
            history = getattr(session, "chat_history", None) or getattr(
                session, "history", None
            )
            if not history:
                return None
            items = list(history) if isinstance(history, (list, tuple)) else []
            for item in reversed(items):
                if not isinstance(item, dict):
                    continue
                role = item.get("role") or item.get("speaker")
                text = item.get("text") or item.get("content")
                if role == "agent" and isinstance(text, str) and text.strip():
                    return text.strip()
        except Exception:  # noqa: BLE001 — snapshot is best-effort only
            return None
        return None


__all__ = ["ObserverAgentWait"]
