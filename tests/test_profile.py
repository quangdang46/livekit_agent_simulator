"""Tests for `simulator.profiles` + `--profile` caller-profile selection."""

import pytest

from livekit_agent_simulator.config import ConfigError, config_snapshot, load_config

BASE = """
project: demo
livekit:
  url: "wss://demo.livekit.cloud"
  api_key: "APIkey"
  api_secret: "secret"
  agent_name: "my-agent-local"
simulator:
  provider: google
  api_key: "AIzaDefault"
  language: "en-US"
  voice:
    model: "gemini-3.1-flash-live-preview"
    voice: "Puck"
"""

WITH_PROFILES = BASE + """
  profiles:
    profile-1:
      provider: google
      api_key: "AIzaProfile1"
    profile-2:
      provider: openai
      api_key: "sk-openai-2"
      voice:
        model: "gpt-realtime-2.1-mini"
        voice: "marin"
    profile-3:
      provider: openai
      api_key: "sk-openai-3"
      language: "ja-JP"
"""


def _write(tmp_path, text):
    dot = tmp_path / ".agent-sim"
    dot.mkdir()
    (dot / "config.yaml").write_text(text, encoding="utf-8")
    return tmp_path


def test_flat_block_without_profile_is_backward_compatible(tmp_path):
    """No --profile + no profiles map → flat block, active_profile None."""
    cfg = load_config(_write(tmp_path, BASE))
    assert cfg.simulator.provider == "google"
    assert cfg.simulator.api_key == "AIzaDefault"
    assert cfg.simulator.name == "default"
    assert cfg.active_profile is None
    assert config_snapshot(cfg)["simulator"]["active_profile"] is None


def test_profiles_present_but_no_profile_uses_flat_block(tmp_path):
    """profiles: map present but no --profile → flat block (unchanged behavior)."""
    cfg = load_config(_write(tmp_path, WITH_PROFILES))
    assert cfg.simulator.provider == "google"
    assert cfg.simulator.api_key == "AIzaDefault"
    assert cfg.active_profile is None


def test_select_profile_overrides_provider_and_key(tmp_path):
    cfg = load_config(_write(tmp_path, WITH_PROFILES), profile="profile-2")
    assert cfg.simulator.provider == "openai"
    assert cfg.simulator.api_key == "sk-openai-2"
    assert cfg.simulator.voice.model == "gpt-realtime-2.1-mini"
    assert cfg.simulator.voice.voice == "marin"
    assert cfg.simulator.name == "profile-2"
    assert cfg.active_profile == "profile-2"
    snap = config_snapshot(cfg)["simulator"]
    assert snap["provider"] == "openai"
    assert snap["active_profile"] == "profile-2"
    assert "sk-openai-2" not in str(config_snapshot(cfg))


def test_profile_inherits_unspecified_fields(tmp_path):
    """profile-1 only sets provider+key → inherits voice/language/mode from flat block."""
    cfg = load_config(_write(tmp_path, WITH_PROFILES), profile="profile-1")
    assert cfg.simulator.provider == "google"
    assert cfg.simulator.api_key == "AIzaProfile1"
    assert cfg.simulator.voice.model == "gemini-3.1-flash-live-preview"  # inherited
    assert cfg.simulator.voice.voice == "Puck"  # inherited
    assert cfg.simulator.language == "en-US"  # inherited
    assert cfg.simulator.mode == "realtime"  # inherited


def test_profile_own_language_wins_over_inherited(tmp_path):
    cfg = load_config(_write(tmp_path, WITH_PROFILES), profile="profile-3")
    assert cfg.simulator.provider == "openai"
    assert cfg.simulator.language == "ja-JP"  # profile's own
    assert cfg.simulator.voice.language == "ja-JP"  # authoritative for speech


def test_missing_profile_errors_with_available_list(tmp_path):
    with pytest.raises(ConfigError, match="profile-9") as ei:
        load_config(_write(tmp_path, WITH_PROFILES), profile="profile-9")
    msg = str(ei.value)
    assert "not found" in msg  # no silent fallback to default
    assert "Available profiles" in msg
    assert "profile-1" in msg
    assert "profile-2" in msg


def test_profile_requested_but_no_profiles_map_errors(tmp_path):
    with pytest.raises(ConfigError, match="simulator.profiles"):
        load_config(_write(tmp_path, BASE), profile="profile-2")


def test_profile_invalid_provider_fails_fast(tmp_path):
    bad = BASE + """
  profiles:
    bad:
      provider: anthropic
      api_key: "x"
"""
    with pytest.raises(ConfigError, match="simulator.provider"):
        load_config(_write(tmp_path, bad), profile="bad")


def test_snapshot_never_leaks_profile_key(tmp_path):
    cfg = load_config(_write(tmp_path, WITH_PROFILES), profile="profile-2")
    text = str(config_snapshot(cfg))
    assert "sk-openai-2" not in text
    assert "AIzaProfile1" not in text


# --- default: true marker -------------------------------------------------

DEFAULT_PROFILES = BASE + """
  profiles:
    gemini:
      default: true
      provider: google
      api_key: "AIzaGeminiDefault"
    openai:
      provider: openai
      api_key: "sk-openai-nondefault"
"""


def test_default_profile_used_without_flag(tmp_path):
    """No --profile + one `default: true` profile → that profile wins."""
    cfg = load_config(_write(tmp_path, DEFAULT_PROFILES))
    assert cfg.active_profile == "gemini"
    assert cfg.simulator.provider == "google"
    assert cfg.simulator.api_key == "AIzaGeminiDefault"
    assert config_snapshot(cfg)["simulator"]["active_profile"] == "gemini"


