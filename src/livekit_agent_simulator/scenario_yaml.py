"""YAML scenario transport — serialize/deserialize the section-object shape.

A YAML scenario is ONE top-level mapping whose keys map 1:1 onto the
:class:`Scenario` fields :func:`scenario_from_dict` accepts:

    apiVersion: agent-sim/v1
    kind: Scenario
    metadata: {id: constraint-no-card, locale: en-US, tags: [smoke]}
    persona:
      name: Sam
      brief: |
        You called support about a delayed order...
    execute: {max_turns: 5, timeout_s: 120, first_speaker: agent}
    assert:
      outcomes:
        - id: no_card_leak
          type: constraint_respected
          ...
    pass_criteria:
      criteria: [ ... ]

The parser supports two extra conveniences:
  - A LiveKit-style ``name:`` + ``scenarios:`` group wrapper that holds exactly
    one scenario (LKS keeps one scenario per file).
  - Multiple YAML documents (``---``) merged into one scenario.

Validation is NOT duplicated here: the loaded dict is passed straight to
:func:`scenario_from_dict`, so the JSONL rule set stays the single source of
truth. YAML text that should not be coerced (``on``/``no``/leading zeros) must
be quoted by the author (PyYAML 1.x behavior).
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from .scenario import Scenario, ScenarioError
from .scenario_from_dict import scenario_from_dict


def _as_plain(data: Any) -> Any:
    """Recursively convert dataclasses/datetime to YAML-safe primitives."""
    if is_dataclass(data) and not isinstance(data, type):
        return _as_plain(asdict(data))
    if isinstance(data, dict):
        return {k: _as_plain(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_as_plain(v) for v in data]
    if hasattr(data, "isoformat"):
        return data.isoformat()
    return data


def _clean(data: Any) -> Any:
    """Drop None / empty collection values so serialized YAML stays readable.

    Semantic round-trip is unaffected: the parsers apply the same defaults
    when a key is absent.
    """
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for k, v in data.items():
            cleaned = _clean(v)
            if cleaned is None:
                continue
            if isinstance(cleaned, (dict, list)) and not cleaned:
                continue
            out[k] = cleaned
        return out
    if isinstance(data, list):
        cleaned_list = [_clean(v) for v in data]
        return [c for c in cleaned_list if c is not None]
    return data


class _OrderedDumper(yaml.SafeDumper):
    """SafeDumper that preserves insertion order of dict keys."""


def _dict_representer(dumper: yaml.Dumper, data: dict) -> Any:
    return dumper.represent_mapping("tag:yaml.org,2002:map", data.items(), flow_style=False)


_OrderedDumper.add_representer(dict, _dict_representer)


def _dump_plain(data: Any) -> str:
    return yaml.dump(
        _clean(_as_plain(data)),
        Dumper=_OrderedDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )


def dump_scenario_dict(data: dict[str, Any]) -> str:
    """Serialize a section-object scenario dict to YAML text (ordered keys)."""
    return _dump_plain(data)


def scenario_to_yaml_text(scenario: Scenario) -> str:
    """Serialize a Scenario to the section-object YAML shape.

    Key order follows the canonical JSONL section order: Scenario header,
    Persona, Context, Simulator/Execute, Dispatch, Caller, Telephony,
    Behavior, Script, Assert, PassCriteria. Script steps keep their authored
    field order (id/trigger/delay/say/…), which keeps diffs meaningful.
    """
    data: dict[str, Any] = {
        "apiVersion": "agent-sim/v1",
        "kind": "Scenario",
        "metadata": {
            "id": scenario.id,
            "locale": scenario.effective_locale(),
            "tags": list(scenario.tags),
        },
        "persona": dict(scenario.persona or {}),
    }
    if scenario.context:
        data["context"] = dict(scenario.context)
    if scenario.execute is not None:
        data["execute"] = {
            "max_turns": scenario.execute.max_turns,
            "timeout_s": scenario.execute.timeout_s,
            "first_speaker": scenario.execute.first_speaker,
            "hold_music_timeout_s": scenario.execute.hold_music_timeout_s,
        }
    elif scenario.simulator.max_turns != 6 or scenario.simulator.timeout_s != 120 or scenario.simulator.first_speaker != "agent":
        data["simulator"] = {
            "max_turns": scenario.simulator.max_turns,
            "timeout_s": scenario.simulator.timeout_s,
            "first_speaker": scenario.simulator.first_speaker,
        }
    if scenario.dispatch and scenario.dispatch.metadata:
        data["dispatch"] = {"metadata": scenario.dispatch.metadata}
    if scenario.caller is not None:
        data["caller"] = {"mode": scenario.caller.mode}
    if scenario.telephony is not None:
        data["telephony"] = _as_plain(scenario.telephony)
    if scenario.behavior_spec:
        data["behavior"] = dict(scenario.behavior_spec)
    if scenario.script_steps:
        script: dict[str, Any] = {
            "steps": [_as_plain(s) for s in scenario.script_steps],
        }
        if scenario.script_verify is not None:
            script["verify"] = {
                "require_during_agent_speech": scenario.script_verify.require_during_agent_speech,
                "min_agent_finals_after_first_cue": scenario.script_verify.min_agent_finals_after_first_cue,
                "min_user_finals_after_first_cue": scenario.script_verify.min_user_finals_after_first_cue,
                "min_interruptions": scenario.script_verify.min_interruptions,
                "max_interruptions": scenario.script_verify.max_interruptions,
                "min_agent_finals_after_silence": scenario.script_verify.min_agent_finals_after_silence,
                "min_agent_finals_after_barge_in": scenario.script_verify.min_agent_finals_after_barge_in,
                "plugins": list(scenario.script_verify.plugins),
                "plugin_options": dict(scenario.script_verify.plugin_options),
            }
        data["script"] = script
    if scenario.asserts is not None and not scenario.asserts.empty:
        data["assert"] = _as_plain(scenario.asserts)
    if scenario.pass_criteria or scenario.pass_judges:
        pc: dict[str, Any] = {"criteria": list(scenario.pass_criteria)}
        if scenario.pass_judges:
            pc["mode"] = scenario.pass_criteria_mode
            pc["judges"] = _as_plain(scenario.pass_judges)
        data["pass_criteria"] = pc

    return dump_scenario_dict(data)


def load_scenario_yaml(path: Path | str) -> Scenario:
    """Parse one YAML scenario file → Scenario (raises ScenarioError)."""
    path = Path(path)
    if not path.exists():
        raise ScenarioError(f"Scenario file not found: {path}")

    try:
        documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except yaml.YAMLError as e:
        raise ScenarioError(f"{path}: invalid YAML — {e}") from e

    # Drop empty docs (a trailing `---` produces None).
    sections = [d for d in documents if isinstance(d, dict) and d]
    if not sections:
        raise ScenarioError(f"{path}: empty scenario file")

    if "scenarios" in sections[0]:
        # LiveKit-style group wrapper: {name, scenarios: [ {label, ...} ]}.
        group = sections[0]
        raw_items = group.get("scenarios")
        if not isinstance(raw_items, list) or not raw_items:
            raise ScenarioError(f"{path}: scenarios must be a non-empty list")
        if len(raw_items) != 1:
            raise ScenarioError(
                f"{path}: group wrapper with {len(raw_items)} scenarios — "
                f"LKS uses one scenario per file; split into separate files"
            )
        merged = dict(raw_items[0])
        # Carry group-level metadata onto the single scenario.
        for key in ("metadata", "tags", "locale", "id"):
            if key not in merged and key in group:
                merged[key] = group[key]
        sections = [merged]

    # Merge multiple documents: later docs overwrite scalar keys, lists append.
    data: dict[str, Any] = {}
    for sec in sections:
        for k, v in sec.items():
            if k in data and isinstance(data[k], list) and isinstance(v, list):
                data[k] = list(data[k]) + list(v)
            else:
                data[k] = v

    return scenario_from_dict(data, path=path, path_label=str(path))
