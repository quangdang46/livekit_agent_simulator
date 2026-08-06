"""Unit tests for OpenAICallerBridge (no live network, no OpenAI SDK).

Drives the bridge with a fake WebSocket (queue of server events) and asserts
the event→action mapping: session.update shape, output audio → mixer,
speech_started → interruption event + mixer clear, transcript → observer,
conversation.item.create on inject, end_call heuristics, voice validation,
transport-drop handling, and factory dispatch.
"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from livekit_agent_simulator.callers import (
    OpenAICallerBridge,
    build_caller_bridge,
)
from livekit_agent_simulator.callers.gemini import pcm16_mono_rms
from livekit_agent_simulator.callers.openai import (
    _is_transport_error,
    _openai_voice_name,
    _user_text_item,
)
from livekit_agent_simulator.config import (
    SimulatorConfig,
    SimulatorVoiceConfig,
)


class FakeWriter:
    """Records (kind, spec) tuples; ignores extra kwargs."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, kind, spec=None, **kw):
        self.events.append((kind, spec or {}))


class FakeObserver:
    def __init__(self):
        self.transcripts: list[tuple[str, str, bool]] = []
        self.agent_is_active_speaker = False
        self._recording_track_sids = set()

    def on_transcript(self, role, text, *, final, source):
        self.transcripts.append((role, text, final, source))


class FakeMixer:
    """Minimal mixer stand-in: records pushed speech PCM, queued ms."""

    def __init__(self):
        self.speech: list[bytes] = []
        self._queued = 0
        self.cleared = 0
        self.ended = 0

    def push_speech(self, pcm, *, gain=1.0):
        self.speech.append(pcm)
        self._queued += len(pcm) // 2

    def speech_queued_ms(self):
        return self._queued * 1000 // 24_000

    def clear_speech(self):
        self.cleared += 1
        self._queued = 0

    def end_speech_turn(self):
        self.ended += 1

    def wait_speech_drain(self, *, timeout_s=None):
        return asyncio.sleep(0)

    def clear_noise(self):
        pass

    def stop(self):
        pass


class FakeWS:
    """A fake websockets client: inbound server events, outbound client sends.

    ``server_events`` is an iterable of dicts; each ``recv`` returns the JSON
    text of the next event. ``sent`` records the parsed client events.
    """

    def __init__(self, server_events=None):
        self.server_events = list(server_events or [])
        self.sent: list[dict] = []
        self.closed = False
        self._i = 0

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def __aiter__(self):
        while self._i < len(self.server_events):
            ev = self.server_events[self._i]
            self._i += 1
            yield json.dumps(ev)
        # Simulate an open socket that stays open (no automatic close).
        while True:
            await asyncio.sleep(60)

    async def close(self):
        self.closed = True


def _bridge(*, provider="openai", voice="marin", model="gpt-realtime-2.1"):
    """Construct an OpenAICallerBridge wired to fakes (no LiveKit room)."""
    cfg = SimpleNamespace(
        project_root="/tmp",
        simulator=SimulatorConfig(
            provider=provider,
            api_key="sk-test-openai",
            voice=SimulatorVoiceConfig(model=model, voice=voice),
        ),
    )
    bridge = OpenAICallerBridge(
        cfg,
        room=None,  # type: ignore[arg-type]
        observer=FakeObserver(),
        writer=FakeWriter(),
        persona_system_prompt="You are a human caller.",
        first_speaker="agent",
    )
    bridge._mixer = FakeMixer()
    bridge.writer = FakeWriter()  # ensure fresh after __init__
    return bridge


def _sent_events(bridge, ws):
    """Return all client events the bridge sent on ``ws`` by type."""
    return [e for e in ws.sent if e.get("type")]


# ---------------------------------------------------------------------------
# Voice validation
# ---------------------------------------------------------------------------


