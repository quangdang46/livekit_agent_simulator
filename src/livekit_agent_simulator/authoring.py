"""Hamming-style authoring quality checks for scenarios (P1.G / #27).

**Rule-based only — no LLM.** Soft by default: never flips ``valid`` false.
``validate_scenario`` merges warning messages into the flat ``warnings`` list and
returns a structured ``authoring`` object (codes, scorecard, tier).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .script.models import counts_for_recovery_barge

STRESS_TRAITS = frozenset(
    {
        "interrupts",
        "impatient",
        "hangup_threat",
        "angry",
        "urgent",
        "backchannel",
        "silent",
        "quiet",
    }
)

RISK_TAGS = frozenset(
    {
        "blocking",
        "scheduled",
        "exploratory",
        "draft",
        "smoke",
        "regression",
    }
)


@dataclass(frozen=True)
class AuthoringWarning:
    """One soft authoring finding (machine-readable for agents/CI)."""

    code: str
    message: str
    severity: str = "warn"  # warn | info

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _persona_goals(persona: dict[str, Any]) -> list[str]:
    goals = persona.get("goals") or []
    if isinstance(goals, str):
        goals = [goals]
    return [str(g).strip() for g in goals if str(g).strip()]


def _persona_traits(persona: dict[str, Any]) -> list[str]:
    traits = persona.get("traits") or persona.get("behaviors") or []
    if isinstance(traits, str):
        traits = [traits]
    out: list[str] = []
    for t in traits:
        key = str(t).strip().lower().replace(" ", "_").replace("-", "_")
        if key:
            out.append(key)
    return out


def _persona_constraints(persona: dict[str, Any]) -> list[str]:
    raw = persona.get("constraints") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(c).strip() for c in raw if str(c).strip()]


def _scenario_tags(scenario: Any) -> list[str]:
    raw_tags = getattr(scenario, "tags", None) or []
    if not isinstance(raw_tags, (list, tuple)):
        return []
    return [str(t).strip().lower() for t in raw_tags if str(t).strip()]


def _has_recovery_proof(scenario: Any) -> bool:
    """True if Assert recovery outcome or script_verify recovery min is set."""
    asserts = getattr(scenario, "asserts", None)
    if asserts is not None:
        for oc in getattr(asserts, "outcomes", None) or []:
            if getattr(oc, "type", None) == "recovery":
                return True
    sv = getattr(scenario, "script_verify", None)
    if sv is not None and int(getattr(sv, "min_agent_finals_after_barge_in", 0) or 0) > 0:
        return True
    return False


def _has_constraint_proof(scenario: Any) -> bool:
    asserts = getattr(scenario, "asserts", None)
    if asserts is None:
        return False
    for oc in getattr(asserts, "outcomes", None) or []:
        if getattr(oc, "type", None) == "constraint_respected":
            return True
    return False


def _has_ended_by_proof(scenario: Any) -> bool:
    asserts = getattr(scenario, "asserts", None)
    if asserts is None:
        return False
    for oc in getattr(asserts, "outcomes", None) or []:
        if getattr(oc, "type", None) == "ended_by":
            return True
    return False


def _recovery_barge_steps(scenario: Any) -> list[Any]:
    steps = list(getattr(scenario, "script_steps", None) or [])
    return [
        s
        for s in steps
        if counts_for_recovery_barge(
            barge_in=bool(getattr(s, "barge_in", False)),
            interrupt_class=getattr(s, "interrupt_class", None),
        )
    ]


def _silent_mode(persona: dict[str, Any]) -> bool:
    sc = persona.get("speech_conditions") or persona.get("speechConditions") or {}
    if not isinstance(sc, dict):
        return False
    raw = sc.get("silent_mode", sc.get("silentMode", sc.get("silent")))
    if raw is True or raw == 1:
        return True
    if isinstance(raw, str) and raw.strip().lower() in ("1", "true", "yes", "on", "silent"):
        return True
    return False


def _first_speaker(scenario: Any) -> str:
    execute = getattr(scenario, "execute", None)
    sim = getattr(scenario, "simulator", None)
    if execute is not None and getattr(execute, "first_speaker", None):
        return str(execute.first_speaker)
    if sim is not None and getattr(sim, "first_speaker", None):
        return str(sim.first_speaker)
    return "agent"


def collect_authoring_findings(scenario: Any) -> list[AuthoringWarning]:
    """Return structured soft authoring findings for a parsed Scenario."""
    findings: list[AuthoringWarning] = []
    persona = getattr(scenario, "persona", None) or {}
    if not isinstance(persona, dict):
        persona = {}

    goals = _persona_goals(persona)
    if not goals:
        findings.append(
            AuthoringWarning(
                code="empty_goals",
                message=(
                    "Persona.goals is empty — Hamming: caller needs a job-to-be-done "
                    "(underspecified personas pass on different agent workflows)."
                ),
            )
        )

    brief = str(persona.get("brief") or "").strip()
    situation = str(persona.get("situation") or "").strip()
    steps = list(getattr(scenario, "script_steps", None) or [])
    if not brief and not situation:
        findings.append(
            AuthoringWarning(
                code="empty_brief",
                message=(
                    "Persona.brief and Persona.situation are empty — add who is calling and why "
                    "(dialogue mode prefers situation + outcome)."
                ),
            )
        )
    elif not situation and not steps:
        findings.append(
            AuthoringWarning(
                code="dialogue_missing_situation",
                severity="info",
                message=(
                    "Dialogue scenario (no Script): consider Persona.situation + Persona.outcome "
                    "so the caller has a world problem and a clear done-state."
                ),
            )
        )

    outcome = str(persona.get("outcome") or persona.get("desired_outcome") or "").strip()
    if situation and not outcome and not steps:
        findings.append(
            AuthoringWarning(
                code="situation_without_outcome",
                severity="info",
                message=(
                    "Persona.situation set without Persona.outcome — add what “done” looks like "
                    "for PassCriteria/Judge."
                ),
            )
        )

    if _first_speaker(scenario) == "agent" and not steps and not _silent_mode(persona):
        findings.append(
            AuthoringWarning(
                code="agent_first_no_script",
                message=(
                    "Dialogue with first_speaker=agent and no Script: if the agent-under-test "
                    "also waits for the caller to speak first, both sides stay silent — "
                    "prefer first_speaker=user, silent_mode, or a Script open cue."
                ),
            )
        )

    tags = _scenario_tags(scenario)
    has_risk = any(t in RISK_TAGS or t.startswith("risk:") for t in tags)
    if not tags:
        findings.append(
            AuthoringWarning(
                code="no_tags",
                severity="info",
                message=(
                    "Scenario has no metadata.tags — add a risk/lifecycle tag "
                    "(smoke, draft, blocking, scheduled, exploratory, regression)."
                ),
            )
        )
    elif not has_risk:
        findings.append(
            AuthoringWarning(
                code="no_risk_tag",
                message=(
                    "Scenario tags have no risk/lifecycle hint "
                    "(prefer one of: smoke, draft, blocking, scheduled, exploratory, regression)."
                ),
            )
        )

    traits = _persona_traits(persona)
    stress = [t for t in traits if t in STRESS_TRAITS]
    has_interaction = bool(steps) or bool(getattr(scenario, "behavior_spec", None))
    sc = persona.get("speech_conditions") or {}
    if isinstance(sc, dict) and (
        sc.get("barge_policy")
        or sc.get("noise")
        or sc.get("ambient")
        or sc.get("silence_ms")
        or sc.get("silent_mode")
        or sc.get("interruption_rate")
    ):
        has_interaction = True
    if stress and not has_interaction and not _silent_mode(persona):
        findings.append(
            AuthoringWarning(
                code="stress_trait_without_interaction",
                message=(
                    f"Traits {stress} imply interaction stress but there is no Script/Behavior/"
                    f"speech_conditions step — CI cannot hard-prove interrupt/silence/hangup "
                    f"(prompt-only traits are soft)."
                ),
            )
        )

    barges = _recovery_barge_steps(scenario)
    if barges and not _has_recovery_proof(scenario):
        ids = ", ".join(getattr(s, "id", "?") for s in barges[:5])
        findings.append(
            AuthoringWarning(
                code="barge_without_recovery",
                message=(
                    f"Recovery barge step(s) present ({ids}) but no Assert outcome type=recovery "
                    f"and script_verify.min_agent_finals_after_barge_in is 0 — "
                    f"add recovery assert so CI proves agent re-engages."
                ),
            )
        )

    hangups = [s for s in steps if getattr(s, "action", None) == "hang_up"]
    if hangups and not _has_ended_by_proof(scenario):
        findings.append(
            AuthoringWarning(
                code="hang_up_without_ended_by",
                message=(
                    "Script hang_up present but no Assert outcome type=ended_by — "
                    "add ended_by to prove which side ended the call."
                ),
            )
        )

    constraints = _persona_constraints(persona)
    if constraints and not _has_constraint_proof(scenario):
        findings.append(
            AuthoringWarning(
                code="constraint_without_assert",
                message=(
                    "Persona.constraints present but no Assert outcome type=constraint_respected — "
                    "prompt-only constraints are soft; add constraint_respected for hard CI."
                ),
            )
        )

    dtmf_steps = [s for s in steps if getattr(s, "action", None) == "dtmf"]
    if dtmf_steps and "draft" not in tags:
        findings.append(
            AuthoringWarning(
                code="dtmf_untagged_draft",
                severity="info",
                message=(
                    "Script action=dtmf present — tag scenario draft until the agent under test "
                    "handles SIP DTMF (sim can send; many agents only parse spoken digits)."
                ),
            )
        )

    if _silent_mode(persona):
        findings.append(
            AuthoringWarning(
                code="silent_mode_active",
                severity="info",
                message=(
                    "silent_mode=true: freestyle/nudge/auto barge-noise are suppressed — "
                    "assert agent reprompt/timeout/ended_by rather than goals_met speech."
                ),
            )
        )

    return findings


def collect_authoring_warnings(scenario: Any) -> list[str]:
    """Return soft authoring warning messages (backward-compatible flat list)."""
    return [f.message for f in collect_authoring_findings(scenario) if f.severity == "warn"]


def authoring_scorecard(scenario: Any) -> dict[str, Any]:
    """Lightweight dimensions for report/debug (not a hard gate).

    Dimensions are 0–2 each. Max 12:
    goals, constraints, behavior, assertion, risk_tags, interaction_proof.
    """
    persona = getattr(scenario, "persona", None) or {}
    if not isinstance(persona, dict):
        persona = {}
    goals = _persona_goals(persona)
    constraints = _persona_constraints(persona)
    barges = _recovery_barge_steps(scenario)
    has_assert = getattr(scenario, "asserts", None) is not None
    tags = _scenario_tags(scenario)
    has_risk = any(t in RISK_TAGS or t.startswith("risk:") for t in tags)
    steps = list(getattr(scenario, "script_steps", None) or [])
    has_behavior = bool(barges) or bool(getattr(scenario, "behavior_spec", None)) or bool(steps)
    has_interaction_proof = (
        _has_recovery_proof(scenario)
        or _has_ended_by_proof(scenario)
        or _has_constraint_proof(scenario)
    )

    dims = {
        "goals": 2 if goals else 0,
        "constraints": 2 if constraints else (1 if goals else 0),
        "behavior": 2 if has_behavior else 0,
        "assertion": 0,
        "risk_tags": 2 if has_risk else (1 if tags else 0),
        "interaction_proof": 2 if has_interaction_proof else (1 if has_assert else 0),
    }
    if has_assert and barges and not _has_recovery_proof(scenario):
        dims["assertion"] = 1
    elif has_assert:
        dims["assertion"] = 2

    total = sum(dims.values())
    return {"dimensions": dims, "total": total, "max": 12}


def authoring_tier(scorecard: dict[str, Any], findings: list[AuthoringWarning]) -> str:
    """Map score + warn codes → suite tier (soft recommendation only)."""
    total = int(scorecard.get("total") or 0)
    max_s = int(scorecard.get("max") or 12)
    codes = {f.code for f in findings if f.severity == "warn"}
    critical = {"empty_goals", "barge_without_recovery", "stress_trait_without_interaction"}
    if codes & critical or total < max(4, max_s // 3):
        return "exploratory"
    if total >= max(8, (max_s * 2) // 3) and not (codes & critical):
        return "blocking"
    return "scheduled"


def build_authoring_report(scenario: Any) -> dict[str, Any]:
    """Full structured authoring payload for validate_scenario."""
    findings = collect_authoring_findings(scenario)
    scorecard = authoring_scorecard(scenario)
    tier = authoring_tier(scorecard, findings)
    warn_findings = [f for f in findings if f.severity == "warn"]
    info_findings = [f for f in findings if f.severity == "info"]
    return {
        "scorecard": scorecard,
        "tier": tier,
        "warnings": [f.as_dict() for f in warn_findings],
        "infos": [f.as_dict() for f in info_findings],
        "warning_codes": [f.code for f in warn_findings],
        "info_codes": [f.code for f in info_findings],
        "message": (
            f"authoring tier={tier} score={scorecard['total']}/{scorecard['max']} "
            f"warns={len(warn_findings)} (soft — does not fail valid)"
        ),
        "soft": True,
    }
