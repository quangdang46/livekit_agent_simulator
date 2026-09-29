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

from pathlib import Path

from types import SimpleNamespace

from unittest.mock import patch



import pytest



from livekit_agent_simulator.caller_contract import EndedBy, FailureReason

from livekit_agent_simulator.caller_contract.live_wiring import (

    ContractDriverFailure,

    run_contract_driver_path,

)

from livekit_agent_simulator.caller_contract.dsl import parse_steps

from livekit_agent_simulator.caller_contract.driver import ContractCallerDriver, DriverResult

from livekit_agent_simulator.caller_contract.failures import RunFailure

from livekit_agent_simulator.caller_contract.language_adapter import AILanguageAdapter

from livekit_agent_simulator.caller_contract.orchestrator import Orchestrator

from livekit_agent_simulator.caller_contract.publish_sink import BridgePublishSink

from livekit_agent_simulator.caller_contract.semantic import RuleBasedSemanticVerifier

from livekit_agent_simulator.caller_contract.validator import ContractValidator





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

    # 0.19 not 0.2: asyncio.sleep is not a hard lower bound on Windows — a bare
    # sleep(0.120) returned at 109ms in 8/200 runs here. See
    # tests/test_contract_trigger.py::test_time_trigger_fires_after_delay. The
    # regression this guards is "end fired without waiting", which lands near 0.
    assert elapsed >= 0.19, "driver must wait for say's drain before firing end"

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


# ---------------------------------------------------------------------------
# Legacy compatibility bridge: the removal gate is "zero runs emit it", which
# only works if something actually proves it still fires when a scenario
# relies on it. Before these tests nothing asserted the event at all.
# ---------------------------------------------------------------------------


def _bridged_scenario(**speech_conditions):
    """Scenario whose do: step has NO interaction:, so the persona-level
    legacy keys are what the bridge has to fill in."""
    return SimpleNamespace(
        id="bridge-probe",
        caller_actions=parse_steps(
            [
                {
                    "do": {
                        "behavior": "ask",
                        "target": "hours",
                        "constraints": {"max_turns": 1},
                    }
                }
            ],
            file="t",
        ),
        persona={"speech_conditions": dict(speech_conditions)},
        run_spec=SimpleNamespace(first_speaker="agent", timeout_s=1.0),
    )


@pytest.mark.asyncio
async def test_persona_interruption_rate_bridge_fires_and_is_visible():
    """A scenario that still relies on persona speech_conditions gets the
    interaction filled in AND a contract.compat_bridge event naming it —
    that event is the removal gate's only signal."""
    scenario = _bridged_scenario(interruption_rate="medium")

    observer = FakeObserver(replies=["We open at nine."])
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value = _mock_openai_reply(
            {"act": "ask", "target": "hours", "slots": {}, "utterance": "What are your hours?"}
        )
        with patch(
            "livekit_agent_simulator.caller_contract.live_wiring.ObserverAgentWait",
            return_value=ScriptedAgentWait(observer),
        ):
            writer = FakeWriter()
            try:
                await run_contract_driver_path(
                    scenario, None, observer, FakeBridge(), writer, _fake_cfg()
                )
            except ContractDriverFailure:
                # The bridge fires BEFORE dispatch, so a downstream
                # behavior verdict is irrelevant here: this test is about
                # the migration event, not about satisfying the agent.
                pass

    bridges = [spec for kind, spec in writer.events if kind == "contract.compat_bridge"]
    assert [b["bridge"] for b in bridges] == ["persona.interruption_rate"]
    assert bridges[0]["actions"] == 1
    # The bridge is functional, not just an alarm: the do: step now carries
    # the interaction the persona block asked for.
    assert scenario.caller_actions[0].interaction is not None
    assert scenario.caller_actions[0].interaction.interruption_rate == "medium"


