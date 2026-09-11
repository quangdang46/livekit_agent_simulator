"""P0-3: Behavior DSL — parses scenario caller steps into CallerAction list.

Extends the existing scenario step vocabulary (see scenario.py / script_parse.py
KNOWN_KINDS "Behavior"/"Script") with two new primitives:

    say: "exact text"          -> 100% deterministic utterance, no AI, no
                                   validator — but STILL gated by the
                                   Orchestrator turn (never a bypass of the
                                   turn/timing invariant, only of AI+Validator).
    do:  {behavior: ..., ...}  -> adaptive behavior with a BehaviorContract;
                                   constraint field names are imported from
                                   caller_contract (P0-1), never redeclared
                                   here, so the DSL and the contract schema
                                   can never drift apart.

Plus existing/adjacent action kinds carried through as CallerAction.kind:
wait, dtmf, interrupt, end, silence, hangup.

Both a shorthand form (``do: ask_price``) and an explicit mapping form
(``do: {behavior: ask, target: price, constraints: {...}}``) are accepted.

Unknown top-level keys are a hard parse error naming the file, line, and
field — never a silent ignore (report §21/§28 self-containment convention).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import BehaviorContract, ContractConstraints

# Default behavior allowlist. This is a *default* catalog for convenience —
# scenarios may extend it via `known_behaviors=` on parse_steps(); it must
# never be hardcoded business vocabulary (AGENTS.md generic-core rule), only
# the small set of generic conversational primitives the report settles on.
DEFAULT_BEHAVIOR_CATALOG = frozenset(
    {
        "ask",
        "confirm",
        "deny",
        "accept",
        "reject",
        "negotiate",
        "clarify",
        "provide",
        "arrange_visit",
        "interrupt",
        "end",
    }
)

# Fields accepted at the top level of an explicit `do:` mapping. Anything
# else is an unknown-key parse error.
_DO_ALLOWED_KEYS = frozenset({"behavior", "target", "constraints", "interaction"})
_CONSTRAINTS_ALLOWED_KEYS = frozenset(
    {"max_turns", "max_budget", "max_words", "max_duration_s", "forbidden_intents", "must_not"}
)
_INTERACTION_ALLOWED_KEYS = frozenset(
    {
        "pace",
        "hesitation",
        "stumble",
        "pre_delay",
        "backchannel",
        "barge_in",
        "interrupt_class",
        "interruption_rate",
        "interruption_interval_ms",
        "interruption_seed",
    }
)
_INTERRUPT_CLASSES = frozenset({"correction", "backchannel"})
_INTERRUPTION_RATES = frozenset({"low", "medium", "high"})

_KNOWN_ACTION_KINDS = frozenset(
    {"say", "do", "wait", "dtmf", "interrupt", "play_audio", "end", "silence", "hangup"}
)

_PLAY_AUDIO_ALLOWED_KEYS = frozenset({"asset", "gain", "loop"})


class DSLError(Exception):
    """Malformed scenario step. Message must name file, line, and field."""

    def __init__(self, message: str, *, file: str | None = None, line: int | None = None, field: str | None = None):
        self.file = file
        self.line = line
        self.field = field
        located = f"{file or '<scenario>'}:{line if line is not None else '?'}"
        super().__init__(f"{located}: {message}" + (f" (field={field})" if field else ""))


@dataclass
class InteractionConfig:
    """Delivery config consumed by the Caller Interaction Planner (P0-5b).

    Kept here (not renamed to speech-policy/delivery-config — see bead
    naming convention) purely as a passthrough data holder; the DSL does
    not interpret these values, it only validates the key names.
    """

    pace: str | None = None
    hesitation: str | None = None
    stumble: str | None = None
    pre_delay_ms: int | None = None
    backchannel: bool | None = None
    barge_in: bool | None = None
    interrupt_class: str | None = None
    # Seeded interruption policy (contract equivalent of the legacy
    # InterruptRateRunner): while a ``do:`` behavior runs, the driver may
    # emit a fixed backchannel cut-in at most once per interval while the
    # agent is speaking. Deterministic in (scenario_id, seed, agent-turn
    # index) — never an LLM decision. None = off (default).
    interruption_rate: str | None = None
    interruption_interval_ms: int | None = None
    interruption_seed: int | None = None


@dataclass
class TriggerConfig:
    """WHEN a ``say`` action may fire (Slice 4: minimal trigger/delay).

    Mirrors the legacy ScriptRunner trigger vocabulary (see script/models.py)
    minus everything deferred: no ``caller_turn`` (never existed in legacy),
    no ``once``/``loop`` (the driver walks the action list once: sequential
    order IS once), no hangup-defer/open-question gating.

    Semantics per kind:
    - ``time``: fire ``delay_ms`` after the action is armed. ``min_agent_active_ms``
      is accepted but ignored.
    - ``agent_speaking``: fire once the agent has spoken continuously for
      ``min_agent_active_ms``, then wait ``delay_ms`` unconditionally.
    - ``silence``: fire once the agent has been silent continuously for
      ``delay_ms`` — here ``delay_ms`` IS the required silence duration.

    ``delay_ms`` never aliases ``interaction.pre_delay_ms``: pre-delay is
    unconditional pacing applied first; trigger delay applies after the
    condition fires. Total delay = pre_delay + trigger delay.
    """

    kind: str  # "time" | "agent_speaking" | "silence"
    delay_ms: int = 0
    min_agent_active_ms: int = 400  # legacy default; only meaningful for agent_speaking


_TRIGGER_KINDS = frozenset({"time", "agent_speaking", "silence"})

_TRIGGER_ALLOWED_KEYS = frozenset({"kind", "delay_ms", "min_agent_active_ms"})

# Sibling keys allowed next to the action verb (e.g. ``{say: ..., trigger: ...}``).
# Anything else is a hard DSLError — without this, a typo'd ``trigger:`` would
# be silently dropped and the action would fire immediately (see parse_step).
_SAY_SIBLING_KEYS = frozenset({"say", "interaction", "trigger", "barge_in"})


@dataclass
class CallerAction:
    """One parsed scenario step, ready for the Orchestrator (P0-4).

    ``requires_turn_gate`` is always True: even ``say`` (which bypasses the
    AI Language Adapter and the Caller Contract Validator) must still pass
    through the Orchestrator's caller-turn gate before publishing audio —
    see report §28.5(17) / bead notes.

    ``trigger`` gates WHEN a ``say`` fires (None = immediately, legacy
    ``say`` behavior). ``barge_in`` only matters on ``say``: True skips the
    wait-for-agent-silence gate so the caller can interrupt mid-sentence.
    """

    kind: str  # one of _KNOWN_ACTION_KINDS
    line_no: int
    say_text: str | None = None
    contract: BehaviorContract | None = None
    interaction: InteractionConfig | None = None
    trigger: TriggerConfig | None = None
    barge_in: bool = False
    dtmf_digits: str | None = None
    wait_ms: int | None = None
    audio_asset: str | None = None
    audio_gain: float = 1.0
    audio_loop: bool = False
    requires_turn_gate: bool = True
    bypasses_ai_and_validator: bool = False

    def __post_init__(self) -> None:
        if self.kind == "say":
            self.bypasses_ai_and_validator = True
        if self.kind == "play_audio":
            # Audio beds are delivery, never semantic content.
            self.bypasses_ai_and_validator = True


def _err(msg: str, *, file: str | None, line: int, field: str | None = None) -> DSLError:
    return DSLError(msg, file=file, line=line, field=field)


def _parse_constraints(raw: dict[str, Any] | None, *, file: str | None, line: int) -> ContractConstraints:
    raw = raw or {}
    unknown = set(raw.keys()) - _CONSTRAINTS_ALLOWED_KEYS
    if unknown:
        raise _err(f"unknown constraints key(s): {sorted(unknown)}", file=file, line=line, field="constraints")
    # Field names are imported straight from ContractConstraints (P0-1) — the
    # dict is passed through as-is via **raw, so any drift between this
    # parser and the contract schema fails loudly as a TypeError, not
    # silently as a mismatched validator check downstream.
    try:
        constraints = ContractConstraints(**raw)
    except TypeError as exc:  # pragma: no cover - guarded by _CONSTRAINTS_ALLOWED_KEYS above
        raise _err(f"invalid constraints: {exc}", file=file, line=line, field="constraints") from exc
    constraints.validate()
    return constraints


def _parse_interaction(raw: dict[str, Any] | None, *, file: str | None, line: int) -> InteractionConfig | None:
    if raw is None:
        return None
    unknown = set(raw.keys()) - _INTERACTION_ALLOWED_KEYS
    if unknown:
        raise _err(f"unknown interaction key(s): {sorted(unknown)}", file=file, line=line, field="interaction")
    interrupt_class = raw.get("interrupt_class")
    if interrupt_class is not None and interrupt_class not in _INTERRUPT_CLASSES:
        raise _err(
            f"unknown interrupt_class {interrupt_class!r}; expected one of {sorted(_INTERRUPT_CLASSES)}",
            file=file,
            line=line,
            field="interaction.interrupt_class",
        )
    rate = raw.get("interruption_rate")
    if rate is not None:
        rate = str(rate).strip().lower()
        if rate in ("", "none", "off"):
            rate = None
        elif rate not in _INTERRUPTION_RATES:
            raise _err(
                f"unknown interruption_rate {rate!r}; expected one of {sorted(_INTERRUPTION_RATES)}",
                file=file,
                line=line,
                field="interaction.interruption_rate",
            )
    interval_ms = raw.get("interruption_interval_ms")
    if interval_ms is not None and (not isinstance(interval_ms, int) or interval_ms < 1000):
        raise _err(
            "interaction.interruption_interval_ms must be an integer >= 1000",
            file=file,
            line=line,
            field="interaction.interruption_interval_ms",
        )
    seed = raw.get("interruption_seed")
    if seed is not None and (not isinstance(seed, int) or seed < 0):
        raise _err(
            "interaction.interruption_seed must be a non-negative integer",
            file=file,
            line=line,
            field="interaction.interruption_seed",
        )
    if (interval_ms is not None or seed is not None) and rate is None:
        raise _err(
            "interaction.interruption_interval_ms/seed require interruption_rate",
            file=file,
            line=line,
            field="interaction.interruption_rate",
        )
    return InteractionConfig(
        pace=raw.get("pace"),
        hesitation=raw.get("hesitation"),
        stumble=raw.get("stumble"),
        pre_delay_ms=raw.get("pre_delay_ms", raw.get("pre_delay")),
        backchannel=raw.get("backchannel"),
        barge_in=raw.get("barge_in"),
        interrupt_class=interrupt_class,
        interruption_rate=rate,
        interruption_interval_ms=interval_ms,
        interruption_seed=seed,
    )


def _parse_trigger(raw: Any, *, file: str | None, line: int) -> TriggerConfig | None:
    """Parse an optional ``trigger:`` sibling mapping on a ``say`` step."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _err("trigger: must be a mapping with a 'kind' key", file=file, line=line, field="trigger")
    unknown = set(raw.keys()) - _TRIGGER_ALLOWED_KEYS
    if unknown:
        raise _err(f"unknown trigger: key(s): {sorted(unknown)}", file=file, line=line, field="trigger")
    if "kind" not in raw:
        raise _err("trigger: mapping requires a 'kind' key", file=file, line=line, field="trigger.kind")
    kind = raw["kind"]
    if kind not in _TRIGGER_KINDS:
        raise _err(
            f"unknown trigger kind {kind!r}; expected one of {sorted(_TRIGGER_KINDS)}",
            file=file,
            line=line,
            field="trigger.kind",
        )
    delay_ms = raw.get("delay_ms", 0)
    if not isinstance(delay_ms, int) or delay_ms < 0:
        raise _err("trigger.delay_ms must be a non-negative integer (milliseconds)", file=file, line=line, field="trigger.delay_ms")
    min_active = raw.get("min_agent_active_ms", 400)
    if not isinstance(min_active, int) or min_active < 0:
        raise _err(
            "trigger.min_agent_active_ms must be a non-negative integer (milliseconds)",
            file=file,
            line=line,
            field="trigger.min_agent_active_ms",
        )
    return TriggerConfig(kind=kind, delay_ms=delay_ms, min_agent_active_ms=min_active)


