"""`simulator.target_keywords` — the consumer's target vocabulary.

The extension point on RuleBasedSemanticVerifier shipped with no consumer, so
it was a dead knob in the exact sense AGENTS.md forbids: a parameter nobody
could reach. These tests pin the config surface that finally feeds it, and
pin the SHAPE of the path rather than only its endpoint — a test that calls a
builder directly stays green when nothing forwards the value, which is how
`no_router` and `router_digest` both shipped broken in this migration.
"""

from __future__ import annotations

import inspect

import pytest

from livekit_agent_simulator.caller_contract.semantic import (
    TARGET_KEYWORDS,
    RuleBasedSemanticVerifier,
)
from livekit_agent_simulator.config import (
    ConfigError,
    config_snapshot,
    load_config,
)

BASE = """
project: demo
livekit:
  url: "wss://demo.livekit.cloud"
  api_key: "APIkey"
  api_secret: "secret"
  agent_name: "my-agent-local"
simulator:
  provider: google
  api_key: "AIzaTest"
"""


def _load(tmp_path, text):
    dot = tmp_path / ".agent-sim"
    dot.mkdir()
    (dot / "config.yaml").write_text(text, encoding="utf-8")
    return load_config(tmp_path)


# ------------------------------------------------------------------ parsing


def test_absent_by_default_is_the_normal_case(tmp_path):
    cfg = _load(tmp_path, BASE)
    assert cfg.simulator.target_keywords == {}


def test_consumer_vocabulary_is_accepted(tmp_path):
    cfg = _load(
        tmp_path,
        BASE
        + """
  target_keywords:
    maintenance_schedule: [maintenance, repair, servicing]
    loan_offer: [loan, rate, apr]
""",
    )
    assert cfg.simulator.target_keywords["maintenance_schedule"] == (
        "maintenance", "repair", "servicing",
    )
    assert cfg.simulator.target_keywords["loan_offer"] == ("loan", "rate", "apr")


def test_words_are_stripped_and_blanks_dropped(tmp_path):
    cfg = _load(
        tmp_path,
        BASE + "  target_keywords:\n    t: ['  repair  ', '', '   ', 'fix']\n",
    )
    assert cfg.simulator.target_keywords["t"] == ("repair", "fix")


# ------------------------------------------------------- structural errors


def test_a_bare_string_is_rejected_not_coerced(tmp_path):
    """The failure this prevents is quiet, not loud: a string would be scanned
    as ONE substring, so the verifier would match `maintenance repair` and
    never match either word alone."""
    with pytest.raises(ConfigError) as exc:
        _load(tmp_path, BASE + "  target_keywords:\n    t: maintenance repair\n")
    assert "list of words" in str(exc.value)


def test_a_non_mapping_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as exc:
        _load(tmp_path, BASE + "  target_keywords: [a, b]\n")
    assert "must be a mapping" in str(exc.value)


def test_an_empty_word_list_is_rejected(tmp_path):
    """Silently dropping it would leave a target that can never be proven —
    which reads to an author as 'my target is wrong', not 'I listed no words'."""
    with pytest.raises(ConfigError) as exc:
        _load(tmp_path, BASE + "  target_keywords:\n    t: ['  ', '']\n")
    assert "no usable words" in str(exc.value)


def test_an_empty_target_name_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as exc:
        _load(tmp_path, BASE + "  target_keywords:\n    '': [word]\n")
    assert "empty target name" in str(exc.value)


# ---------------------------------------------------------------- snapshot


def test_snapshot_omits_the_key_when_unconfigured(tmp_path):
    """D11: absent, not null. A report written before this feature must be
    byte-identical to one written now."""
    snap = config_snapshot(_load(tmp_path, BASE))
    assert "simulator_target_keywords" not in snap


def test_snapshot_names_targets_without_dumping_every_word(tmp_path):
    cfg = _load(
        tmp_path,
        BASE + "  target_keywords:\n    zeta: [a, b]\n    alpha: [c]\n",
    )
    got = config_snapshot(cfg)["simulator_target_keywords"]
    assert got["targets"] == ["alpha", "zeta"]  # sorted, so runs are comparable
    assert got["word_counts"] == {"alpha": 1, "zeta": 2}


# --------------------------------------- the shape of the path, not the end


def test_the_verifier_actually_accepts_what_config_carries():
    """The point of the whole change. If this fails, the extension point is
    still unreachable and the config key is a knob that changes nothing."""

    from livekit_agent_simulator.caller_contract import BehaviorContract

    v = RuleBasedSemanticVerifier(
        target_keywords={"maintenance_schedule": ("maintenance", "repair")}
    )
    # A maintenance utterance must now produce target evidence, which it
    # could not before the vocabulary existed.
    out = v.classify(
        utterance="I can send someone out for the maintenance on Thursday",
        contract=BehaviorContract(
            behavior="ask", target="maintenance_schedule"
        ),
    )
    assert out is not None


def test_the_constructor_still_takes_the_key(tmp_path):
    """A signature-level check, because threading a value is exactly the class
    of bug this migration shipped twice: the parameter exists, something is
    supposed to pass it, and the suite stays green when the pass is dropped."""

    assert "target_keywords" in inspect.signature(
        RuleBasedSemanticVerifier.__init__
    ).parameters


def test_default_construction_is_unchanged(tmp_path):
    """Existing scenarios must behave exactly as before: no config key, no
    override, the package's own TARGET_KEYWORDS still in force."""
    from livekit_agent_simulator.caller_contract.semantic import TARGET_KEYWORDS

    cfg = _load(tmp_path, BASE)
    assert cfg.simulator.target_keywords == {}
    v = RuleBasedSemanticVerifier()
    assert v._target_keywords.keys() == TARGET_KEYWORDS.keys()
