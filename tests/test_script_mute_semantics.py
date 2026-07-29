"""Post-cue mute: wait holds silence; speak/line must not long-mute freestyle."""

from __future__ import annotations

import array
import asyncio
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from livekit_agent_simulator.script.models import ScriptStep
from livekit_agent_simulator.script.runtime import ScriptRunner


class _FakeBridge:
    def __init__(self) -> None:
        self.suppress_calls: list[int] = []
        self.silence_calls: list[int] = []
        self.injected: list[str] = []
        self.nudges: list[str] = []
        self.end_call = asyncio.Event()

    def suppress_persona_output(self, duration_ms: int) -> None:
        self.suppress_calls.append(int(duration_ms))

    def begin_scripted_user_silence(
        self, duration_ms: int, *, grace_s: float = 20.0, mute_persona: bool = False
    ) -> None:
        self.silence_calls.append(int(duration_ms))
        if mute_persona:
            self.suppress_persona_output(duration_ms)

    async def inject_cue(self, text: str, **kwargs: Any) -> None:
        self.injected.append(str(text))

    async def nudge_freestyle_answer(self, agent_hint: str = "") -> None:
        self.nudges.append(str(agent_hint or ""))

    def sim_hang_up(self) -> None:
        self.end_call.set()


def _runner(steps: list[ScriptStep], bridge: _FakeBridge) -> ScriptRunner:
    observer = MagicMock()
    observer.agent_is_active_speaker = False
    observer.agent_has_spoken = True
    observer.agent_active_duration_ms.return_value = 0
    writer = MagicMock()
    return ScriptRunner(steps, observer, bridge, writer)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_speak_line_does_not_suppress_for_silence_after_cue_ms() -> None:
    """silence_after_cue_ms on speak is not a freestyle blackout (inject drains TTS)."""
    bridge = _FakeBridge()
    step = ScriptStep(
        id="open",
        trigger="silence",
        delay_ms=0,
        say="Hello, my name is Mai",
        action="speak",
        silence_after_cue_ms=45_000,
    )
    runner = _runner([step], bridge)
    await runner._fire(step, waited_ms=0)
    assert bridge.injected == ["Hello, my name is Mai"]
    assert bridge.suppress_calls == []
    assert bridge.silence_calls == []


@pytest.mark.asyncio
async def test_wait_still_holds_intentional_silence() -> None:
    bridge = _FakeBridge()
    step = ScriptStep(
        id="quiet",
        trigger="silence",
        delay_ms=0,
        action="wait",
        silence_after_cue_ms=50,
        mute_persona=True,
    )
    runner = _runner([step], bridge)
    await runner._fire(step, waited_ms=0)
    assert bridge.silence_calls == [50]
    assert bridge.suppress_calls == [50]
    assert bridge.injected == []


@pytest.mark.asyncio
async def test_wait_default_does_not_mute_persona() -> None:
    """Human-caller default: pacing waits keep freestyle unmuted."""
    bridge = _FakeBridge()
    step = ScriptStep(
        id="pace",
        trigger="silence",
        delay_ms=0,
        action="wait",
        silence_after_cue_ms=50,
    )
    runner = _runner([step], bridge)
    await runner._fire(step, waited_ms=0)
    assert bridge.silence_calls == [50]
    assert bridge.suppress_calls == []


@pytest.mark.asyncio
async def test_wait_mute_persona_false_paces_without_suppress() -> None:
    """Pacing waits must not blackout freestyle between Script milestones."""
    bridge = _FakeBridge()
    step = ScriptStep(
        id="pace",
        trigger="silence",
        delay_ms=0,
        action="wait",
        silence_after_cue_ms=50,
        mute_persona=False,
    )
    runner = _runner([step], bridge)
    await runner._fire(step, waited_ms=0)
    assert bridge.silence_calls == [50]
    assert bridge.suppress_calls == []
    assert bridge.injected == []


def test_script_speak_directive_verbatim_only_no_freestyle_tail() -> None:
    from livekit_agent_simulator.gemini.live_session import script_speak_directive

    d = script_speak_directive("Is that still available?")
    assert "Is that still available?" in d
    assert "<<<" in d and ">>>" in d
    assert "UNMISTAKABLY" in d
    assert "HUMAN CALLER" in d
    assert "never speak as them" in d.lower() or "never speak as" in d.lower()
    assert "wait silently" not in d.lower()
    # Freestyle-after belongs in SI — not the inject turn (paraphrase trap).
    assert "not muted" not in d.lower()
    assert "answer naturally" not in d.lower()
    assert "SIMULATOR CUE" in d


@pytest.mark.asyncio
async def test_nudge_freestyle_answer_sends_audio_stream_end_not_text() -> None:
    """Non-text freestyle kick: activity_end only while agent stream is open."""
    from google.genai import types

    from livekit_agent_simulator.gemini.live_session import GeminiCallerBridge

    bridge = object.__new__(GeminiCallerBridge)
    bridge._inject_turn_active = False
    bridge._script_hangup_farewell = False
    bridge._silent_mode = False
    bridge._mute_persona_audio = False
    bridge._agent_audio_paused = False
    bridge._agent_stream_open = False
    bridge._agent_speech_frames = 0
    bridge._agent_silence_ms = 0.0
    bridge._persona_output_suppressed = lambda: False  # type: ignore[attr-defined]
    calls: list[dict] = []

    class _Writer:
        def emit(self, *a, **k):  # noqa: ANN002, ANN003
            return None

    class _Sess:
        async def send_realtime_input(self, **kwargs):  # noqa: ANN003
            calls.append(dict(kwargs))

    bridge.writer = _Writer()  # type: ignore[attr-defined]
    bridge._live_session = _Sess()
    # Closed stream: no-op (redundant ends caused Live 1006 on 018).
    await GeminiCallerBridge.nudge_freestyle_answer(bridge, "Which car?")
    assert calls == []

    bridge._agent_stream_open = True
    await GeminiCallerBridge.nudge_freestyle_answer(bridge, "Still there?")
    assert len(calls) == 1
    assert "activity_end" in calls[0]
    assert isinstance(calls[0]["activity_end"], types.ActivityEnd)
    assert "text" not in calls[0]
    assert bridge._agent_stream_open is False