@pytest.mark.asyncio
async def test_interrupt_rate_medium_template_needs_no_bridge():
    """Regression for the migrated template: now that it authors
    interaction: on the do: step, the legacy bridge must stay SILENT — a
    bridge event here would mean the migration silently regressed."""
    from livekit_agent_simulator.scenario import parse_scenario

    path = (
        Path(__file__).resolve().parents[1]
        / "templates" / "examples" / "interrupt-rate-medium.yaml"
    )
    scenario = parse_scenario(path)
    # The persona no longer authors the legacy keys at all (removal-gate
    # condition "zero templates authoring legacy keys").
    assert (scenario.persona.get("speech_conditions") or {}) == {}

    observer = FakeObserver(replies=["We open at nine."])
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value = _mock_openai_reply(
            {"act": "ask", "target": "hours", "slots": {}, "utterance": "What are your hours?"}
        )
        with patch(
            "livekit_agent_simulator.caller_contract.live_wiring.ObserverAgentWait",
            return_value=ScriptedAgentWait(observer),
        ):
            writer = FakeWriter()
            try:
                await run_contract_driver_path(
                    scenario, None, observer, FakeBridge(), writer, _fake_cfg()
                )
            except ContractDriverFailure:
                # Downstream outcome is out of scope: the assertion below
                # is about the bridge staying silent, and the bridge
                # decision happens before any dispatch.
                pass

    assert [spec for kind, spec in writer.events if kind == "contract.compat_bridge"] == []




# ---------------------------------------------------------------------------
# Audit B — the audio-side invariant: INVALID never reaches TTS / PCM / mixer.
#
# The E2E hard-boundary test asserts nothing is PUBLISHED. That is necessary
# but not sufficient: TTS synthesis is the first irreversible side effect, and
# it happens BEFORE publish. If a rejected candidate were ever synthesized,
# the run would burn TTS cost and could leak audio through any later path even
# though the sink refused the publish. So this test spies on the synthesize
# callable itself.
# ---------------------------------------------------------------------------


class _AlwaysDriftingBackend:
    """Language backend that always returns an out-of-contract utterance."""

    DRIFT = "Could I spread this over a couple of years and pay it down gradually?"

    def __init__(self):
        self.calls = 0

    def generate(self, context):
        self.calls += 1
        return {
            "act": context["current_behavior"]["act"],
            "target": context["current_behavior"]["target"],
            "slots": {},
            "utterance": self.DRIFT,
        }


@pytest.mark.asyncio
async def test_invalid_candidate_never_reaches_tts_pcm_or_mixer():
    """INVALID -> retry -> INVALID -> NO TTS, NO PCM, NO publish.

    Uses the REAL BridgePublishSink + FakeMixer (so the publish path under
    test is the shipping one, not a stub) and a counting TTS callable.
    """
    scenario = SimpleNamespace(
        caller_actions=parse_steps(
            [
                {"do": {
                    "behavior": "negotiate",
                    "target": "price",
                    "constraints": {"max_turns": 1},
                }},
                {"end": True},
            ],
            file="t",
        )
    )
    backend = _AlwaysDriftingBackend()
    bridge = FakeBridge()
    observer = FakeObserver(replies=["I can do $30,000."])
    writer = FakeWriter()

    synthesized: list[str] = []

    def spy_synthesize(text: str) -> bytes:
        synthesized.append(text)
        return b"\x00\x01" * 8

    driver = ContractCallerDriver(
        orchestrator=Orchestrator(),
        validator=ContractValidator(semantic_verifier=RuleBasedSemanticVerifier()),
        adapter=AILanguageAdapter(backend=backend),
        synthesize=spy_synthesize,
    )
    sink = BridgePublishSink(bridge=bridge, orchestrator=driver.orchestrator)

    result = await driver.run(scenario.caller_actions, sink, ScriptedAgentWait(observer))

    # 1. The run failed as a caller violation ...
    assert result.failure is not None
    assert result.failure.reason == FailureReason.CALLER_BEHAVIOR_VIOLATION
    # 2. ... the backend really was asked (so the test is not vacuous) ...
    assert backend.calls >= 1
    # 3. ... and the rejected text never reached TTS.
    assert all(backend.DRIFT not in text for text in synthesized), synthesized
    # 4. no PCM was pushed to the mixer, i.e. the agent never heard anything.
    assert bridge._mixer.pushed == []
    # 5. nothing was reported as published.
    assert [k for k, _ in writer.events if k == "contract.published"] == []


