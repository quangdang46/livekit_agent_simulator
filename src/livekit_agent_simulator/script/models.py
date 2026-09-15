"""Script step / verify models (pure data, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

SUPPORTED_TRIGGERS = frozenset({"agent_speaking", "silence", "time"})
SUPPORTED_ACTIONS = frozenset({"speak", "wait", "hang_up", "dtmf"})

# Hamming-aligned mid-call input classes (P1.F). JSON field name: ``class``.
INTERRUPTION_CLASSES = frozenset(
    {"correction", "backchannel", "noise", "dtmf", "silence", "escalate"}
)
# Only these (with barge_in) feed recovery asserts / barge_recovery_rate.
RECOVERY_BARGE_CLASSES = frozenset({"correction", "escalate"})


def normalize_interrupt_class(
    raw: Any,
    *,
    barge_in: bool = False,
    default_when_barge: str = "correction",
) -> str | None:
    """Return a supported class or None.

    ``barge_in=True`` without class defaults to ``correction`` so existing scenarios
    keep recovery semantics.
    """
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return default_when_barge if barge_in else None
    key = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "true_correction": "correction",
        "correct": "correction",
        "barge": "correction",
        "ack": "backchannel",
        "uhhuh": "backchannel",
        "uh_huh": "backchannel",
        "false_positive": "noise",
        "false_interrupt": "noise",
        "click": "noise",
        "digit": "dtmf",
        "digits": "dtmf",
        "human": "escalate",
        "handoff": "escalate",
        "safety": "escalate",
    }
    key = aliases.get(key, key)
    if key not in INTERRUPTION_CLASSES:
        raise ValueError(
            f"unsupported interrupt class {raw!r} "
            f"(supported: {sorted(INTERRUPTION_CLASSES)})"
        )
    return key


def counts_for_recovery_barge(
    *,
    barge_in: bool,
    interrupt_class: str | None,
) -> bool:
    """True when this cue should drive recovery asserts / barge_recovery_rate."""
    if not barge_in:
        return False
    cls = interrupt_class or "correction"
    return cls in RECOVERY_BARGE_CLASSES


# --- Event-vocabulary bridge (caller_contract migration) --------------------
#
# The caller log has TWO vocabularies for the same caller action:
#
#   legacy  : sim.script.cue{barge_in, interrupt_class}  /  interruption{by=sim}
#   contract: contract.barge{class}  /  contract.interrupt{class}
#
# Only the legacy emitters (ScriptRunner, InterruptRateRunner, the Realtime
# session pump) ever wrote the legacy kinds, and none of them is instantiated
# on the contract path — so a reader that only understands the legacy spelling
# counts ZERO barges for a contract run. That silently fails every
# ``type: recovery`` outcome whose ``min_agent_finals_after_barge_in`` is set,
# and reports barge_count=0 / recovery_rate=None in metrics.
#
# These helpers are the single place that knows both spellings, so asserts,
# metrics and authoring can never drift apart again.

# Contract kinds that represent the caller talking over the agent.
CONTRACT_BARGE_KINDS = frozenset({"contract.barge", "contract.interrupt"})

# Classes that are never a *recovery* barge even though they are cut-ins.
_NON_RECOVERY_CUTIN_CLASSES = frozenset({"noise", "backchannel", "dtmf", "silence"})


def _spec_of(event: Mapping[str, Any]) -> Mapping[str, Any]:
    spec = event.get("spec")
    return spec if isinstance(spec, Mapping) else {}


def is_recovery_barge_event(event: Mapping[str, Any]) -> bool:
    """True when this log event is a recovery-relevant caller barge.

    Understands both the legacy and the contract event vocabulary — see the
    module note above. ``contract.policy_interrupt`` (the seeded backchannel
    cut-in) is deliberately NOT a recovery barge: it is a backchannel, and
    ``RECOVERY_BARGE_CLASSES`` excludes those.
    """
    kind = str(event.get("kind") or "")
    spec = _spec_of(event)

    if kind == "sim.script.cue":
        return counts_for_recovery_barge(
            barge_in=bool(spec.get("barge_in")),
            interrupt_class=_class_of(spec),
        )
    if kind == "interruption":
        if not (spec.get("barge_in") or str(spec.get("by") or "") == "sim"):
            return False
        if spec.get("false_positive"):
            return False
        return counts_for_recovery_barge(
            barge_in=True, interrupt_class=_class_of(spec)
        )
    if kind in CONTRACT_BARGE_KINDS:
        return counts_for_recovery_barge(
            barge_in=True, interrupt_class=_class_of(spec)
        )
    return False


def is_interruption_event(event: Mapping[str, Any]) -> bool:
    """True when this log event counts as a caller interruption.

    Covers the legacy ``interruption`` kind and every contract cut-in kind, so
    ``min_interruptions`` / ``interruption_count`` mean the same thing on both
    paths.

    ``contract.policy_interrupt`` counts here but is NOT a recovery barge:
    a backchannel cut-in is still an interruption, it just is not a
    *correction/escalate* one (see ``RECOVERY_BARGE_CLASSES``). The legacy
    seeded runner drew the same line — it always emitted ``interruption``,
    and the class filter decided whether it counted for recovery.
    """
    kind = str(event.get("kind") or "")
    if kind == "interruption":
        return True
    return kind in CONTRACT_BARGE_KINDS or kind == "contract.policy_interrupt"


def _class_of(spec: Mapping[str, Any]) -> str | None:
    cls = spec.get("class") or spec.get("interrupt_class")
    return str(cls) if cls else None


# --- End-of-call attribution, both vocabularies ----------------------------
#
# `run.end_condition.reason` is written by whichever caller engine ran, and the
# two engines spell it differently:
#
#   legacy  : sim_end_call / agent_disconnected / dead_call_silence / max_turns / timeout
#   contract: contract_scenario_end / contract_caller_end / contract_agent_end /
#             contract_timeout / contract_transport_error / contract_error
#             (live_wiring.py's EndedBy -> reason map)
#
# A reader that knows only the legacy spellings reports "detect" for every
# contract run, which fails any `type: ended_by` assert that names a side.

# Contract reason -> the assert vocabulary (sim | agent). Absent = no side.
_CONTRACT_END_SIDES: dict[str, str] = {
    # The scenario's caller_steps ran to completion (or hit `end: true`):
    # the simulated caller drove the ending.
    "contract_scenario_end": "sim",
    "contract_caller_end": "sim",
    "contract_agent_end": "agent",
}

_LEGACY_END_SIDES: dict[str, str] = {
    "sim_end_call": "sim",
    "agent_disconnected": "agent",
    "dead_call_silence": "agent",
}


def end_side_from_reason(reason: str) -> str | None:
    """Map a `run.end_condition` reason to the side that ended the call.

    Returns "sim" / "agent", or None when the reason carries no side
    (timeout, max_turns, transport). Understands both the legacy and the
    contract spelling — see the module note above.
    """
    key = str(reason or "")
    for table in (_CONTRACT_END_SIDES, _LEGACY_END_SIDES):
        if key in table:
            return table[key]
    return None


@dataclass(frozen=True)
class ScriptStep:
    id: str
    trigger: str
    delay_ms: int
    say: str = ""
    label: str = ""
    once: bool = True
    min_agent_active_ms: int = 400
    delivery: str = "gemini_text"  # gemini_text | room_pcm
    asset: str | None = None
    silence_after_cue_ms: int = 0
    action: str = "speak"
    # wait + silence_after_cue_ms: None/False = pace only (caller keeps answering).
    # True = mute freestyle (intentional dead-air / unresponsive tests).
    mute_persona: bool | None = None

    # DTMF digit string: only valid when action="dtmf".
    # characters 0-9*#w (w = wait 120ms gap).
    digits: str = ""
        # Continuous ambient bed for room_pcm noise (re-queues until hang-up).
    # Distinct from once= (fire this step once). Only valid with delivery=room_pcm.
    loop: bool = False  # speak | wait | hang_up
    # For silence trigger: only start counting idle after agent has spoken once.
    require_agent_spoke_first: bool = True
    # hang_up: do not fire while user spoke and agent has not answered that turn yet.
    require_agent_reply_this_turn: bool = True
    # hang_up: defer while last agent final still expects a caller reply (open ? / prompt).
    # After open_question_idle_ms of no user reply, hang_up may proceed (ghost hang).
    defer_on_open_question: bool = True
    open_question_idle_ms: int = 20000
    barge_in: bool = False
    # When barge_in + gemini_text: play builtin noise.blip first (audible cut-in).
    with_blip: bool = True
    # Linear playback gain for this cue (0.0–1.0). Applies to gemini_text TTS and room_pcm.
    gain: float = 1.0
    # Hamming class: correction | backchannel | noise | dtmf | silence | escalate
    interrupt_class: str | None = None
    # Overlay role: fixture (PCM/barge/noise) | line (forced say) | None → auto
    overlay: str | None = None


OVERLAY_ROLES = frozenset({"fixture", "line"})


def effective_overlay(step: ScriptStep) -> str:
    """Classify Script step as audio fixture vs forced spoken line."""
    if step.overlay in OVERLAY_ROLES:
        return str(step.overlay)
    if (
        step.barge_in
        or step.delivery == "room_pcm"
        or (step.interrupt_class or "") in ("noise", "backchannel", "dtmf", "silence")
    ):
        return "fixture"
    if step.action == "speak" and str(step.say or "").strip():
        return "line"
    return "fixture"


@dataclass(frozen=True)
class ScriptVerifySpec:
    require_during_agent_speech: bool = True
    min_agent_finals_after_first_cue: int = 0
    min_user_finals_after_first_cue: int = 0
    min_interruptions: int | None = None
    max_interruptions: int | None = None
    # After a silence-wait step, require agent transcript final later (agent re-prompts).
    min_agent_finals_after_silence: int = 0
    # After a barge_in cue, require agent to speak again (recovery).
    min_agent_finals_after_barge_in: int = 0
    plugins: tuple[str, ...] = ()
    plugin_options: dict[str, Any] = field(default_factory=dict)