def test_explicit_profile_overrides_default(tmp_path):
    """--profile openai wins even though gemini is marked default."""
    cfg = load_config(_write(tmp_path, DEFAULT_PROFILES), profile="openai")
    assert cfg.active_profile == "openai"
    assert cfg.simulator.provider == "openai"
    assert cfg.simulator.api_key == "sk-openai-nondefault"


def test_default_marker_stripped_from_config(tmp_path):
    """`default` is a marker, never a simulator field."""
    cfg = load_config(_write(tmp_path, DEFAULT_PROFILES))
    assert cfg.simulator.name == "gemini"
    assert not hasattr(cfg.simulator, "default")
    # voice/language/mode inherited from flat block
    assert cfg.simulator.mode == "realtime"
    assert cfg.simulator.voice.model == "gemini-3.1-flash-live-preview"


def test_multiple_defaults_error(tmp_path):
    """2+ `default: true` profiles → ConfigError (no first-wins)."""
    bad = BASE + """
  profiles:
    a:
      default: true
      provider: google
      api_key: "AIzaA"
    b:
      default: true
      provider: openai
      api_key: "sk-B"
"""
    with pytest.raises(ConfigError, match="Multiple profiles marked `default: true`"):
        load_config(_write(tmp_path, bad))


def test_zero_defaults_falls_back_to_flat(tmp_path):
    """No --profile + no `default: true` → legacy flat block (backward compat)."""
    cfg = load_config(_write(tmp_path, WITH_PROFILES))
    assert cfg.active_profile is None
    assert cfg.simulator.provider == "google"
    assert cfg.simulator.api_key == "AIzaDefault"


def test_default_false_is_not_default(tmp_path):
    """Explicit `default: false` must not select the profile."""
    cfg_text = BASE + """
  profiles:
    google:
      default: false
      provider: google
      api_key: "AIzaNotDefault"
    openai:
      default: true
      provider: openai
      api_key: "sk-OpenAI"
"""
    cfg = load_config(_write(tmp_path, cfg_text))
    assert cfg.active_profile == "openai"
    assert cfg.simulator.provider == "openai"
    assert cfg.simulator.api_key == "sk-OpenAI"


def test_nested_voice_partial_override_inherits(tmp_path):
    """Profile overrides only voice.voice → voice.model inherited from flat."""
    flat = BASE.replace(
        '    voice: "Puck"',
        '    voice: "Kore"',
    )
    cfg_text = flat + """
  profiles:
    gemini:
      provider: google
      api_key: "AIzaGemini"
      voice:
        voice: "marin"   # only voice overridden
"""
    cfg = load_config(_write(tmp_path, cfg_text), profile="gemini")
    assert cfg.simulator.voice.voice == "marin"  # profile
    assert cfg.simulator.voice.model == "gemini-3.1-flash-live-preview"  # inherited


def test_profiles_only_with_default_selected(tmp_path):
    """Only profiles, one has default:true → that profile runs (no flat)."""
    cfg_text = """
project: demo
livekit:
  url: "wss://demo.livekit.cloud"
  api_key: "APIkey"
  api_secret: "secret"
  agent_name: "agent"
simulator:
  profiles:
    google:
      default: true
      provider: google
      api_key: "AIzaGoogle"
    openai:
      provider: openai
      api_key: "sk-OpenAI"
"""
    cfg = load_config(_write(tmp_path, cfg_text))
    assert cfg.active_profile == "google"
    assert cfg.simulator.provider == "google"
    assert cfg.simulator.api_key == "AIzaGoogle"


def test_profiles_only_no_default_no_flat_errors(tmp_path):
    """Only profiles, no default, no flat api_key → clear error (no silent fallback)."""
    cfg_text = """
project: demo
livekit:
  url: "wss://demo.livekit.cloud"
  api_key: "APIkey"
  api_secret: "secret"
  agent_name: "agent"
simulator:
  profiles:
    google:
      provider: google
      api_key: "AIzaGoogle"
    openai:
      provider: openai
      api_key: "sk-OpenAI"
"""
    with pytest.raises(ConfigError, match="No default profile configured") as ei:
        load_config(_write(tmp_path, cfg_text))
    assert "simulator.api_key" in str(ei.value)


def test_credential_inheritance_allowed(tmp_path):
    """Profile without api_key inherits flat api_key."""
    cfg_text = BASE.replace("  api_key: \"AIzaDefault\"", "  api_key: \"AIzaInherited\"") + """
  profiles:
    google:
      provider: google       # no api_key → inherit
"""
    cfg = load_config(_write(tmp_path, cfg_text), profile="google")
    assert cfg.simulator.api_key == "AIzaInherited"


def test_invalid_profile_name_with_slash_errors(tmp_path):
    """A profile name with a slash is simply not found → clear error."""
    with pytest.raises(ConfigError, match="foo/bar"):
        load_config(_write(tmp_path, WITH_PROFILES), profile="foo/bar")


def test_profile_lookup_is_case_sensitive(tmp_path):
    """Profile names are case-sensitive — 'OPENAI' != 'profile-2'."""
    with pytest.raises(ConfigError, match="OPENAI"):
        load_config(_write(tmp_path, WITH_PROFILES), profile="OPENAI")
