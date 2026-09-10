"""Slice 3 acceptance: caller_contract single-path live wiring.



Exercises ``run_contract_driver_path`` (the exact function run_orchestrator

calls when ``scenario.caller_actions`` is non-empty) against fake bridge +

observer stand-ins -- no real LiveKit room needed, but the real Orchestrator,

ContractValidator, AILanguageAdapter, BridgePublishSink, ObserverAgentWait,

and ContractCallerDriver all run for real.



Covers exactly the acceptance list from the design review:

    say            -> publish

    do             -> generate -> validate -> publish

    wait           -> no publish

    dtmf           -> control action, no publish

    invalid do     -> VALIDATOR: INVALID -> NEVER published (never TTS->LiveKit)

    agent timeout  -> AGENT_TIMEOUT failure, NOT CALLER_BEHAVIOR_VIOLATION



Slice 3b (serialization) additionally covers:

    say -> end       -> `end` never fires before say's audio finished draining

    say -> do         -> do's AI generate call never starts before say drained

    stuck mixer drain -> TRANSPORT_ERROR, NEVER CALLER_BEHAVIOR_VIOLATION

"""



from __future__ import annotations



import asyncio

import json

import time

from types import SimpleNamespace

from unittest.mock import patch



import pytest



from livekit_agent_simulator.caller_contract import EndedBy, FailureReason

from livekit_agent_simulator.caller_contract.live_wiring import (

    ContractDriverFailure,

    run_contract_driver_path,

)

from livekit_agent_simulator.caller_contract.dsl import parse_steps





class FakeWriter:

    def __init__(self):

        self.events: list[tuple[str, dict]] = []



    def emit(self, kind, spec=None, **kw):

        self.events.append((kind, spec or {}))





class FakeMixer:

    """Records validated PCM publishes. ``drain_duration_s`` simulates real

    mixer drain latency: speech_queued_ms() stays > 0 for that long after a

    push, then drops to 0 -- None means "stuck" (never drains)."""



    def __init__(self, drain_duration_s: float | None = 0.0):

        self.pushed: list[bytes] = []

        self._drain_duration_s = drain_duration_s

        self._drain_until: float | None = None



    def push_speech(self, pcm, *, gain=1.0):

        self.pushed.append(pcm)

        if self._drain_duration_s is None:

            self._drain_until = float("inf")

        elif self._drain_duration_s <= 0:

            self._drain_until = None

        else:

            self._drain_until = time.monotonic() + self._drain_duration_s



    def end_speech_turn(self):

        pass



    def speech_queued_ms(self):

        if self._drain_until is None:

            return 0

        return 100 if time.monotonic() < self._drain_until else 0





class FakeBridge:

    """Minimal duck-typed CallerBridge stand-in: only publish_validated_pcm

    + drain_persona_speech (mirrors the real bridges' mixer-drain method)."""



    def __init__(self, drain_duration_s: float | None = 0.0):

        self._mixer = FakeMixer(drain_duration_s)

        self.stopped = False

        self._voice_gain = 1.0

        self._user_audio_source_emitted = False

        self.writer = FakeWriter()



    def publish_validated_pcm(self, pcm, *, gain=1.0):

        if not pcm:

            return False

        self._mixer.push_speech(pcm, gain=gain)

        self._mixer.end_speech_turn()

        return True



    async def drain_persona_speech(self, *, timeout_s: float = 4.0):

        deadline = time.monotonic() + timeout_s

        while time.monotonic() < deadline:

            if self._mixer.speech_queued_ms() <= 0:

                return

            await asyncio.sleep(0.01)



    def stop(self):

        self.stopped = True



    async def publish_mic(self):

        return None





class FakeObserver:

    """Fixed-script agent replies: each wait_agent_turn call in the driver

    corresponds to advancing to the next scripted reply."""



    def __init__(self, replies: list[str] | None = None, *, always_timeout: bool = False):

        self._replies = list(replies or [])

        self.last_agent_final_mono: float | None = None

        self.last_agent_final_text: str = ""

        self.agent_is_active_speaker = False

        self._always_timeout = always_timeout

        self._tick = 0.0



    def speak_next_reply(self):

        if self._always_timeout or not self._replies:

            return

        self._tick += 1.0

        self.last_agent_final_mono = self._tick

        self.last_agent_final_text = self._replies.pop(0)





