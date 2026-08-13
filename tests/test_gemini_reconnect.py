"""Gemini Live WebSocket reconnect / transport-drop diagnostics."""

from __future__ import annotations

import asyncio

import pytest

from google.genai import types

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
    bridge._resume_handle = None
    bridge._reconnect_required = asyncio.Event()
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

    class _RaisingSession:
        async def receive(self):
            # Async generator — raises on first iteration, cross-platform
            # (a custom __anext__ iter can behave differently across Python/OS).
            raise ConnectionError("APIError: 1006 None. abnormal closure [internal]")
            yield  # pragma: no cover — never reached

    await bridge._pump_gemini_events(_RaisingSession(), None)
    assert bridge.transport_dropped is True
    kinds = [e[0] for e in bridge.writer.events]
    assert "sim.gemini_socket_drop" in kinds
    assert "sim.error" in kinds


@pytest.mark.asyncio
async def test_pump_receive_timeout_with_handle_signals_reconnect() -> None:
    """A receive() TimeoutError with a resumption handle is a retryable drop.

    The pump must signal `_reconnect_required` (so run() reconnects) and NOT
    set `end_call` — otherwise a transient 15s receive-timeout mid-call would
    kill the call instead of resuming with the saved handle.
    """
    bridge = _make_bridge()
    bridge._mute_persona_audio = False
    bridge._resume_handle = "h123"  # handle already saved by an earlier update

    class _TimeoutSession:
        async def receive(self):
            raise asyncio.TimeoutError
            yield  # pragma: no cover — never reached

    await bridge._pump_gemini_events(_TimeoutSession(), None)
    assert bridge._reconnect_required.is_set()
    assert not bridge.end_call.is_set()  # do not kill the call
    assert bridge.transport_dropped is False  # resumable → not a fatal drop
    kinds = [e[0] for e in bridge.writer.events]
    assert "sim.gemini_socket_drop" in kinds
    assert "sim.error" in kinds


def _session_yielding(messages: list, after_each: callable | None = None) -> object:
    """A session whose receive() yields the given LiveServerMessage objects.

    ``after_each`` is called after each yield (e.g. to set end_call so the pump's
    ``while not end_call`` loop terminates after the control message).
    """

    class _Iter:
        def __init__(self, msgs):
            self._msgs = list(msgs)
            self._i = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._i >= len(self._msgs):
                raise StopAsyncIteration
            m = self._msgs[self._i]
            self._i += 1
            if after_each:
                after_each()
            return m

    class _S:
        def __init__(self, msgs):
            self._msgs = msgs

        def receive(self):
            return _Iter(self._msgs)

    return _S(messages)


@pytest.mark.asyncio
async def test_pump_go_away_signals_reconnect() -> None:
    """go_away sets _reconnect_required (not end_call / transport_dropped)."""
    bridge = _make_bridge()
    go = types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="30s"))
    await bridge._pump_gemini_events(_session_yielding([go]), None)

    assert bridge._reconnect_required.is_set()
    assert not bridge.end_call.is_set()  # graceful, not a hang-up
    assert bridge.transport_dropped is False  # not an abnormal drop
    kinds = [e[0] for e in bridge.writer.events]
    assert "sim.gemini_go_away" in kinds


@pytest.mark.asyncio
async def test_pump_saves_resumption_handle() -> None:
    """session_resumption_update(new_handle, resumable) is saved for reconnect."""
    bridge = _make_bridge()
    msg = types.LiveServerMessage(
        session_resumption_update=types.LiveServerSessionResumptionUpdate(
            resumable=True, new_handle="h123"
        )
    )
    # A resumption-only message has no server_content → loop continues; set
    # end_call right after the yield so the pump's while-loop terminates.
    await bridge._pump_gemini_events(
        _session_yielding([msg], after_each=lambda: bridge.end_call.set()), None
    )

    assert bridge._resume_handle == "h123"
    kinds = [e[0] for e in bridge.writer.events]
    assert "sim.gemini_resumption_handle" in kinds


@pytest.mark.asyncio
async def test_pump_ignores_non_resumable_update() -> None:
    """resumable=False must NOT overwrite the saved handle."""
    bridge = _make_bridge()
    bridge._resume_handle = "prev"
    msg = types.LiveServerMessage(
        session_resumption_update=types.LiveServerSessionResumptionUpdate(
            resumable=False, new_handle="stale"
        )
    )
    await bridge._pump_gemini_events(
        _session_yielding([msg], after_each=lambda: bridge.end_call.set()), None
    )

    assert bridge._resume_handle == "prev"  # unchanged