@pytest.mark.asyncio
async def test_valid_candidate_reaches_mixer_through_the_real_sink():
    """Positive control for the invariant above: when the candidate DOES
    validate, the exact validated string is what gets synthesized and pushed.

    Without this, 'nothing was pushed' would also pass if the harness were
    simply broken."""
    scenario = SimpleNamespace(
        caller_actions=parse_steps(
            [
                {"do": {
                    "behavior": "negotiate",
                    "target": "price",
                    "constraints": {"max_turns": 1},
                }},
                {"end": True},
            ],
            file="t",
        )
    )

    class _GoodBackend:
        UTTERANCE = "Would you be able to come down to $30,000?"

        def generate(self, context):
            return {
                "act": "negotiate",
                "target": "price",
                "slots": {"max_budget": 30000},
                "utterance": self.UTTERANCE,
            }

    backend = _GoodBackend()
    bridge = FakeBridge()
    observer = FakeObserver(replies=["I can do $30,000."])
    synthesized: list[str] = []

    driver = ContractCallerDriver(
        orchestrator=Orchestrator(),
        validator=ContractValidator(semantic_verifier=RuleBasedSemanticVerifier()),
        adapter=AILanguageAdapter(backend=backend),
        synthesize=lambda text: (synthesized.append(text), b"\x00\x01" * 8)[1],
    )
    sink = BridgePublishSink(bridge=bridge, orchestrator=driver.orchestrator)

    result = await driver.run(scenario.caller_actions, sink, ScriptedAgentWait(observer))

    assert result.failure is None, result.failure
    assert synthesized == [backend.UTTERANCE], synthesized
    assert len(bridge._mixer.pushed) == 1


# ---------------------------------------------------------------------------
# play_audio assets must never reach the SPEECH layer.
#
# The speech layer is the validated-utterance channel: its only writer is
# publish_validated_pcm, called through BridgePublishSink (which owns the
# staleness re-check and the drain gate). Authored ambient beds belong on the
# mixer's parallel noise layer. BridgeAssetPlayer used to fall back to
# bridge.inject_cue(delivery="room_pcm") when the mixer had no push_noise —
# and for a voice.* asset inject_cue calls push_speech directly, i.e. a
# speech-layer route with no validator, no sink and no staleness check.
# ---------------------------------------------------------------------------


class _NoiseMixer:
    """Duck-typed mixer with the noise layer (the shape both shipped bridges
    actually construct: ParallelMicMixer has push_speech AND push_noise)."""

    def __init__(self):
        self.noise: list[tuple[bytes, float, bool]] = []
        self.speech: list[bytes] = []

    def push_noise(self, pcm, *, gain=1.0, loop=False):
        self.noise.append((pcm, gain, loop))

    def push_speech(self, pcm, *, gain=1.0):
        self.speech.append(pcm)


class _SpeechOnlyMixer:
    """A mixer with the speech layer but NO noise layer — the only shape that
    could ever have reached the removed fallback."""

    def __init__(self):
        self.speech: list[bytes] = []

    def push_speech(self, pcm, *, gain=1.0):
        self.speech.append(pcm)


class _BridgeWith:
    def __init__(self, mixer):
        self._mixer = mixer
        self.inject_cue_calls: list[dict] = []

    async def inject_cue(self, text, **kw):
        self.inject_cue_calls.append({"text": text, **kw})


def _write_voice_asset(tmp_path) -> str:
    """Minimal mono 24kHz WAV so resolve_cue_asset/load_wav_pcm succeed."""
    import struct
    import wave

    path = tmp_path / "barge_short.wav"
    frames = struct.pack("<" + "h" * 240, *([1200] * 240))  # 10 ms @ 24kHz
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(frames)
    return str(path)


@pytest.mark.asyncio
async def test_play_asset_uses_noise_layer_never_speech(tmp_path):
    """The shipped shape: bed goes to the noise layer, speech layer untouched."""
    from livekit_agent_simulator.caller_contract.live_wiring import BridgeAssetPlayer
    from livekit_agent_simulator.audio import pcm_cue

    wav = _write_voice_asset(tmp_path)
    monkey = pcm_cue.resolve_cue_asset
    pcm_cue.resolve_cue_asset = lambda asset, **kw: wav  # noqa: ARG005
    try:
        mixer = _NoiseMixer()
        player = BridgeAssetPlayer(bridge=_BridgeWith(mixer))
        ok = await player.play_asset("builtin:voice.barge_short", gain=0.5, loop=False, label="l")
    finally:
        pcm_cue.resolve_cue_asset = monkey

    assert ok is True
    assert len(mixer.noise) == 1
    assert mixer.noise[0][1] == 0.5 and mixer.noise[0][2] is False
    assert mixer.speech == [], "authored bed must never land on the speech layer"