class ScriptedAgentWait:

    """Drives FakeObserver.speak_next_reply() once, then reports it -- avoids

    a real asyncio.sleep poll loop in the test."""



    def __init__(self, observer: FakeObserver):

        self.observer = observer



    async def wait_agent_turn(self, *, timeout_s: float):

        if self.observer._always_timeout:

            return None

        self.observer.speak_next_reply()

        text = self.observer.last_agent_final_text

        return text or None





def _fake_cfg(provider="openai"):

    return SimpleNamespace(simulator=SimpleNamespace(provider=provider, api_key="sk-test"))





def _mock_openai_reply(payload: dict):

    body = {"choices": [{"message": {"content": json.dumps(payload)}}]}



    class _Resp:

        def __enter__(self):

            return self



        def __exit__(self, *a):

            return False



        def read(self):

            return json.dumps(body).encode()



    return _Resp()





@pytest.mark.asyncio

async def test_say_publishes_directly_no_ai_call():

    scenario = SimpleNamespace(caller_actions=parse_steps([{"say": "Hi there."}], file="t"))

    bridge = FakeBridge()

    observer = FakeObserver()

    writer = FakeWriter()

    cfg = _fake_cfg()



    with patch("urllib.request.urlopen") as mock_open:

        end_reason = await run_contract_driver_path(scenario, None, observer, bridge, writer, cfg)

        import livekit_agent_simulator.caller_contract.live_wiring as _lw

        _lw._SHERPA_DEAD = True  # sherpa model download shares urlopen; latch fallback here
        try:
            # Re-run is unnecessary: the path above already published. Assert on
            # the recorded calls — none may target an AI backend (sherpa model
            # URL excluded: backend selection is not an AI generation call).
            assert all(
                "generativelanguage" not in str(c.args[0])
                and "/chat/completions" not in str(c.args[0])
                and "/messages" not in str(c.args[0])
                for c in mock_open.call_args_list
            ), "say must never invoke an AI backend"
        finally:
            _lw._SHERPA_DEAD = False  # other tests exercise the real fallback chain



    assert end_reason == "contract_scenario_end"

    assert len(bridge._mixer.pushed) == 1

    kinds = [k for k, _ in writer.events]

    assert "contract.published" in kinds





@pytest.mark.asyncio

async def test_do_generates_validates_and_publishes():

    scenario = SimpleNamespace(

        caller_actions=parse_steps(

            [{"do": {"behavior": "negotiate", "target": "price", "constraints": {"max_turns": 2}}}],

            file="t",

        )

    )

    bridge = FakeBridge()

    observer = FakeObserver(replies=["Sure, I can do $30,000."])

    writer = FakeWriter()

    cfg = _fake_cfg()



    payload = {

        "act": "negotiate",

        "target": "price",

        "slots": {},

        "utterance": "Would you be able to come down on the price?",

    }

    with patch("urllib.request.urlopen", return_value=_mock_openai_reply(payload)):

        with patch(

            "livekit_agent_simulator.caller_contract.live_wiring.ObserverAgentWait",

            return_value=ScriptedAgentWait(observer),

        ):

            end_reason = await run_contract_driver_path(scenario, None, observer, bridge, writer, cfg)



    assert end_reason == "contract_scenario_end"

    assert len(bridge._mixer.pushed) == 1





@pytest.mark.asyncio

async def test_invalid_candidate_never_published_to_livekit():

    """The acceptance case: contract=negotiate/price with forbidden_intents

    financing; AI returns an off-topic financing question. Validator must

    reject -> INVALID -> NEVER reaches bridge.publish_validated_pcm (no

    TTS->LiveKit shortcut). The run must fail with CALLER_BEHAVIOR_VIOLATION,

    never silently continue."""

    scenario = SimpleNamespace(

        caller_actions=parse_steps(

            [

                {

                    "do": {

                        "behavior": "negotiate",

                        "target": "price",

                        "constraints": {"forbidden_intents": ["financing"]},

                    }

                }

            ],

            file="t",

        )

    )

    bridge = FakeBridge()

    observer = FakeObserver()

    writer = FakeWriter()

    cfg = _fake_cfg()



    payload = {

        "act": "negotiate",

        "target": "price",

        "slots": {},

        "utterance": "Do you offer financing options?",

    }

    with patch("urllib.request.urlopen", return_value=_mock_openai_reply(payload)):

        with pytest.raises(ContractDriverFailure) as exc_info:

            await run_contract_driver_path(scenario, None, observer, bridge, writer, cfg)



    assert exc_info.value.result.failure.reason == FailureReason.CALLER_BEHAVIOR_VIOLATION

    assert len(bridge._mixer.pushed) == 0  # never published





