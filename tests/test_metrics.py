"""Unit tests for voice metrics aggregates (P1.3)."""

from livekit_agent_simulator.metrics import compute_voice_metrics, metrics_digest


def _ev(kind: str, mono: int, **spec):
    return {"kind": kind, "ts_mono_ms": mono, "spec": spec}


def test_empty_events():
    m = compute_voice_metrics([])
    assert m["schema"] == "agent-sim/metrics/v1"
    assert m["turn_taking_ms"]["count"] == 0
    assert m["ttfw_ms"] is None
    assert m["barge_count"] == 0
    assert m["barge_recovery_rate"] is None
    assert m["talk_ratio"] is None
    assert m["user_words_count"] == 0
    assert m["user_words_p50"] is None
    assert m["user_words_p10"] is None
    assert m["user_words_mean"] is None
    assert m["user_words_natural_count"] == 0
    assert m["user_words_natural_p50"] is None
    assert m["user_words_script_count"] == 0
    assert m["user_words_script_p50"] is None


def test_turn_taking_and_ttfw():
    events = [
        _ev("transcript.user.final", 1000, text="hello there"),
        _ev("transcript.agent.final", 1800, text="Hi, how can I help?", turn_taking_ms=800),
        _ev("transcript.user.final", 3000, text="book me"),
        _ev("transcript.agent.final", 5200, text="Sure.", turn_taking_ms=2200),
    ]
    m = compute_voice_metrics(events)
    assert m["ttfw_ms"] == 1800
    assert m["ttfw_source"] == "transcript.agent.final"
    assert m["turn_taking_ms"]["count"] == 2
    assert m["turn_taking_ms"]["p50"] == 800
    assert m["turn_taking_ms"]["max"] == 2200
    assert m["agent_finals"] == 2
    assert m["user_finals"] == 2
    assert m["talk_ratio"] is not None
    assert 0 < m["talk_ratio"] < 1
    assert m["user_words_count"] == 2
    assert m["user_words_p50"] == 2.0  # "hello there" and "book me" → 2, 2
    assert m["user_words_mean"] == 2.0
    # No Script cues → all finals count as natural
    assert m["user_words_natural_count"] == 2
    assert m["user_words_natural_p50"] == 2.0
    assert m["user_words_script_count"] == 0
    d = metrics_digest(m)
    assert d["ttfw_ms"] == 1800
    assert d["turn_p50_ms"] == 800
    assert d["user_words_p50"] == 2.0
    assert d["user_words_natural_p50"] == 2.0


def test_user_words_dedupe_consecutive_and_percentiles():
    events = [
        _ev("transcript.user.final", 100, text="hi"),
        _ev("transcript.user.final", 110, text="hi"),  # consecutive dup
        _ev("transcript.user.final", 200, text="I need help with my order please"),
        _ev("transcript.user.final", 300, text=""),  # empty excluded
        _ev("transcript.user.final", 400, text="okay thanks a lot for clarifying that"),
    ]
    m = compute_voice_metrics(events)
    # 1, 7, 7 words
    assert m["user_words_count"] == 3
    assert m["user_words_p10"] == 1.0
    assert m["user_words_p50"] == 7.0
    assert m["user_words_mean"] == (1 + 7 + 7) / 3


def test_user_words_natural_excludes_script_say_matches():
    """Freestyle soft metric ignores finals that match Script say lines."""
    script_say = "My name is Mai and I need an appointment on Tuesday please"
    # Distinct content words so speech_origin overlap does not false-positive.
    freestyle = (
        "Um well the delivery never arrived so I am calling "
        "to track the package and confirm the refund timeline"
    )
    events = [
        _ev("sim.script.cue", 500, say=script_say, step_id="open", action="speak"),
        _ev("transcript.user.final", 900, text=script_say),
        _ev("transcript.agent.final", 2000, text="Sure, what day works?"),
        _ev("transcript.user.final", 3500, text=freestyle),
        _ev(
            "transcript.user.final",
            3600,
            text=freestyle,
        ),  # consecutive dup ignored
    ]
    m = compute_voice_metrics(events)
    assert m["user_words_count"] == 2
    assert m["user_words_script_count"] == 1
    assert m["user_words_script_p50"] == float(len(script_say.split()))
    assert m["user_words_natural_count"] == 1
    assert m["user_words_natural_p50"] == float(len(freestyle.split()))
    assert m["user_words_natural_p50"] > m["user_words_script_p50"]
    d = metrics_digest(m)
    assert d["user_words_natural_p50"] == m["user_words_natural_p50"]
    # Overall p50 mixes both — not the freestyle signal
    assert m["user_words_p50"] is not None


def test_user_words_natural_excludes_script_clause_stt_splits():
    """Trailing STT fragment of a multi-clause Script say is not freestyle."""
    say = (
        "I was looking at the 2022 Mazda CX-5 actually. "
        "Is that one still available?"
    )
    events = [
        _ev("sim.script_inject", 1000, text=say, label="name_car"),
        _ev("transcript.user.final", 2500, text="I was looking at the 2022 Mazda CX-5, actually."),
        _ev("transcript.user.final", 4200, text="Is that one still available?"),
        _ev(
            "transcript.user.final",
            8000,
            text="Yeah this is for myself, I'm calling from near the city.",
        ),
    ]
    m = compute_voice_metrics(events)
    assert m["user_words_script_count"] == 2
    assert m["user_words_natural_count"] == 1
    assert m["user_words_natural_p50"] == float(
        len("Yeah this is for myself, I'm calling from near the city.".split())
    )


def test_ttfw_from_preamble():
    events = [
        _ev("transcript.agent.preamble", 400, text="Welcome!"),
        _ev("transcript.user.final", 1000, text="hi"),
        _ev("transcript.agent.final", 1500, text="yes", turn_taking_ms=500),
    ]
    m = compute_voice_metrics(events)
    assert m["ttfw_ms"] == 400
    assert m["ttfw_source"] == "transcript.agent.preamble"


def test_barge_recovery():
    events = [
        _ev("sim.script.cue", 1000, barge_in=True, step_id="cut"),
        _ev("interruption", 1000, by="sim", barge_in=True),
        _ev("transcript.agent.final", 2500, text="Sorry, go on."),
        _ev("sim.script.cue", 4000, barge_in=True),
        # no recovery after second barge
    ]
    m = compute_voice_metrics(events)
    assert m["barge_count"] == 2  # deduped cue+interruption at 1000, plus 4000
    assert m["barges_recovered"] == 1
    assert m["barge_recovery_rate"] == 0.5
    assert m["recovery_ms"]["count"] == 1
    assert m["recovery_ms"]["p50"] == 1500


def test_tool_error_rate_and_slow_turns():
    events = [
        _ev("tool.start", 100, name="a"),
        _ev("tool.start", 200, name="b"),
        _ev("tool.error", 300, name="b"),
        _ev("transcript.agent.final", 400, text="x", turn_taking_ms=3000),
        _ev("transcript.agent.final", 500, text="y", turn_taking_ms=6000),
    ]
    m = compute_voice_metrics(events)
    assert m["tool_calls"] == 2
    assert m["tool_errors"] == 1
    assert m["tool_error_rate"] == 0.5
    assert m["slow_turns_over_2500ms"] == 2
    assert m["slow_turns_over_5000ms"] == 1