def _parse_do(
    raw: Any,
    *,
    file: str | None,
    line: int,
    known_behaviors: frozenset[str],
) -> CallerAction:
    if isinstance(raw, str):
        # Shorthand: `do: ask_price` -> behavior=ask_price, no explicit target/constraints.
        behavior_name = raw
        target = None
        constraints_raw: dict[str, Any] | None = None
        interaction_raw: dict[str, Any] | None = None
    elif isinstance(raw, dict):
        unknown = set(raw.keys()) - _DO_ALLOWED_KEYS
        if unknown:
            raise _err(f"unknown do: key(s): {sorted(unknown)}", file=file, line=line, field="do")
        if "behavior" not in raw:
            raise _err("do: mapping requires a 'behavior' key", file=file, line=line, field="do.behavior")
        behavior_name = raw["behavior"]
        target = raw.get("target")
        constraints_raw = raw.get("constraints")
        interaction_raw = raw.get("interaction")
    else:
        raise _err(f"do: must be a string or mapping, got {type(raw).__name__}", file=file, line=line, field="do")

    if behavior_name not in known_behaviors:
        raise _err(
            f"unknown behavior '{behavior_name}'; known behaviors: {sorted(known_behaviors)}",
            file=file,
            line=line,
            field="do.behavior",
        )

    constraints = _parse_constraints(constraints_raw, file=file, line=line)
    contract = BehaviorContract(behavior=behavior_name, target=target, constraints=constraints)
    contract.validate()
    interaction = _parse_interaction(interaction_raw, file=file, line=line)

    return CallerAction(kind="do", line_no=line, contract=contract, interaction=interaction)


