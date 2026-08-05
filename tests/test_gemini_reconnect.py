"""Gemini Live WebSocket reconnect / transport-drop diagnostics."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from livekit_agent_simulator.gemini.live_session import GeminiCallerBridge


def _make_bridge() -> GeminiCallerBridge:
    class W:
        def __init__(self):
            self.events: list[tuple] = []

        def emit(self, kind, spec=None, **kw):
            self.events.append((kind, spec))

    bridge = object.__new__(GeminiCallerBridge)
    bridge.writer = W()
    bridge.end_call = asyncio.Event()
    bridge.transport_dropped = False
    return bridge


class _FakeClient:
    """Simulates google.genai Client.aio.live.connect.

    Fails the first `max_attempts - 1` calls with a transport error, then
    succeeds with a sentinel session object.
    """

    def __init__(self, failures: list[Exception] | None = None, session: object = None):
        self._failures = list(failures or [])
        self._session = session or object()
        self._calls = 0

    class _Live:
        def __init__(self, owner):
            self._owner = owner

        async def connect(self, *, model, config):
            self._owner._calls += 1
            if self._owner._failures:
                raise self._owner._failures.pop(0)
            return self._owner._session

    class _Aio:
        def __init__(self, owner):
            self._owner = owner
            self.live = _FakeClient._Live(owner)

    @property
    def aio(self) -> "_FakeClient._Aio":
        return _FakeClient._Aio(self)


@pytest.mark.asyncio
async def test_connect_retries_transport_error_then_succeeds() -> None:
    bridge = _make_bridge()
    client = _FakeClient(
        failures=[
            ConnectionError("APIError: 1006 None. abnormal closure [internal]"),
        ],
        session="session-ok",
    )
    session = await bridge._connect_live_with_retry(client, "m", object())
    assert session == "session-ok"
    assert client._calls == 2
    drops = [e for e in bridge.writer.events if e[0] == "sim.gemini_socket_drop"]
    assert len(drops) == 1
    assert drops[0][1]["attempt"] == 1
    assert drops[0][1]["retryable"] is True


@pytest.mark.asyncio
async def test_connect_gives_up_after_max_attempts() -> None:
    bridge = _make_bridge()
    client = _FakeClient(
        failures=[
            ConnectionError("APIError: 1006 None. abnormal closure [internal]"),
            ConnectionError("APIError: 1006 None. abnormal closure [internal]"),
            ConnectionError("APIError: 1006 None. abnormal closure [internal]"),
        ],
    )
    with pytest.raises(ConnectionError):
        await bridge._connect_live_with_retry(client, "m", object())
    assert client._calls == 3
    drops = [e for e in bridge.writer.events if e[0] == "sim.gemini_socket_drop"]
    assert len(drops) == 3


@pytest.mark.asyncio
async def test_connect_non_transport_error_does_not_retry() -> None:
    bridge = _make_bridge()
    client = _FakeClient(
        failures=[ValueError("bad config")],
    )
    with pytest.raises(ValueError):
        await bridge._connect_live_with_retry(client, "m", object())
    assert client._calls == 1
    drops = [e for e in bridge.writer.events if e[0] == "sim.gemini_socket_drop"]
    assert len(drops) == 1
    assert drops[0][1]["retryable"] is False


@pytest.mark.asyncio
async def test_pump_transport_drop_marks_bridge() -> None:
    """Mid-call socket drop sets `transport_dropped` and emits a diagnostic."""
    bridge = _make_bridge()
    bridge._mute_persona_audio = False

    class _RaisingIter:
        def __init__(self):
            self._raised = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._raised:
                self._raised = True
                raise ConnectionError("APIError: 1006 None. abnormal closure [internal]")
            raise StopAsyncIteration

    class _RaisingSession:
        def receive(self):
            return _RaisingIter()

    # Drive the pump's exception path directly via the private catch.
    await bridge._pump_gemini_events(
        _RaisingSession(),
        None,
    )
    assert bridge.transport_dropped is True
    kinds = [e[0] for e in bridge.writer.events]
    assert "sim.gemini_socket_drop" in kinds
    assert "sim.error" in kinds
