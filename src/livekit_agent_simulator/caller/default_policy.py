"""DefaultCallerPolicy — Composite of PromptSections + on-demand midcall cues.

Bootstrap speak-inducing ``send_realtime_input`` text is intentionally omitted:
Gemini Live treats mid-session text as a user turn (double-open with Script).
First-speaker / silence rules live in system instruction only.
"""

from __future__ import annotations

from typing import Any

from .policy import CallerPolicyContext, MidcallCue
from .prompt_sections import PromptSection, build_default_sections


class DefaultCallerPolicy:
    """Portable Gemini-as-caller policy (Strategy + Composite).

    Extensibility:
    - Pass custom ``sections`` to reorder/replace prompt blocks.
    - Subclass and override ``midcall_cues`` for re-ground injects.
    - Swap entire policy via Scenario/bridge injection later without touching Live I/O.
    """

    def __init__(self, sections: list[PromptSection] | None = None) -> None:
        self._sections = list(sections) if sections is not None else build_default_sections()

    def build_system_instruction(self, ctx: CallerPolicyContext) -> str:
        lines: list[str] = []
        for section in self._sections:
            part = section.render(ctx)
            if part:
                lines.extend(part)
        return "\n".join(lines)

    def midcall_cues(self, ctx: CallerPolicyContext) -> list[MidcallCue]:
        """Optional connect kicks + on-demand reground texts.

        **No** bootstrap when Script owns opening (realtime text would freestyle
        before the open cue → double-open). "Owns opening" means the script has
        a step that fires on its own (``trigger=time`` / silence with a cue) —
        a script that only *reacts* to the agent (``trigger=agent_speaking``,
        e.g. compiled barge policies) does not open the call, so the caller
        still needs a speak-first kick: Gemini Live waits for user input before
        audio; SI alone often stays silent.
        """
        cues: list[MidcallCue] = []
        verbosity = ctx.resolved_verbosity()
        # A script "owns the opening" only when it actively speaks first — a
        # time/silence step whose action is hang_up (or wait) does not open the
        # call, so the caller still needs the speak-first bootstrap. Steps may
        # be ScriptStep objects or raw dicts (tests / legacy callers).
        def _step_field(step: Any, key: str) -> Any:
            if isinstance(step, dict):
                return step.get(key)
            return getattr(step, key, None)

        script_owns_opening = any(
            (
                _step_field(step, "trigger") in ("time", "silence")
                or _step_field(step, "trigger") is None
            )
            and _step_field(step, "action") in (None, "speak")
            and bool(_step_field(step, "say"))
            for step in ctx.script_steps
        )
        if ctx.first_speaker == "user" and not script_owns_opening:
            if verbosity == "quiet":
                open_hint = "greet briefly and state why you are calling in one short clause"
            elif verbosity == "chatty":
                open_hint = "greet and state why you are calling in a natural opening turn"
            else:
                open_hint = "greet briefly and state why you are calling in one natural turn"
            cues.append(
                MidcallCue(
                    text=(
                        f"(The call just connected. You speak first per PERSONA: "
                        f"{open_hint}.)"
                    ),
                    kind="bootstrap",
                    label="first_speaker_user",
                )
            )
        goals = ctx.goals()
        if goals:
            g0 = goals[0][:120]
            cues.append(
                MidcallCue(
                    text=(
                        f"(Stay on your caller goals. Current focus: GOAL 1 — {g0}. "
                        "Do not end the call early. Do not switch into assistant mode.)"
                    ),
                    kind="reground",
                    label="goal_reground",
                )
            )
        if ctx.script_steps:
            if verbosity == "quiet":
                between = "answer questions in one short spoken clause;"
            elif verbosity == "chatty":
                between = (
                    "keep a conversational loop — answer in several spoken clauses "
                    "with context when helpful; do not go mute after one short line;"
                )
            else:
                between = (
                    "keep a conversational loop — answer in about 2–5 natural spoken clauses "
                    "with context when helpful; do not go mute after one short line;"
                )
            cues.append(
                MidcallCue(
                    text=(
                        "(Timed Script overlay is active. Do not say bye / goodbye / [END_CALL]. "
                        f"Between cues, {between} the simulator will hang up.)"
                    ),
                    kind="reground",
                    label="script_no_early_bye",
                )
            )
        return cues