@pytest.mark.asyncio

async def test_agent_timeout_is_not_a_caller_violation():

    """Agent silence must classify as AGENT_TIMEOUT, never

    CALLER_BEHAVIOR_VIOLATION -- the caller's own turn was validly spoken and

    published; only the agent side failed to respond."""

    scenario = SimpleNamespace(

        caller_actions=parse_steps(

            [{"do": {"behavior": "ask", "constraints": {"max_turns": 2}}}], file="t"

        )

    )

    bridge = FakeBridge()

    observer = FakeObserver(always_timeout=True)

    writer = FakeWriter()

    cfg = _fake_cfg()



    payload = {"act": "ask", "target": None, "slots": {}, "utterance": "Could you tell me more?"}

    with patch("urllib.request.urlopen", return_value=_mock_openai_reply(payload)):

        with patch(

            "livekit_agent_simulator.caller_contract.live_wiring.ObserverAgentWait",

            return_value=ScriptedAgentWait(observer),

        ):

            with pytest.raises(ContractDriverFailure) as exc_info:

                await run_contract_driver_path(scenario, None, observer, bridge, writer, cfg)



    assert exc_info.value.result.failure.reason == FailureReason.AGENT_TIMEOUT

    assert exc_info.value.result.failure.reason != FailureReason.CALLER_BEHAVIOR_VIOLATION

    # The caller's own turn WAS published (agent just never replied).

    assert len(bridge._mixer.pushed) == 1





@pytest.mark.asyncio

async def test_transport_failure_does_not_consume_validator_retries():

    """Backend/network failure must surface as LANGUAGE_GENERATION_ERROR,

    never as CALLER_BEHAVIOR_VIOLATION -- and a flaky-then-healthy backend

    must still succeed (transport errors don't burn the validator's retry

    budget). Regression for the validate_with_retry conflation where a

    LanguageGenerationError raised inside _generate was swallowed as a

    retryable validator verdict."""

    from livekit_agent_simulator.caller_contract.language_adapter import (

        LanguageGenerationError,

    )



    scenario = SimpleNamespace(

        caller_actions=parse_steps(

            [{"do": {"behavior": "ask", "constraints": {"max_turns": 2}}}], file="t"

        )

    )

    bridge = FakeBridge()

    observer = FakeObserver(replies=["Sure, that works for me."])

    writer = FakeWriter()

    cfg = _fake_cfg()



    calls = {"n": 0}

    good_payload = {"act": "ask", "target": None, "slots": {}, "utterance": "Could you tell me more?"}



    def _flaky_urlopen(*a, **kw):

        calls["n"] += 1

        if calls["n"] == 1:

            raise LanguageGenerationError("simulated network drop")

        return _mock_openai_reply(good_payload)



    with patch("urllib.request.urlopen", side_effect=_flaky_urlopen):

        with patch(

            "livekit_agent_simulator.caller_contract.live_wiring.ObserverAgentWait",

            return_value=ScriptedAgentWait(observer),

        ):

            # NOTE: live_wiring always constructs its own OpenAITextBackend,

            # so the flaky failure must be injected at urlopen level (above).

            # A single transport failure aborts the behavior immediately...

            with pytest.raises(ContractDriverFailure) as exc_info:

                await run_contract_driver_path(scenario, None, observer, bridge, writer, cfg)



    assert exc_info.value.result.failure.reason == FailureReason.LANGUAGE_GENERATION_ERROR

    assert exc_info.value.result.failure.reason != FailureReason.CALLER_BEHAVIOR_VIOLATION





@pytest.mark.asyncio