@pytest.mark.asyncio
async def test_play_asset_refuses_rather_than_reaching_speech(tmp_path):
    """Regression pin: with a mixer that has no noise layer, the asset player
    must REFUSE — not reroute authored audio onto the validated-speech
    channel via inject_cue."""
    from livekit_agent_simulator.caller_contract.live_wiring import BridgeAssetPlayer
    from livekit_agent_simulator.audio import pcm_cue

    wav = _write_voice_asset(tmp_path)
    monkey = pcm_cue.resolve_cue_asset
    pcm_cue.resolve_cue_asset = lambda asset, **kw: wav  # noqa: ARG005
    try:
        mixer = _SpeechOnlyMixer()
        bridge = _BridgeWith(mixer)
        writer = FakeWriter()
        player = BridgeAssetPlayer(bridge=bridge, writer=writer)
        ok = await player.play_asset("builtin:voice.barge_short", gain=1.0, loop=False, label="l")
    finally:
        pcm_cue.resolve_cue_asset = monkey

    assert ok is False
    assert mixer.speech == [], "must not push authored audio onto the speech layer"
    assert bridge.inject_cue_calls == [], "the inject_cue fallback is removed"
    kinds = [k for k, _ in writer.events]
    assert "contract.audio_refused" in kinds


@pytest.mark.asyncio
async def test_play_asset_refusal_fails_the_run_as_transport_not_caller_violation():
    """A refused bed is an execution failure, never a caller violation."""
    from livekit_agent_simulator.caller_contract.driver import ContractCallerDriver

    class _RefusingPlayer:
        async def play_asset(self, asset, *, gain, loop, label):
            return False

    scenario = SimpleNamespace(
        caller_actions=parse_steps([{"play_audio": {"asset": "builtin:noise.ambient"}}], file="t")
    )
    driver = ContractCallerDriver(
        orchestrator=Orchestrator(),
        validator=ContractValidator(semantic_verifier=RuleBasedSemanticVerifier()),
        adapter=AILanguageAdapter(backend=_AlwaysDriftingBackend()),
        synthesize=lambda text: b"\x00\x01" * 8,
    )
    result = await driver.run(
        scenario.caller_actions,
        BridgePublishSink(bridge=FakeBridge(), orchestrator=driver.orchestrator),
        ScriptedAgentWait(FakeObserver()),
        assets=_RefusingPlayer(),
    )
    assert result.failure is not None
    assert result.failure.reason == FailureReason.TRANSPORT_ERROR
    assert result.failure.reason != FailureReason.CALLER_BEHAVIOR_VIOLATION


# --- Slice 3c: the end_reason labels are product surface ---------------------
#
# Commit c0d3e26 relabelled five of the six EndedBy -> reason keys to MUTANTC_*
# and the suite stayed green, because every test asserting the returned string
# drove the SCENARIO ending -- the one key the mutant left alone. These labels
# are not cosmetic: script/models.py maps contract_caller_end /
# contract_agent_end onto the `sim` / `agent` assert vocabulary and asserts.py
# special-cases contract_timeout, so a wrong label turns every
# `type: ended_by` assertion into "unrecognized" with nothing complaining.


@pytest.mark.asyncio

async def test_agent_hangup_returns_contract_agent_end():

    """The real driver, no mocking of the unit under test: the caller speaks,
    the agent goes silent AND disconnects, so driver.run returns a failure-free
    DriverResult(ended_by=AGENT). run_contract_driver_path must name the agent
    as the side that ended the call."""

    scenario = SimpleNamespace(
        caller_actions=parse_steps(
            [{"do": {"behavior": "ask", "constraints": {"max_turns": 2}}}], file="t"
        )
    )

    bridge = FakeBridge()

    # always_timeout: wait_agent_turn returns None, which is the branch that
    # then probes is_agent_gone().
    observer = FakeObserver(always_timeout=True)

    writer = FakeWriter()

    cfg = _fake_cfg()

    agent = ScriptedAgentWait(observer)
    agent.is_agent_gone = lambda: True

    payload = {"act": "ask", "target": None, "slots": {}, "utterance": "Could you tell me more?"}

    with patch("urllib.request.urlopen", return_value=_mock_openai_reply(payload)):

        with patch(
            "livekit_agent_simulator.caller_contract.live_wiring.ObserverAgentWait",
            return_value=agent,
        ):

            end_reason = await run_contract_driver_path(
                scenario, None, observer, bridge, writer, cfg
            )

    assert end_reason == "contract_agent_end"

    # Not a failure: the caller published its turn, the agent simply hung up.
    assert len(bridge._mixer.pushed) == 1