def parse_step(
    raw_step: dict[str, Any],
    *,
    line_no: int,
    file: str | None = None,
    known_behaviors: frozenset[str] = DEFAULT_BEHAVIOR_CATALOG,
) -> CallerAction:
    """Parse a single scenario step dict into a CallerAction.

    Exactly one of the recognized top-level keys (say/do/wait/dtmf/
    interrupt/end/silence/hangup) must be present.
    """
    present = _KNOWN_ACTION_KINDS & set(raw_step.keys())
    if len(present) == 0:
        raise _err(
            f"step has no recognized action key; expected one of {sorted(_KNOWN_ACTION_KINDS)}",
            file=file,
            line=line_no,
        )
    if len(present) > 1:
        raise _err(f"step has multiple action keys {sorted(present)}; exactly one is allowed", file=file, line=line_no)

    kind = next(iter(present))

    if kind == "say":
        value = raw_step["say"]
        if not isinstance(value, str):
            raise _err("say: must be a plain string, not a mapping", file=file, line=line_no, field="say")
        if not value.strip():
            raise _err("say: must not be empty", file=file, line=line_no, field="say")
        unknown_siblings = set(raw_step.keys()) - _SAY_SIBLING_KEYS
        if unknown_siblings:
            raise _err(
                f"unknown say: sibling key(s): {sorted(unknown_siblings)}",
                file=file,
                line=line_no,
                field="say",
            )
        interaction = _parse_interaction(raw_step.get("interaction"), file=file, line=line_no)
        trigger = _parse_trigger(raw_step.get("trigger"), file=file, line=line_no)
        barge_in = raw_step.get("barge_in", False)
        if not isinstance(barge_in, bool):
            raise _err("barge_in: must be a boolean", file=file, line=line_no, field="barge_in")
        return CallerAction(
            kind="say",
            line_no=line_no,
            say_text=value,
            interaction=interaction,
            trigger=trigger,
            barge_in=barge_in,
        )

    if kind == "do":
        if "trigger" in raw_step or "barge_in" in raw_step:
            raise _err(
                "trigger:/barge_in: are only supported on say: steps in this slice",
                file=file,
                line=line_no,
                field="do",
            )
        return _parse_do(raw_step["do"], file=file, line=line_no, known_behaviors=known_behaviors)

    if kind in ("dtmf", "wait"):
        if "trigger" in raw_step or "barge_in" in raw_step:
            raise _err(
                "trigger:/barge_in: are only supported on say: steps in this slice",
                file=file,
                line=line_no,
                field=kind,
            )
        if kind == "dtmf":
            digits = raw_step["dtmf"]
            if not isinstance(digits, str) or not digits:
                raise _err("dtmf: must be a non-empty digit string", file=file, line=line_no, field="dtmf")
            return CallerAction(kind="dtmf", line_no=line_no, dtmf_digits=digits)
        ms = raw_step["wait"]
        if not isinstance(ms, int) or ms < 0:
            raise _err("wait: must be a non-negative integer (milliseconds)", file=file, line=line_no, field="wait")
        return CallerAction(kind="wait", line_no=line_no, wait_ms=ms)

    if kind == "play_audio":
        pass  # handled below (trigger allowed, barge_in rejected there)
    elif "trigger" in raw_step or "barge_in" in raw_step:
        raise _err(
            "trigger:/barge_in: are only supported on say: steps in this slice",
            file=file,
            line=line_no,
            field=kind,
        )
    if kind == "interrupt":
        # Explicit caller cut-in: optional interaction.interrupt_class only.
        # Any other sibling is a hard error (no silent config).
        allowed = {"interrupt", "interaction"}
        unknown = set(raw_step.keys()) - allowed
        if unknown:
            raise _err(
                f"unknown interrupt: sibling key(s): {sorted(unknown)}",
                file=file,
                line=line_no,
                field="interrupt",
            )
        raw_flag = raw_step.get("interrupt", True)
        if raw_flag not in (True, None) and not (
            isinstance(raw_flag, dict) and not raw_flag
        ):
            raise _err(
                "interrupt: takes no value (optionally interaction: {interrupt_class})",
                file=file,
                line=line_no,
                field="interrupt",
            )
        interaction = _parse_interaction(
            raw_step.get("interaction"), file=file, line=line_no
        )
        return CallerAction(
            kind="interrupt", line_no=line_no, interaction=interaction
        )
    if kind == "play_audio":
        # Delivery primitive for background beds (ambient/office noise):
        # an asset ref resolved at publish time, never a say: string.
        # ``say: "noise.wav"`` is a design violation — audio files are not
        # utterances and must never flow through TTS or the validator.
        raw_spec = raw_step["play_audio"]
        if not isinstance(raw_spec, dict):
            raise _err(
                "play_audio: must be a mapping {asset:, gain:, loop:}",
                file=file,
                line=line_no,
                field="play_audio",
            )
        unknown = set(raw_spec.keys()) - _PLAY_AUDIO_ALLOWED_KEYS
        if unknown:
            raise _err(
                f"unknown play_audio: key(s): {sorted(unknown)}",
                file=file,
                line=line_no,
                field="play_audio",
            )
        extra_siblings = set(raw_step.keys()) - {"play_audio", "trigger"}
        if extra_siblings:
            raise _err(
                f"unknown play_audio: sibling key(s): {sorted(extra_siblings)}",
                file=file,
                line=line_no,
                field="play_audio",
            )
        asset = raw_spec.get("asset")
        if not isinstance(asset, str) or not asset.strip():
            raise _err(
                "play_audio.asset must be a non-empty asset ref",
                file=file,
                line=line_no,
                field="play_audio.asset",
            )
        gain = raw_spec.get("gain", 1.0)
        if not isinstance(gain, (int, float)) or not 0.0 <= float(gain) <= 1.0:
            raise _err(
                "play_audio.gain must be a number in [0.0, 1.0]",
                file=file,
                line=line_no,
                field="play_audio.gain",
            )
        loop = raw_spec.get("loop", False)
        if not isinstance(loop, bool):
            raise _err(
                "play_audio.loop must be a boolean",
                file=file,
                line=line_no,
                field="play_audio.loop",
            )
        trigger = _parse_trigger(raw_step.get("trigger"), file=file, line=line_no)
        return CallerAction(
            kind="play_audio",
            line_no=line_no,
            audio_asset=asset.strip(),
            audio_gain=float(gain),
            audio_loop=loop,
            trigger=trigger,
        )
    # end / silence / hangup: presence-only control actions. Any sibling
    # key is a hard error — an unknown field here must never silently
    # disappear (same invariant as say:/interrupt:/play_audio:).
    if kind in ("end", "silence", "hangup"):
        allowed = {kind}
        unknown = set(raw_step.keys()) - allowed
        if unknown:
            raise _err(
                f"unknown {kind}: sibling key(s): {sorted(unknown)}",
                file=file,
                line=line_no,
                field=kind,
            )
        raw_flag = raw_step.get(kind, True)
        if raw_flag not in (True, None) and not (
            isinstance(raw_flag, dict) and not raw_flag
        ):
            raise _err(
                f"{kind}: takes no value",
                file=file,
                line=line_no,
                field=kind,
            )
        return CallerAction(kind=kind, line_no=line_no)
    raise _err(  # pragma: no cover - guarded by _KNOWN_ACTION_KINDS above
        f"unhandled action kind {kind!r}",
        file=file,
        line=line_no,
    )


