from livekit_agent_simulator.ops import guide


def test_guide_returns_markdown() -> None:
    result = guide()
    text = result["text"]
    assert "livekit-agent-simulator" in text
    assert "scenario-init" in text or "init_scenario" in text
    assert "preflight" in text
    assert "execute" in text
    assert "config.yaml" in text
    assert "conversation.wav" in text
    assert "run_id" in text
