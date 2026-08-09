"""Gemini Live WebSocket reconnect / transport-drop diagnostics."""

from __future__ import annotations

import asyncio

import pytest

from livekit_agent_simulator.callers.gemini import GeminiCallerBridge


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


class _FakeCM:
    """Mimics the SDK's `_AsyncGeneratorContextManager` for `live.connect`."""

    def __init__(self, fail: Exception | None = None, session: object = None):
        self.fail = fail
        self.session = session or object()
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        self.entered += 1
        if self.fail is not None:
            raise self.fail
        return self.session

    async def __aexit__(self, *exc):
        self.exited += 1
        return False


class _FakeClient:
    def __init__(self, cms: list[_FakeCM]):
        self._cms = list(cms)
        self._calls = 0

    class _Live:
        def __init__(self, owner):
            self._owner = owner

        def connect(self, *, model, config):
            self._owner._calls += 1
            return self._owner._cms.pop(0)

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
    cm1 = _FakeCM(fail=ConnectionError("APIError: 1006 None. abnormal closure [internal]"))
    cm2 = _FakeCM(session="session-ok")
    client = _FakeClient([cm1, cm2])

    cm, session = await bridge._connect_live_with_retry(client, "m", object())
    assert session == "session-ok"
    assert cm is cm2  # the succeeded manager is returned for teardown
    assert client._calls == 2
    drops = [e for e in bridge.writer.events if e[0] == "sim.gemini_socket_drop"]
    assert len(drops) == 1
    assert drops[0][1]["attempt"] == 1
    assert drops[0][1]["retryable"] is True
    assert cm1.exited == 1  # failed manager was closed


@pytest.mark.asyncio
async def test_connect_gives_up_after_max_attempts() -> None:
    bridge = _make_bridge()
    err = ConnectionError("APIError: 1006 None. abnormal closure [internal]")
    client = _FakeClient([_FakeCM(fail=err), _FakeCM(fail=err), _FakeCM(fail=err)])

    with pytest.raises(ConnectionError):
        await bridge._connect_live_with_retry(client, "m", object())
    assert client._calls == 3
    drops = [e for e in bridge.writer.events if e[0] == "sim.gemini_socket_drop"]
    assert len(drops) == 3


@pytest.mark.asyncio
async def test_connect_non_transport_error_does_not_retry() -> None:
    bridge = _make_bridge()
    client = _FakeClient([_FakeCM(fail=ValueError("bad config"))])

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

    await bridge._pump_gemini_events(_RaisingSession(), None)
    assert bridge.transport_dropped is True
    kinds = [e[0] for e in bridge.writer.events]
    assert "sim.gemini_socket_drop" in kinds
    assert "sim.error" in kinds