def parse_steps(
    raw_steps: list[dict[str, Any]],
    *,
    file: str | None = None,
    known_behaviors: frozenset[str] = DEFAULT_BEHAVIOR_CATALOG,
) -> list[CallerAction]:
    """Parse an ordered list of scenario steps into CallerAction objects."""
    return [
        parse_step(step, line_no=idx + 1, file=file, known_behaviors=known_behaviors)
        for idx, step in enumerate(raw_steps)
    ]


def _unparse_interaction(interaction: InteractionConfig | None) -> dict[str, Any] | None:
    if interaction is None:
        return None
    out: dict[str, Any] = {}
    if interaction.pace is not None:
        out["pace"] = interaction.pace
    if interaction.hesitation is not None:
        out["hesitation"] = interaction.hesitation
    if interaction.stumble is not None:
        out["stumble"] = interaction.stumble
    if interaction.pre_delay_ms is not None:
        out["pre_delay_ms"] = interaction.pre_delay_ms
    if interaction.backchannel is not None:
        out["backchannel"] = interaction.backchannel
    if interaction.barge_in is not None:
        out["barge_in"] = interaction.barge_in
    if interaction.interrupt_class is not None:
        out["interrupt_class"] = interaction.interrupt_class
    if interaction.interruption_rate is not None:
        out["interruption_rate"] = interaction.interruption_rate
    if interaction.interruption_interval_ms is not None:
        out["interruption_interval_ms"] = interaction.interruption_interval_ms
    if interaction.interruption_seed is not None:
        out["interruption_seed"] = interaction.interruption_seed
    return out or None


