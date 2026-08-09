from livekit_agent_simulator.script.farewell import default_hangup_farewell


def test_default_hangup_farewell_en():
    assert "Bye" in default_hangup_farewell("en-US")
    assert default_hangup_farewell(None) == default_hangup_farewell("en-US")


def test_default_hangup_farewell_vi():
    text = default_hangup_farewell("vi-VN")
    assert "Tạm biệt" in text or "tạm biệt" in text.lower()


def test_default_hangup_never_empty():
    assert default_hangup_farewell("").strip()
    assert default_hangup_farewell("zz-ZZ").strip()
