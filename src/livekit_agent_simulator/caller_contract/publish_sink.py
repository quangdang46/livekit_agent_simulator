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
        drained = await self._drain(label)
        if not drained:
            raise PublishDrainTimeout(
                f"{label}: caller audio did not finish draining within {self.drain_timeout_s}s"
            )
        return True

    async def _drain(self, label: str) -> bool:
        """Await mixer drain; return True iff confirmed empty afterward."""
        drain_fn = getattr(self.bridge, "drain_persona_speech", None)
        if drain_fn is not None:
            await drain_fn(timeout_s=self.drain_timeout_s)

        mixer = getattr(self.bridge, "_mixer", None)
        if mixer is None:
            # No mixer to inspect (e.g. a test double without one) — the
            # drain call above is the only signal we have; trust it.
            return True
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
