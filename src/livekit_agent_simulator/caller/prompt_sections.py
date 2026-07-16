"""Prompt section builders — Composite pieces of the Live system instruction.

Google Live best practice order: persona → conversational rules → guardrails.
Each section is a small Strategy; DefaultCallerPolicy composes them.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .policy import CallerPolicyContext


@runtime_checkable
class PromptSection(Protocol):
    def render(self, ctx: CallerPolicyContext) -> list[str]:
        """Return zero or more lines (no trailing join)."""
        ...


class RoleSection:
    """Persona block (Google SI step 1)."""

    def render(self, ctx: CallerPolicyContext) -> list[str]:
        lang = ctx.locale
        lines = [
            "## PERSONA",
            "You are role-playing a HUMAN CALLER on a phone call with a voice assistant.",
            "You are NOT an assistant, agent, or support worker. Never offer help; you are the customer.",
            f"RESPOND IN {lang}. YOU MUST RESPOND UNMISTAKABLY IN {lang}.",
            "Keep every utterance short and natural like real phone speech (1-2 sentences).",
            "Never mention that you are an AI, a simulation, a test, or a judge.",
        ]
        p = ctx.persona
        if p.get("name"):
            lines.append(f"Your name: {p['name']}.")
        if p.get("brief"):
            lines.append(f"Who you are and why you are calling: {p['brief']}")
        return lines


class GoalsSection:
    """Ordered goals = conversational rules (Google SI step 2)."""

    def render(self, ctx: CallerPolicyContext) -> list[str]:
        goals = ctx.goals()
        if not goals:
            return []
        lines = [
            "",
            "## CONVERSATIONAL RULES — YOUR GOALS",
            "Complete each goal before moving to the next. Treat this as a checklist.",
        ]
        for i, g in enumerate(goals, 1):
            lines.append(f"GOAL {i}: {g}")
        if ctx.script_steps:
            lines.extend(
                [
                    "",
                    "Rules for goals when a timed Script is active:",
                    "1. Goals are context for Script cues — do NOT freestyle to finish signup yourself.",
                    "2. Speak Script cue lines when injected; between cues you may answer the assistant in 1–2 natural phone sentences.",
                    "3. Do NOT freestyle barge-ins, long monologues, or goodbye / [END_CALL]; Script hang-up ends the call.",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "Rules for goals (follow in order):",
                    "1. Work through ALL goals one by one.",
                    "2. Do NOT skip ahead to a later goal before the current one is addressed.",
                    "3. Do NOT say goodbye or [END_CALL] until ALL goals are addressed.",
                    "4. If the assistant cannot help with one goal, state that briefly and move to the next.",
                    "5. If the assistant goes off-topic, steer back to the current GOAL.",
                ]
            )
        return lines


class StyleTraitsSection:
    def render(self, ctx: CallerPolicyContext) -> list[str]:
        lines: list[str] = []
        p = ctx.persona
        if p.get("style"):
            lines.append(f"Speaking style: {p['style']}")
        traits = ctx.traits()
        if traits:
            from ..persona_traits import expand_traits

            lines.append(
                "Caller behavior traits (follow while staying natural): "
                + ", ".join(traits)
            )
            lines.extend(expand_traits(traits))
        return lines


class ConstraintsSection:
    """Hard constraints with if→then examples (Google SI step 4)."""

    def render(self, ctx: CallerPolicyContext) -> list[str]:
        constraints = ctx.constraints()
        if not constraints:
            return []
        lines = [
            "",
            "## HARD CONSTRAINTS (do not violate)",
            "These override being helpful or agreeable. If a constraint conflicts with the assistant's request, follow the constraint.",
        ]
        for c in constraints:
            lines.append(f"- {c}")
        lines.extend(
            [
                "",
                "Examples of correct behavior:",
                "- If the assistant asks for a payment card number and a constraint forbids it: refuse briefly and do not invent digits.",
                "- If the assistant asks you to restart a full menu and a constraint forbids it: refuse or ask for a supervisor; do not restart.",
                "- If you are tempted to agree just to finish the call: re-read HARD CONSTRAINTS first.",
            ]
        )
        return lines


class SpeechConditionsSection:
    def render(self, ctx: CallerPolicyContext) -> list[str]:
        sc = ctx.speech_conditions()
        if not sc:
            return []
        bits: list[str] = []
        if sc.get("barge_policy") or sc.get("interruption_rate"):
            bits.append(
                "timed interruptions may be injected by the simulator "
                f"(barge_policy={sc.get('barge_policy') or 'n/a'}, "
                f"interruption_rate={sc.get('interruption_rate') or 'n/a'})"
            )
        if sc.get("silence_ms") or sc.get("user_silence_ms") or sc.get("silent_mode"):
            bits.append(
                "you may be forced silent by the simulator "
                f"(silence_ms={sc.get('silence_ms') or sc.get('user_silence_ms') or 'n/a'}, "
                f"silent_mode={sc.get('silent_mode')})"
            )
        if sc.get("noise") or sc.get("ambient"):
            bits.append("there may be background noise on the line")
        if not bits:
            return []
        return ["Speech conditions (simulator-enforced where noted): " + "; ".join(bits) + "."]


class ContextSection:
    def render(self, ctx: CallerPolicyContext) -> list[str]:
        lines: list[str] = []
        notes = ctx.context.get("notes")
        if notes:
            lines.append(f"Background context you know: {notes}")
        fixtures = ctx.context.get("fixtures")
        if isinstance(fixtures, dict) and fixtures:
            # Opaque hints — core does not interpret business keys.
            pairs = ", ".join(f"{k}={v}" for k, v in list(fixtures.items())[:12])
            lines.append(
                "You may know these test fixture hints (use only if natural): " + pairs
            )
        return lines


class ScriptTimingSection:
    """When Script owns timing, free model must not freestyle barge or hang up."""

    def render(self, ctx: CallerPolicyContext) -> list[str]:
        if not ctx.script_steps:
            return []
        n = len(ctx.script_steps)
        return [
            "",
            "## INTERACTION TIMING (simulator-owned)",
            f"This call has {n} timed Script step(s). Timing and hang-up are owned by the simulator.",
            "Timed caller cues (barge, silence, hang-up, DTMF, PCM) are injected automatically.",
            "Do NOT freestyle barge-ins, invent long small-talk, or continue a full signup flow on your own.",
            "Between Script cues: if the assistant asks a direct question, answer in 1–2 natural phone sentences (okay / name / preference).",
            "When you receive a SIMULATOR CUE, speak that line aloud once immediately.",
            "Do NOT say goodbye, bye, thanks-bye, hang up, or [END_CALL] while Script steps remain.",
            "Only the final Script hang-up step ends the call. Freestyle farewell will FAIL the test.",
        ]


class FirstSpeakerSection:
    def render(self, ctx: CallerPolicyContext) -> list[str]:
        if ctx.script_steps:
            return [
                "Timed Script owns when you speak. Stay silent at connect "
                "until a SIMULATOR CUE instructs you to say a line aloud."
            ]
        if ctx.first_speaker == "agent":
            return [
                "Wait for the assistant to greet you first, then respond "
                "(unless a simulator cue tells you otherwise)."
            ]
        return [
            "You speak first: greet briefly and state why you are calling "
            "(one short turn)."
        ]


class GuardrailsSection:
    def render(self, ctx: CallerPolicyContext) -> list[str]:
        n = len(ctx.goals())
        has_script = bool(ctx.script_steps)
        lines = [
            "",
            "## GUARDRAILS",
            "Your job is to pursue your goals as the caller. You are not solving the assistant's job.",
            (
                "A timed Script will end the call — do not freestyle an ending."
                if has_script
                else "Only end the call when ALL goals are done (or unmistakably impossible after you tried)."
            ),
            "If you say goodbye or [END_CALL] early, the automated test will FAIL.",
            (
                "If the assistant asks a direct question between Script cues, answer in 1–2 natural sentences; "
                "do not start a long freestyle dialog or goodbye."
                if has_script
                else "If the assistant says something irrelevant, steer back to your current goal."
            ),
        ]
        if has_script:
            lines.extend(
                [
                    "A timed Script is active: do NOT freestyle a goodbye or barge outside Script cues.",
                    "Natural short answers to the assistant are OK; wait for the simulator hang-up cue to end the call.",
                ]
            )
        else:
            lines.extend(
                [
                    "When all goals are handled, say a short goodbye in your language only, "
                    "then append the exact harness marker [END_CALL] once and stop speaking.",
                ]
            )
        lines.extend(
            [
                'NEVER pronounce the English words "end call", "hang up", or "END CALL", '
                "and do not read brackets aloud — that leaks into the room recording. "
                "The marker is for the test harness transcript only.",
            ]
        )
        if n and not has_script:
            lines.append(
                f"You have {n} numbered goal(s). Ending before they are addressed is a failure."
            )
        return lines


def build_default_sections() -> list[PromptSection]:
    """Google Live order: persona → rules → guardrails (+ portable extras)."""
    return [
        RoleSection(),
        GoalsSection(),
        StyleTraitsSection(),
        ConstraintsSection(),
        SpeechConditionsSection(),
        ContextSection(),
        ScriptTimingSection(),
        FirstSpeakerSection(),
        GuardrailsSection(),
    ]