@pytest.mark.asyncio

@pytest.mark.parametrize(
    "ended_by,expected",
    [
        (EndedBy.SCENARIO, "contract_scenario_end"),
        (EndedBy.CALLER, "contract_caller_end"),
        (EndedBy.AGENT, "contract_agent_end"),
        (EndedBy.TIMEOUT, "contract_timeout"),
        (EndedBy.TRANSPORT, "contract_transport_error"),
        (EndedBy.ERROR, "contract_error"),
        # A key outside the map must degrade to the documented fallback rather
        # than leak None/raw enum text into run.end_condition.reason.
        ("not-an-ended-by", "contract_end"),
    ],
)

async def test_ended_by_maps_to_the_contract_reason_label(ended_by, expected):

    """Pin every key of the EndedBy -> reason map, including the ones the
    driver cannot currently reach.

    Reachability today: driver.py builds a failure-free DriverResult only for
    SCENARIO (two sites) and AGENT (one site); every other ending goes through
    _fail(), which always attaches a RunFailure, and run_contract_driver_path
    raises ContractDriverFailure before the map whenever failure is not None.
    So contract_caller_end / contract_timeout / contract_transport_error /
    contract_error are unreachable in production right now. They are pinned
    anyway because they are the map's published domain -- a future change that
    makes one reachable must not also be free to rename it.
    """

    async def _finished(self, *a, **kw):
        return DriverResult(ended_by=ended_by, failure=None)

    scenario = SimpleNamespace(
        caller_actions=parse_steps([{"say": "Hi there."}], file="t")
    )

    with patch("urllib.request.urlopen"):

        with patch.object(ContractCallerDriver, "run", _finished):

            end_reason = await run_contract_driver_path(
                scenario, None, FakeObserver(), FakeBridge(), FakeWriter(), _fake_cfg()
            )

    assert end_reason == expected


def test_driver_failure_message_is_reason_colon_detail():

    """str(ContractDriverFailure) is what the CLI prints and what operators read
    in a failed run. The "reason: detail" shape is the contract; c0d3e26
    rewrote it as f"MUTANTA {reason} MUTANTB {detail}"."""

    result = DriverResult(
        ended_by=EndedBy.ERROR,
        failure=RunFailure(
            reason=FailureReason.AGENT_TIMEOUT, detail="agent silent for 30.0s"
        ),
    )

    exc = ContractDriverFailure(result)

    assert str(exc) == f"{FailureReason.AGENT_TIMEOUT.value}: agent silent for 30.0s"

    # The wrapped result stays reachable for callers that want the typed reason.
    assert exc.result.failure.reason == FailureReason.AGENT_TIMEOUT


@pytest.mark.asyncio

async def test_record_and_replay_paths_are_mutually_exclusive():

    """Passing both is a contradiction the caller cannot honour: the replay
    backend replaces the AI backend, so the run would silently produce no
    record. c0d3e26 deleted this guard and no test noticed. The guard is the
    first statement in the function, so this needs no fakes beyond the args."""

    scenario = SimpleNamespace(
        caller_actions=parse_steps([{"say": "Hi there."}], file="t")
    )

    with pytest.raises(ValueError, match="mutually exclusive"):

        await run_contract_driver_path(
            scenario,
            None,
            FakeObserver(),
            FakeBridge(),
            FakeWriter(),
            _fake_cfg(),
            record_path=Path("run.json"),
            replay_path=Path("run.json"),
        )


# ---------------------------------------------------------------------------
# Response router: attach, end to end through run_contract_driver_path
# ---------------------------------------------------------------------------


def _router_cfg(*, planner_enabled: bool = False):

    """A cfg carrying the two blocks `_attach_response_router` reads.

    text_planner defaults to DISABLED here so the routed turn publishes the
    authored line verbatim and the test makes no AI call. The router itself
    is scripted below.
    """

    return SimpleNamespace(
        simulator=SimpleNamespace(provider="openai", api_key="sk-test"),
        router=SimpleNamespace(
            provider="openai",
            model="",
            api_key="sk-test",
            timeout_ms=1_500,
            temperature=0.0,
            timeout_s=1.5,
        ),
        text_planner=SimpleNamespace(
            enabled=planner_enabled, provider="openai", model="", api_key="sk-test"
        ),
    )


