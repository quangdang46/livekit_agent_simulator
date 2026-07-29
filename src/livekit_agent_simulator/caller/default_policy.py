"""DefaultCallerPolicy — Composite of PromptSections + on-demand midcall cues.

Bootstrap speak-inducing ``send_realtime_input`` text is intentionally omitted:
Gemini Live treats mid-session text as a user turn (double-open with Script).
First-speaker / silence rules live in system instruction only.
"""

from __future__ import annotations

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
        before the open cue → double-open). Dialogue ``first_speaker=user``
        without Script still needs a speak-first kick: Gemini Live waits for
        user input before audio; SI alone often stays silent.
        """
        cues: list[MidcallCue] = []
        verbosity = ctx.resolved_verbosity()
        if ctx.first_speaker == "user" and not ctx.script_steps:
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