def test_pcm16_mono_rms_silence_and_tone() -> None:
    from livekit_agent_simulator.gemini.live_session import pcm16_mono_rms

    silence = b"\x00\x00" * 160
    assert pcm16_mono_rms(silence) == 0.0
    # ~full-scale alternating samples → high RMS
    tone = array.array("h", [10000, -10000] * 80).tobytes()
    assert pcm16_mono_rms(tone) > 1000.0


def test_looks_like_assistant_persona_detects_staff_cues() -> None:
    from livekit_agent_simulator.gemini.live_session import looks_like_assistant_persona

    assert looks_like_assistant_persona(
        "Hi, thanks for calling Vehicle Wholesales. I'd be happy to check that for you."
    )
    assert looks_like_assistant_persona(
        "Let me check on that for you right now. Yes, it looks available."
    )
    assert not looks_like_assistant_persona(
        "Yeah, hi. I was just wondering — have you checked inventory?"
    )
    assert not looks_like_assistant_persona(
        "I was looking at the 2022 Mazda CX-5 actually."
    )


def test_script_speak_directive_hangup_stays_quiet_after() -> None:
    from livekit_agent_simulator.gemini.live_session import script_speak_directive

    d = script_speak_directive("Thanks, bye.", hangup_farewell=True)
    assert "Thanks, bye." in d
    assert "wait silently" in d.lower()
    assert "<<<" not in d


@pytest.mark.asyncio
async def test_speak_awaits_agent_idle_before_inject() -> None:
    bridge = _FakeBridge()
    observer = MagicMock()
    observer.agent_is_active_speaker = True
    observer.agent_has_spoken = True
    observer.agent_active_duration_ms.return_value = 0
    writer = MagicMock()
    step = ScriptStep(
        id="line",
        trigger="silence",
        delay_ms=0,
        say="What is the price?",
        action="speak",
    )
    runner = ScriptRunner([step], observer, bridge, writer)  # type: ignore[arg-type]

    async def _clear_speaker() -> None:
        await asyncio.sleep(0.08)
        observer.agent_is_active_speaker = False

    clearer = asyncio.create_task(_clear_speaker())
    await runner._fire(step, waited_ms=0)
    await clearer
    assert bridge.injected == ["What is the price?"]


@pytest.mark.asyncio
async def test_after_speak_waits_for_agent_final_before_next_step() -> None:
    bridge = _FakeBridge()
    observer = MagicMock()
    observer.agent_is_active_speaker = False
    observer.agent_has_spoken = True
    observer.agent_active_duration_ms.return_value = 0
    observer.last_agent_final_mono = None
    writer = MagicMock()
    steps = [
        ScriptStep(
            id="open",
            trigger="silence",
            delay_ms=0,
            say="Hi, is this available?",
            action="speak",
        ),
        ScriptStep(
            id="pace",
            trigger="silence",
            delay_ms=0,
            action="wait",
            silence_after_cue_ms=30,
            mute_persona=False,
        ),
    ]
    runner = ScriptRunner(steps, observer, bridge, writer)  # type: ignore[arg-type]
    runner._post_speak_settle_ms = 50
    runner._post_speak_reply_budget_s = 2.0

    async def _drive() -> None:
        await asyncio.sleep(0.05)
        observer.last_agent_final_mono = time.monotonic()
        await asyncio.sleep(0.12)

    driver = asyncio.create_task(_drive())
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.35)
    runner.stop()
    await asyncio.wait_for(task, timeout=2.0)
    await driver
    assert bridge.injected == ["Hi, is this available?"]
    assert bridge.silence_calls == [30]

@pytest.mark.asyncio
async def test_pace_hold_nudges_on_unanswered_open_question() -> None:
    bridge = _FakeBridge()
    observer = MagicMock()
    observer.agent_is_active_speaker = False
    observer.agent_has_spoken = True
    observer.last_agent_final_text = "What does your schedule look like this week?"
    observer.last_agent_final_mono = time.monotonic() - 2.5
    observer.last_user_final_mono = time.monotonic() - 10.0
    writer = MagicMock()
    runner = ScriptRunner([], observer, bridge, writer)  # type: ignore[arg-type]
    await runner._pace_hold(600, allow_freestyle_nudge=True)
    assert len(bridge.nudges) == 1
    assert "schedule" in bridge.nudges[0].lower()


@pytest.mark.asyncio
async def test_pace_hold_skips_nudge_when_muted() -> None:
    bridge = _FakeBridge()
    observer = MagicMock()
    observer.agent_is_active_speaker = False
    observer.last_agent_final_text = "Are you still there?"
    observer.last_agent_final_mono = time.monotonic() - 3.0
    observer.last_user_final_mono = None
    writer = MagicMock()
    runner = ScriptRunner([], observer, bridge, writer)  # type: ignore[arg-type]
    await runner._pace_hold(400, allow_freestyle_nudge=False)
    assert bridge.nudges == []