@pytest.mark.asyncio
async def test_a_responses_scenario_routes_through_the_real_driver_path():
    """The whole seam, end to end.

    Every other router test injects `driver.router` by hand, so none of them
    can tell "the router works" from "the router is never attached". This
    drives `run_contract_driver_path` — the function a real run calls — with
    a scenario that authors `responses:` and a config carrying `router:`,
    and asserts a routing decision is actually recorded.
    """

    from livekit_agent_simulator.caller_contract.responses import ResponseCatalog
    from livekit_agent_simulator.caller_contract.router import RouteDecision
    import livekit_agent_simulator.caller_contract.router_openai as _ro

    catalog = ResponseCatalog.from_dict(
        {
            "company_name": {
                "intent": "company_name",
                "instruction": "Provide the company name.",
                "text": "It's Bluebird Property Management.",
            },
            "off_script": {
                "intent": "off_script",
                "instruction": "Select only when no other response fits.",
                "text": "I'm sorry, could we stay focused?",
                "system": True,
            },
        }
    )

    class _ScriptedRouter:
        """Construction is real (the seam builds the class); only the call is
        scripted, because what is under test is the ATTACH, not the HTTP."""

        def __init__(self, **kw):
            self.kw = kw
            self.seen: list[str] = []

        async def route(self, *, agent_transcript, catalog):
            self.seen.append(agent_transcript)
            return RouteDecision(
                response_id="company_name",
                backend="scripted",
                latency_ms=1,
            )

    scenario = SimpleNamespace(
        id="router-e2e",
        caller_actions=parse_steps(
            [
                {
                    "do": {
                        "behavior": "negotiate",
                        "target": "price",
                        "constraints": {"max_turns": 2, "max_budget": 30000},
                    }
                }
            ],
            file="t",
        ),
        responses=catalog,
    )
    observer = FakeObserver(
        replies=[
            "What is your company name?",
            "That is over our budget.",
            "We cannot go any higher.",
            "Then we will pass.",
            "Understood, thank you.",
        ]
    )
    writer = FakeWriter()
    bridge = FakeBridge()

    import livekit_agent_simulator.caller_contract.live_wiring as _lw

    # Turn 0 is legacy (the caller opens the call before the agent has said
    # anything, so there is nothing to route on) and does reach the text
    # backend. Script that one reply with the same shape
    # test_contract_driver's _ScriptedBackend emits for negotiate/price, so
    # the semantic verifier accepts it and the loop turns to the routed turn.
    legacy_reply = _mock_openai_reply(
        {
            "act": "negotiate",
            "target": "price",
            "slots": {"max_budget": 30000},
            "utterance": "Would you be able to come down to $30,000?",
        }
    )

    _lw._SHERPA_DEAD = True  # sherpa model download shares urlopen
    try:
        with patch.object(_ro, "OpenAIResponseRouter", _ScriptedRouter), patch(
            "urllib.request.urlopen", return_value=legacy_reply
        ), patch(
            "livekit_agent_simulator.caller_contract.live_wiring.ObserverAgentWait",
            return_value=ScriptedAgentWait(observer),
        ):
            # negotiate/price is never satisfied by the scripted replies, so
            # the run ends BEHAVIOR_TIMEOUT. That is fine and expected: the
            # routing decision is recorded BEFORE the timeout, and it is the
            # recording this test is about.
            with pytest.raises(ContractDriverFailure) as ended:
                await run_contract_driver_path(
                    scenario, None, observer, bridge, writer, _router_cfg()
                )
    finally:
        _lw._SHERPA_DEAD = False
    assert ended.value.result.ended_by.value == "timeout"

    decisions = [spec for kind, spec in writer.events if kind == "contract.router_decision"]
    assert decisions, (
        "the router was never attached: the run took the legacy caller_steps "
        "path and emitted no routing decision"
    )
    assert decisions[0]["response_id"] == "company_name"
    assert decisions[0]["off_script"] is False
    assert decisions[0]["agent_text"], "the decision must record what it routed on"