def _unparse_trigger(trigger: TriggerConfig | None) -> dict[str, Any] | None:
    if trigger is None:
        return None
    out: dict[str, Any] = {"kind": trigger.kind}
    if trigger.delay_ms:
        out["delay_ms"] = trigger.delay_ms
    if trigger.kind == "agent_speaking" and trigger.min_agent_active_ms != 400:
        out["min_agent_active_ms"] = trigger.min_agent_active_ms
    elif trigger.kind != "agent_speaking" and trigger.min_agent_active_ms != 400:
        # Non-default on a kind that ignores it: preserve (strict re-parse
        # accepts it) rather than silently dropping author intent.
        out["min_agent_active_ms"] = trigger.min_agent_active_ms
    return out


def unparse_action(action: CallerAction) -> dict[str, Any]:
    """Rebuild the authored step dict for a parsed CallerAction.

    Inverse of parse_step for the round-trip export path (scenario_to_dict).
    Output re-parses to an equivalent action (same kind + semantic fields).
    """
    if action.kind == "say":
        assert action.say_text is not None
        step: dict[str, Any] = {"say": action.say_text}
        interaction = _unparse_interaction(action.interaction)
        if interaction is not None:
            step["interaction"] = interaction
        trigger = _unparse_trigger(action.trigger)
        if trigger is not None:
            step["trigger"] = trigger
        if action.barge_in:
            step["barge_in"] = True
        return step
    if action.kind == "do":
        assert action.contract is not None
        inner: dict[str, Any] = {"behavior": action.contract.behavior}
        if action.contract.target is not None:
            inner["target"] = action.contract.target
        constraints = action.contract.constraints
        cdict: dict[str, Any] = {}
        if constraints.max_turns != 3:
            cdict["max_turns"] = constraints.max_turns
        if constraints.max_budget is not None:
            cdict["max_budget"] = constraints.max_budget
        if constraints.max_words is not None:
            cdict["max_words"] = constraints.max_words
        if constraints.max_duration_s is not None:
            cdict["max_duration_s"] = constraints.max_duration_s
        if constraints.forbidden_intents:
            cdict["forbidden_intents"] = list(constraints.forbidden_intents)
        if constraints.must_not:
            cdict["must_not"] = list(constraints.must_not)
        if cdict:
            inner["constraints"] = cdict
        interaction = _unparse_interaction(action.interaction)
        if interaction is not None:
            inner["interaction"] = interaction
        return {"do": inner}
    if action.kind == "wait":
        return {"wait": action.wait_ms or 0}
    if action.kind == "dtmf":
        return {"dtmf": action.dtmf_digits or ""}
    if action.kind == "interrupt":
        step = {"interrupt": True}
        interaction = _unparse_interaction(action.interaction)
        if interaction is not None:
            return {"interrupt": True, "interaction": interaction}
        return step
    if action.kind == "play_audio":
        assert action.audio_asset is not None
        spec: dict[str, Any] = {"asset": action.audio_asset}
        if action.audio_gain != 1.0:
            spec["gain"] = action.audio_gain
        if action.audio_loop:
            spec["loop"] = True
        step = {"play_audio": spec}
        trigger = _unparse_trigger(action.trigger)
        if trigger is not None:
            step["trigger"] = trigger
        return step
    return {action.kind: True}


def unparse_steps(actions: list[CallerAction]) -> list[dict[str, Any]]:
    """Rebuild authored caller_steps for a parsed CallerAction list."""
    return [unparse_action(a) for a in actions]


__all__ = [
    "DEFAULT_BEHAVIOR_CATALOG",
    "CallerAction",
    "DSLError",
    "InteractionConfig",
    "TriggerConfig",
    "parse_step",
    "parse_steps",
    "unparse_action",
    "unparse_steps",
]
