"""Concrete PublishSink: the ONLY way validated caller PCM reaches LiveKit.

Wraps ``CallerBridge.publish_validated_pcm`` (mixer push, no Realtime/AI
session involved) with two responsibilities the ``driver.PublishSink``
protocol requires:

  1. Staleness re-check (``orchestrator.is_stale``) — defense in depth
     against a TOCTOU race between the driver's pre-check and this call.
  2. Drain: ``publish()`` does not return until the pushed PCM has actually
     finished playing out of the mixer (reuses the bridge's existing
     ``drain_persona_speech`` — the same drain already used by
     ``script/runtime.py``'s turn-taking gate). This is what stops the
     driver from firing the NEXT action (including ``end``) while the
     previous action's audio is still mid-playback — see
     docs/contract-caller-wiring.md "serialization" note.

A drain that cannot be confirmed complete within ``drain_timeout_s`` raises
``PublishDrainTimeout`` — an execution/transport failure, never treated as
``CALLER_BEHAVIOR_VIOLATION`` by the driver.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from . import GenerationIdentity
from .driver import PublishDrainTimeout
from .orchestrator import Orchestrator

DEFAULT_DRAIN_TIMEOUT_S = 12.0
# Playout floor for the post-publish drain (see _drain): the mixer emits
# 10ms frames, so 5 frames ~= the first 50ms of actual utterance audio on
# the wire — past the preroll waterline, well before any utterance ends.
# Chosen over a time-based floor so short utterances (which finish in a
# few hundred ms) are unaffected: the floor only delays the *check*, and
# the subsequent empty-queue read still returns immediately when done.
_PLAYOUT_FLOOR_FRAMES = 5


@dataclass
class BridgePublishSink:
    bridge: Any  # CallerBridge (duck-typed; publish_validated_pcm required)
    orchestrator: Orchestrator
    writer: Any = None  # EventWriter, optional
    drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S

    async def publish(
        self,
        pcm: bytes,
        identity: GenerationIdentity,
        *,
        label: str,
        gain: float = 1.0,
    ) -> bool:
        if self.orchestrator.is_stale(identity):
            self._emit("contract.publish_dropped_stale", {"label": label, "identity": identity.behavior_id})
            return False

        ok = bool(self.bridge.publish_validated_pcm(pcm, gain=gain))
        if not ok:
            self._emit("contract.published", {"label": label, "bytes": len(pcm), "ok": False})
            return False

        self._emit("contract.published", {"label": label, "bytes": len(pcm), "ok": True})
        drained = await self._drain(label, pcm_len=len(pcm))
        if not drained:
            raise PublishDrainTimeout(
                f"{label}: caller audio did not finish draining within {self.drain_timeout_s}s"
            )
        return True

    async def _drain(self, label: str, *, pcm_len: int = 0) -> bool:
        """Await mixer drain; return True iff confirmed empty afterward.

        Run 047 proved a post-`end_speech_turn` drain is not sufficient:
        the mixer only starts *emitting* a freshly-pushed turn after
        `speech_preroll_ms` of samples are buffered, so a drain that runs
        immediately after a large push can observe an empty output queue
        (nothing emitted yet) and return while the utterance is still
        queued — the next turn's push then concatenates onto the wire with
        no gap, and the agent's STT merges both turns into one.

        Run 051 proved the fix must be ORDERED, not just added: the old
        code ran the full drain FIRST (its own timeout elapsed while the
        queue was still full), and only then waited for a playout floor —
        by which time there was no budget left to wait on, so the re-check
        read the still-full queue once and returned False immediately.
        Correct order, single shared budget: floor first (prove playout
        started), then drain (prove playout finished), with the deadline
        spanning both.
        """
        import asyncio as _asyncio
        import time as _time

        mixer = getattr(self.bridge, "_mixer", None)
        frames_at_publish = None
        if mixer is not None:
            try:
                frames_at_publish = mixer.frames_written
            except Exception:  # noqa: BLE001 — mixer without a frame counter
                frames_at_publish = None
        drain_fn = getattr(self.bridge, "drain_persona_speech", None)
        if mixer is None:
            # No mixer to inspect (e.g. a test double without one) — the
            # drain call is the only signal we have; trust it.
            if drain_fn is not None:
                await drain_fn(timeout_s=self.drain_timeout_s)
            return True
        # The budget must cover the whole utterance, not a fixed window: a
        # ~5s turn needs ~5s of playout AFTER the floor. PCM is 16-bit mono,
        # so samples = bytes/2 at the mixer's own rate when known (else the
        # 24k contract TTS rate), plus headroom for the preroll waterline +
        # frame period. Run 053: a fixed 12s budget expired mid-utterance
        # (~5.1s audio + floor wait), the next turn concatenated gaplessly,
        # and the agent's STT merged both turns into one.
        try:
            _mixer_rate = int(getattr(mixer, "sample_rate", 0) or 0)
        except Exception:  # noqa: BLE001
            _mixer_rate = 0
        _pcm_seconds = (pcm_len / 2 / _mixer_rate) if _mixer_rate > 0 else (pcm_len / 2 / 24000)
        _budget = max(self.drain_timeout_s, _pcm_seconds + 4.0)
        deadline = _time.monotonic() + _budget
        if frames_at_publish is not None:
            # Playout floor: the mixer must have emitted at least
            # _PLAYOUT_FLOOR_FRAMES frames carrying this publish before an
            # empty queue means "done" (see run-047 note above).
            while True:
                try:
                    if mixer.frames_written >= frames_at_publish + _PLAYOUT_FLOOR_FRAMES:
                        break
                except Exception:  # noqa: BLE001 — counter read failure
                    break
                if _time.monotonic() >= deadline:
                    self._emit(
                        "contract.publish_drain_timeout",
                        {"label": label, "reason": "playout floor not reached", "timeout_s": self.drain_timeout_s},
                    )
                    return False
                await _asyncio.sleep(0.02)
        # Real drain AFTER the floor, on the REMAINING budget: wait until
        # the mixer queue is actually empty (utterance fully played out).
        # drain_fn (wait_speech_drain) waits for empty-queue AND turn-end;
        # re-check the queue after it because it returns early on its own
        # timeout without reporting which condition failed.
        if drain_fn is not None:
            remaining = max(0.5, deadline - _time.monotonic())
            await drain_fn(timeout_s=remaining)
        queued = mixer.speech_queued_ms()
        if asyncio.iscoroutine(queued):
            queued = await queued
        still_draining = bool(queued and queued > 0)
        if still_draining:
            self._emit(
                "contract.publish_drain_timeout",
                {"label": label, "queued_ms": queued, "timeout_s": self.drain_timeout_s},
            )
            return False
        return True

    def _emit(self, kind: str, spec: dict[str, Any]) -> None:
        if self.writer is not None:
            self.writer.emit(kind, spec=spec, source="sim.contract", include_dialogue=False)


__all__ = ["BridgePublishSink", "DEFAULT_DRAIN_TIMEOUT_S"]
