"""The `do:` opener generator must be able to run at a fixed temperature.

Measured on the target repo (2026-09-30): the same scenario, persona and
pinned target produced `INVALID:SEMANTIC_ACT_MISMATCH` on some runs and `VALID`
on others. `RuleBasedSemanticVerifier` is a pure rule with no sampling, so it
cannot flip on identical input — the input was varying. The utterance came from
`OpenAITextBackend` at a fixed `temperature=0.4`, and the driver's retry loop
re-invokes that same generator, so a retry resamples rather than re-reasons.
That is why a mismatch sometimes cleared and sometimes did not.

These tests drive `_build_text_backend` — the real seam — rather than asserting
on the dataclass, because the failure this fixes was a value that was parsed,
validated, snapshotted, and never applied.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from livekit_agent_simulator.config import ConfigError, load_config


def _write_config(root: Path, simulator_extra: str = "") -> Path:
    (root / ".agent-sim").mkdir(parents=True, exist_ok=True)
    (root / ".agent-sim" / "config.yaml").write_text(
        "livekit:\n"
        "  url: wss://example.livekit.cloud\n"
        "  api_key: lk-test\n"
        "  api_secret: secret\n"
        "  agent_name: test-agent\n"
        "simulator:\n"
        "  api_key: sk-test\n" + simulator_extra,
        encoding="utf-8",
    )
    return root


def test_the_default_is_zero(tmp_path: Path) -> None:
    """Not merely configurable — the default must be the comparable one.

    A knob nobody sets reproduces the bug it was added to fix.
    """
    cfg = load_config(_write_config(tmp_path / "p"))
    assert cfg.simulator.text_temperature == 0.0


def test_it_is_configurable(tmp_path: Path) -> None:
    cfg = load_config(
        _write_config(tmp_path / "p", "  text_temperature: 0.4\n")
    )
    assert cfg.simulator.text_temperature == 0.4


@pytest.mark.parametrize("bad", [2.5, -0.1, "hot", None, True])
def test_bad_values_are_a_load_error_not_a_run_error(
    tmp_path: Path, bad: object
) -> None:
    """A provider-side rejection would surface as an opaque 400 at run time.

    By then the run has usually already been paid for, so the range is checked
    where the rest of the config is.

    The `match` is load-bearing. An earlier version of this test asserted only
    that *some* `ConfigError` was raised — and it passed, because the fixture
    was missing `livekit.agent_name` and that was the error being caught. Five
    green tests that proved nothing about temperature.
    """
    with pytest.raises(ConfigError, match="text_temperature"):
        load_config(_write_config(tmp_path / "p", f"  text_temperature: {bad!r}\n"))


def test_the_wiring_actually_applies_it() -> None:
    """The seam. A parsed-but-unused knob is the exact bug class here."""
    from livekit_agent_simulator.caller_contract.live_wiring import (
        _build_text_backend,
    )

    for temperature in (0.0, 0.4, 1.7):
        backend = _build_text_backend(
            SimpleNamespace(
                simulator=SimpleNamespace(
                    provider="openai",
                    api_key="sk-test",
                    text_temperature=temperature,
                )
            )
        )
        assert backend._temperature == pytest.approx(temperature), (
            "text_temperature was parsed and validated but never reached the "
            "backend — the same shape as the router shipping un-attached"
        )


def test_a_config_without_the_key_still_reaches_the_backend() -> None:
    """Older `SimConfig` doubles carry only some fields.

    `getattr` with a default keeps those working rather than raising
    AttributeError on a scenario that was previously fine.
    """
    from livekit_agent_simulator.caller_contract.live_wiring import (
        _build_text_backend,
    )

    backend = _build_text_backend(
        SimpleNamespace(simulator=SimpleNamespace(provider="openai", api_key="k"))
    )
    assert backend._temperature == pytest.approx(0.0)


def test_it_is_not_the_text_planner() -> None:
    """Two backends, two paths. Conflating them cost a peer session an experiment.

    `text_planner` paraphrases an already-authored catalog line; the
    `text_temperature` key governs the `do:` opener generator. Turning the
    planner off does not make generation deterministic.
    """
    import inspect

    from livekit_agent_simulator.caller_contract import live_wiring

    planner_src = inspect.getsource(live_wiring._attach_response_router)
    builder_src = inspect.getsource(live_wiring._build_text_backend)
    assert "text_temperature" not in planner_src, (
        "the planner backend must not pick up the generator's temperature — "
        "they are separate calls on separate paths"
    )
    assert "text_temperature" in builder_src
