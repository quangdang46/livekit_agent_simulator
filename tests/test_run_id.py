import re
from pathlib import Path

from livekit_agent_simulator.run_orchestrator import (
    allocate_run_dir,
    new_run_id,
    next_run_seq,
)

_RUN_ID = re.compile(
    r"^(?P<seq>\d{3})-(?P<slug>[a-z0-9_-]+)-(?P<ymd>\d{8})-(?P<hms>\d{6})-(?P<hex>[0-9a-f]{4})$"
)


def test_new_run_id_seq_prefix_plus_scenario() -> None:
    rid = new_run_id("sp-vad-rt-barge-early", seq=1)
    m = _RUN_ID.match(rid)
    assert m is not None
    assert m.group("seq") == "001"
    assert m.group("slug") == "sp-vad-rt-barge-early"


def test_new_run_id_sanitizes_weird_ids() -> None:
    rid = new_run_id("My Case!!/../x", seq=7)
    m = _RUN_ID.match(rid)
    assert m is not None
    assert m.group("seq") == "007"
    assert m.group("slug") == "my-case-x"


def test_new_run_id_name_override() -> None:
    rid = new_run_id("sp-vad-rt-barge-early", name="demo", seq=1)
    m = _RUN_ID.match(rid)
    assert m is not None
    assert m.group("seq") == "001"
    assert m.group("slug") == "demo"
    assert "sp-vad-rt-barge-early" not in rid


def test_new_run_id_sanitizes_name_override() -> None:
    rid = new_run_id("smoke-hello", name="Run #1!!", seq=3)
    m = _RUN_ID.match(rid)
    assert m is not None
    assert m.group("seq") == "003"
    assert m.group("slug") == "run-1"


def test_new_run_id_unique_across_calls() -> None:
    a = new_run_id("smoke-hello", seq=1)
    b = new_run_id("smoke-hello", seq=1)
    assert a != b


def test_next_run_seq_from_reports_dir(tmp_path: Path) -> None:
    (tmp_path / "001-smoke-hello-20260716-000000-aaaa").mkdir()
    (tmp_path / "003-demo-20260716-000001-bbbb").mkdir()
    (tmp_path / "legacy-no-seq-folder").mkdir()
    (tmp_path / "suite-20260716.json").write_text("{}", encoding="utf-8")
    assert next_run_seq(tmp_path) == 4
    assert next_run_seq(tmp_path / "missing") == 1


def test_allocate_run_dir_increments_on_collision(tmp_path: Path) -> None:
    first_id, first_dir = allocate_run_dir(tmp_path, "smoke-hello")
    assert _RUN_ID.match(first_id)
    assert first_id.startswith("001-smoke-hello-")
    assert first_dir.is_dir()

    second_id, second_dir = allocate_run_dir(tmp_path, "smoke-hello")
    assert _RUN_ID.match(second_id)
    assert second_id.startswith("002-smoke-hello-")
    assert second_dir.is_dir()

    named_id, _ = allocate_run_dir(tmp_path, "smoke-hello", name="demo")
    assert _RUN_ID.match(named_id)
    assert named_id.startswith("003-demo-")
