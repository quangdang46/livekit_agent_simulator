from livekit_agent_simulator.run_orchestrator import new_run_id


def test_new_run_id_includes_scenario_and_timestamp() -> None:
    rid = new_run_id("smoke-hello")
    assert rid.startswith("smoke-hello-")
    parts = rid.split("-")
    # smoke-hello-YYYYMMDD-HHMMSS-hex → at least 5 segments when scenario has a hyphen
    assert len(rid) > len("smoke-hello-")
    assert parts[-1]  # hex suffix
    assert len(parts[-1]) == 4
    # date segment YYYYMMDD
    assert any(len(p) == 8 and p.isdigit() for p in parts)


def test_new_run_id_sanitizes_weird_ids() -> None:
    rid = new_run_id("My Case!!/../x")
    assert " " not in rid
    assert "/" not in rid
    assert rid.startswith("my-case")