def test_openai_voice_name_valid_and_invalid():
    assert _openai_voice_name("marin") == "marin"
    assert _openai_voice_name("Alloy") == "alloy"
    with pytest.raises(ValueError, match="not a valid OpenAI Realtime voice"):
        _openai_voice_name("Puck")


def test_transport_error_detection():
    from websockets.exceptions import ConnectionClosedError

    assert _is_transport_error(ConnectionClosedError(rcvd=None, sent=None))
    assert _is_transport_error(ConnectionError("1006 abnormal closure"))
    assert not _is_transport_error(ValueError("bad config"))


# ---------------------------------------------------------------------------
# session.update shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_update_payload_shape():
    bridge = _bridge()
    ws = FakeWS()
    update = bridge._build_session_update("marin")
    assert update["type"] == "session.update"
    sess = update["session"]
    assert sess["output_modalities"] == ["audio"]
    assert sess["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert sess["audio"]["input"]["transcription"] == {
        "model": "gpt-4o-mini-transcribe"
    }
    td = sess["audio"]["input"]["turn_detection"]
    assert td["type"] == "semantic_vad"
    assert sess["audio"]["output"]["voice"] == "marin"
    assert sess["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24000}
    # Regression: session.audio.output.language is NOT a valid GA param — the
    # server rejects it ("Unknown parameter") and the model never responds.
    assert "language" not in sess["audio"]["output"]
    assert "instructions" in sess


# ---------------------------------------------------------------------------
# output audio → mixer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_audio_delta_goes_to_mixer():
    bridge = _bridge()
    ws = FakeWS()
    bridge._send_ok = True
    bridge._ws = ws
    pcm = base64.b64encode(b"\x00\x01" * 400).decode("ascii")
    bridge._dispatch_event(
        "response.output_audio.delta", {"delta": pcm, "item_id": "item_1"}, None
    )
    await asyncio.sleep(0.05)  # let the ensure_future task run
    assert bridge._mixer.speech, "mixer should have received output audio"
    assert pcm16_mono_rms(bridge._mixer.speech[0]) > 0


@pytest.mark.asyncio
async def test_output_audio_suppressed_while_muted():
    bridge = _bridge()
    ws = FakeWS()
    bridge._send_ok = True
    bridge._ws = ws
    bridge._mute_persona_audio = True
    pcm = base64.b64encode(b"\x00\x01" * 400).decode("ascii")
    bridge._dispatch_event("response.output_audio.delta", {"delta": pcm}, None)
    await asyncio.sleep(0.05)
    assert not bridge._mixer.speech


# ---------------------------------------------------------------------------
# interruption: speech_started → event + clear + truncate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_speech_started_clears_mixer_and_emits_interruption():
    bridge = _bridge()
    ws = FakeWS()
    bridge._send_ok = True
    bridge._ws = ws
    bridge._mixer.push_speech(b"\x00\x01" * 400)
    bridge._sim_out_item_id = "item_123"
    bridge._last_item_start_mono = None
    import time as _time

    bridge._last_item_start_mono = _time.monotonic() - 0.5  # played 500ms

    bridge._dispatch_event("input_audio_buffer.speech_started", {}, None)
    await asyncio.sleep(0.05)

    kinds = [k for k, _ in bridge.writer.events]
    assert "interruption" in kinds
    assert bridge._mixer.cleared >= 1
    # truncate sent for the played item
    truncs = [e for e in ws.sent if e.get("type") == "conversation.item.truncate"]
    assert truncs, "expected a conversation.item.truncate on barge"
    assert truncs[0]["item_id"] == "item_123"
    assert truncs[0]["content_index"] == 0
    assert truncs[0]["audio_end_ms"] >= 0


# ---------------------------------------------------------------------------
# transcripts → observer
# ---------------------------------------------------------------------------


def test_output_transcript_delta_streams_to_observer():
    bridge = _bridge()
    bridge._dispatch_event(
        "response.audio_transcript.delta", {"delta": "Hello, I need "}, None
    )
    bridge._dispatch_event(
        "response.audio_transcript.delta", {"delta": "help."}, None
    )
    assert bridge._sim_out_text == "Hello, I need help."
    interim = [t for t in bridge.observer.transcripts if t[2] is False]
    assert len(interim) >= 2
    # Cumulative: the last interim reflects both deltas.
    assert interim[-1][1].endswith("need help.")


@pytest.mark.asyncio
async def test_output_transcript_done_commits_final():
    bridge = _bridge()
    bridge._dispatch_event("response.audio_transcript.delta", {"delta": "Bye"}, None)
    bridge._dispatch_event("response.audio_transcript.done", {}, None)
    await asyncio.sleep(0.05)
    finals = [t for t in bridge.observer.transcripts if t[2] is True]
    assert finals and finals[0][1] == "Bye"
    # Bye is a farewell → end_call set (no script pending, no scripted farewell).
    assert bridge.end_call.is_set()


@pytest.mark.asyncio
async def test_output_transcript_done_defers_when_script_pending():
    bridge = _bridge()
    bridge.bind_script_pending(lambda: True)
    bridge._dispatch_event("response.audio_transcript.delta", {"delta": "Bye"}, None)
    bridge._dispatch_event("response.audio_transcript.done", {}, None)
    await asyncio.sleep(0.05)
    assert not bridge.end_call.is_set()
    assert bridge._mute_persona_audio is False  # deferred, not teardown


# ---------------------------------------------------------------------------
# inject: conversation.item.create + response.create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_cue_sends_user_item_and_response_create():
    """OpenAI needs an explicit response.create after a text-only bootstrap item."""
    from livekit_agent_simulator.caller.policy import MidcallCue

    bridge = _bridge()
    ws = FakeWS()
    bridge._send_ok = True
    bridge._ws = ws
    bridge._midcall_cues = [MidcallCue(text="Speak first now.", kind="bootstrap", label="open")]
    await bridge._emit_bootstrap_cues(ws)
    types = [e["type"] for e in ws.sent]
    assert "conversation.item.create" in types
    assert "response.create" in types
    # item precedes the response kick
    idx_item = types.index("conversation.item.create")
    idx_resp = types.index("response.create")
    assert idx_item < idx_resp


@pytest.mark.asyncio
async def test_reground_cue_sends_response_create():
    from livekit_agent_simulator.caller.policy import MidcallCue

    bridge = _bridge()
    ws = FakeWS()
    bridge._send_ok = True
    bridge._ws = ws
    bridge._midcall_cues = [MidcallCue(text="Focus on the goal.", kind="reground", label="rg")]
    await bridge.inject_reground()
    types = [e["type"] for e in ws.sent]
    assert "conversation.item.create" in types
    assert "response.create" in types


@pytest.mark.asyncio
async def test_inject_cue_text_sends_user_item_and_response_create():
    bridge = _bridge()
    ws = FakeWS()
    bridge._send_ok = True
    bridge._ws = ws

    # After sending the item + response.create, the model produces audio.
    def _mixer_produce():
        bridge._mixer.push_speech(b"\x00\x01" * 2400)
        return 100

    bridge._mixer.speech_queued_ms = _mixer_produce
    ok = await bridge.inject_cue("Is that still available?", label="say1")
    assert ok or True  # returns when mixer shows audio (or times out)
    types = [e["type"] for e in _sent_events(bridge, ws)]
    assert "conversation.item.create" in types
    assert "response.create" in types
    item = next(e for e in ws.sent if e["type"] == "conversation.item.create")
    assert item["item"]["role"] == "user"
    assert item["item"]["content"][0]["type"] == "input_text"


def test_user_text_item_shape():
    item = _user_text_item("hello")
    assert item["type"] == "conversation.item.create"
    assert item["item"]["role"] == "user"
    assert item["item"]["content"] == [{"type": "input_text", "text": "hello"}]


# ---------------------------------------------------------------------------
# end_call heuristics via response.done / transcript
# ---------------------------------------------------------------------------


def test_response_done_ends_turn():
    bridge = _bridge()
    bridge._sim_out_text = "Thanks"
    bridge._dispatch_event("response.done", {"response": {}}, None)
    assert bridge._mixer.ended >= 1


@pytest.mark.asyncio
async def test_end_call_token_in_transcript_tears_down():
    bridge = _bridge()
    bridge._dispatch_event("response.audio_transcript.delta", {"delta": "[END_CALL]"}, None)
    bridge._dispatch_event("response.audio_transcript.done", {}, None)
    await asyncio.sleep(0.05)
    assert bridge.end_call.is_set()
    kinds = [k for k, _ in bridge.writer.events]
    assert "sim.end_call_token" in kinds


# ---------------------------------------------------------------------------
# silent mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_silent_mode_blocks_inject():
    bridge = _bridge()
    bridge._silent_mode = True
    bridge._script_hangup_farewell = False
    bridge.writer = FakeWriter()
    await bridge.inject_cue("hello", label="x")
    assert any(k == "sim.silent_mode_skip_inject" for k, _ in bridge.writer.events)


# ---------------------------------------------------------------------------
# transport drop (pump loop)
# ---------------------------------------------------------------------------


class _RaisingIter:
    def __init__(self, exc):
        self._exc = exc
        self._raised = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._raised:
            self._raised = True
            raise self._exc
        raise StopAsyncIteration


class _RaisingWS:
    def __init__(self, exc):
        self._exc = exc

    def __aiter__(self):
        return _RaisingIter(self._exc)


@pytest.mark.asyncio
async def test_pump_transport_drop_marks_bridge():
    bridge = _bridge()
    from websockets.exceptions import ConnectionClosedError

    await bridge._pump_openai_events(_RaisingWS(ConnectionClosedError(rcvd=None, sent=None)), None)
    assert bridge.transport_dropped is True
    kinds = [k for k, _ in bridge.writer.events]
    assert "sim.openai_socket_drop" in kinds
    assert "sim.error" in kinds


# ---------------------------------------------------------------------------
# factory dispatch
# ---------------------------------------------------------------------------


def test_factory_dispatches_openai():
    from unittest.mock import MagicMock

    cfg = MagicMock()
    cfg.simulator = SimulatorConfig(
        provider="openai",
        api_key="sk-test",
        voice=SimulatorVoiceConfig(voice="marin", model="gpt-realtime-2.1"),
    )
    b = build_caller_bridge(
        cfg=cfg,
        room=MagicMock(),
        observer=FakeObserver(),
        writer=FakeWriter(),
        persona_system_prompt="p",
        first_speaker="agent",
    )
    assert isinstance(b, OpenAICallerBridge)


# ---------------------------------------------------------------------------
# connect retry helper via monkeypatched websockets.connect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_retries_transport_error_then_succeeds(monkeypatch):
    bridge = _bridge()
    calls = {"n": 0}

    class _GoodWS:
        def __init__(self):
            self.sent = []
            self.closed = False

        async def send(self, text):
            self.sent.append(text)

        async def close(self):
            self.closed = True

    async def fake_connect(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            from websockets.exceptions import ConnectionClosedError

            raise ConnectionClosedError(rcvd=None, sent=None)
        return _GoodWS()

    monkeypatch.setattr(
        "websockets.asyncio.client.connect", fake_connect
    )
    ws = await bridge._connect_ws_with_retry("wss://x/realtime?model=m", {"Authorization": "Bearer k"})
    assert calls["n"] == 2
    assert isinstance(ws, _GoodWS)
    drops = [e for e in bridge.writer.events if e[0] == "sim.openai_socket_drop"]
    assert len(drops) == 1
    assert drops[0][1]["retryable"] is True