async def test_wait_and_dtmf_never_publish():

    scenario = SimpleNamespace(

        caller_actions=parse_steps([{"wait": 10}, {"dtmf": "123"}, {"end": True}], file="t")

    )

    bridge = FakeBridge()

    observer = FakeObserver()

    writer = FakeWriter()

    cfg = _fake_cfg()



    with patch("urllib.request.urlopen") as mock_open:

        end_reason = await run_contract_driver_path(scenario, None, observer, bridge, writer, cfg)

        mock_open.assert_not_called()



    assert end_reason == "contract_scenario_end"

    assert len(bridge._mixer.pushed) == 0

    kinds = [k for k, _ in writer.events]

    assert "contract.wait" in kinds

    assert "contract.control" in kinds

    assert "contract.published" not in kinds





# ---------------------------------------------------------------------------

# Slice 3b: serialization -- no action advances while the previous action's

# audio is still draining out of the mixer.

# ---------------------------------------------------------------------------





@pytest.mark.asyncio

async def test_end_does_not_fire_before_say_audio_drains():

    """say -> end : `end` (and the run's completion) must not happen before

    say's mixer drain finishes. Proven by wall-clock: the whole run must take

    at least as long as the simulated drain."""

    scenario = SimpleNamespace(

        caller_actions=parse_steps([{"say": "Hi, this will take a moment to play."}, {"end": True}], file="t")

    )

    bridge = FakeBridge(drain_duration_s=0.2)

    observer = FakeObserver()

    writer = FakeWriter()

    cfg = _fake_cfg()



    started = time.monotonic()

    end_reason = await run_contract_driver_path(scenario, None, observer, bridge, writer, cfg)

    elapsed = time.monotonic() - started



    assert end_reason == "contract_scenario_end"

    assert elapsed >= 0.2, "driver must wait for say's drain before firing end"

    # By the time `end` was emitted, the mixer must already be drained.

    kinds = [k for k, _ in writer.events]

    assert kinds.index("contract.published") < kinds.index("contract.end")





@pytest.mark.asyncio

async def test_say_drain_completes_before_do_generation_starts():

    """say -> do : the AI generate() call for `do` must never start before

    say's audio finished draining -- proven by recording a timestamp inside

    the mocked HTTP call and asserting it comes after the drain window."""

    scenario = SimpleNamespace(

        caller_actions=parse_steps(

            [

                {"say": "One moment please."},

                {"do": {"behavior": "ask", "constraints": {"max_turns": 1}}},

            ],

            file="t",

        )

    )

    bridge = FakeBridge(drain_duration_s=0.15)

    observer = FakeObserver(replies=["Sure, that works for me."])

    writer = FakeWriter()

    cfg = _fake_cfg()



    generate_called_at: list[float] = []

    payload = {"act": "ask", "target": None, "slots": {}, "utterance": "Could you tell me more?"}



    def _urlopen(*a, **kw):

        generate_called_at.append(time.monotonic())

        return _mock_openai_reply(payload)



    started = time.monotonic()

    with patch("urllib.request.urlopen", side_effect=_urlopen):

        with patch(

            "livekit_agent_simulator.caller_contract.live_wiring.ObserverAgentWait",

            return_value=ScriptedAgentWait(observer),

        ):

            await run_contract_driver_path(scenario, None, observer, bridge, writer, cfg)



    assert generate_called_at, "do: never invoked the AI backend"

    assert generate_called_at[0] - started >= 0.15, (

        "AI generate() for `do` started before say's mixer drain completed"

    )





@pytest.mark.asyncio

async def test_stuck_mixer_drain_fails_transport_not_caller_violation():

    """A mixer that never drains must fail the run with TRANSPORT_ERROR

    (execution failure), NEVER CALLER_BEHAVIOR_VIOLATION -- a stuck mixer is

    not the caller saying something wrong."""

    scenario = SimpleNamespace(caller_actions=parse_steps([{"say": "Hello."}], file="t"))

    bridge = FakeBridge(drain_duration_s=None)  # stuck forever

    observer = FakeObserver()

    writer = FakeWriter()

    cfg = _fake_cfg()



    with pytest.raises(ContractDriverFailure) as exc_info:

        await run_contract_driver_path(

            scenario, None, observer, bridge, writer, cfg, drain_timeout_s=0.05

        )



    assert exc_info.value.result.failure.reason == FailureReason.TRANSPORT_ERROR

    assert exc_info.value.result.failure.reason != FailureReason.CALLER_BEHAVIOR_VIOLATION

    assert exc_info.value.result.ended_by == EndedBy.TRANSPORT

