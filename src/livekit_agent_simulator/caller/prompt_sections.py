"""Prompt section builders — Composite pieces of the Live system instruction.

Google Live best practice order: persona → conversational rules → guardrails.
Each section is a small Strategy; DefaultCallerPolicy composes them.

Modes:
- **Dialogue** (no Script): Persona situation/goals/outcome own speech.
- **Interaction / hybrid** (Script present): Script is an overlay (fixture or
  forced line); freestyle answers between cues are allowed.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .policy import CallerPolicyContext, Verbosity


@runtime_checkable
class PromptSection(Protocol):
    def render(self, ctx: CallerPolicyContext) -> list[str]:
        """Return zero or more lines (no trailing join)."""
        ...


# Free-text style phrases that fight natural/chatty length bands (not locale-parsed
# into verbosity — only neutralized when an explicit band is active).
_STYLE_LENGTH_CONFLICTS = (
    "short turns",
    "terse replies",
    "brief replies",
    "brief answers",
    "one-word answers",
    "one word answers",
    "keep it short",
    "keep replies short",
    "keep answers short",
)


def length_guidance(verbosity: Verbosity) -> str:
    """Shared utterance-length band for Role / Script / Guardrails / midcall."""
    if verbosity == "quiet":
        return (
            "Keep each utterance to about one short spoken clause "
            "(sparse phone speech; no padding)."
        )
    if verbosity == "chatty":
        return (
            "Speak like a real phone caller: often 3–6 spoken clauses when explaining "
            "or answering — give context (why you called, what went wrong, what you need), "
            "stay on-intent, and keep a conversational loop going. No monologues."
        )
    return (
        "Speak like a real phone caller in about 2–5 spoken clauses when it helps: "
        "answer what was asked, then add situational detail (why you need help, what "
        "already went wrong, what you hope happens next). Stay on-intent — not a monologue, "
        "and not one-word answers unless the assistant only needs a yes/no."
    )


def neutralize_style_length_hints(style: str, verbosity: Verbosity) -> tuple[str, bool]:
    """Strip known brevity phrases when verbosity is natural/chatty.

    Returns (cleaned_style, did_strip). Quiet keeps style verbatim.
    """
    raw = str(style or "").strip()
    if not raw or verbosity == "quiet":
        return raw, False
    cleaned = raw
    stripped = False
    lower = cleaned.lower()
    for phrase in _STYLE_LENGTH_CONFLICTS:
        idx = lower.find(phrase)
        while idx >= 0:
            stripped = True
            end = idx + len(phrase)
            cleaned = cleaned[:idx] + cleaned[end:]
            lower = cleaned.lower()
            idx = lower.find(phrase)
    # Tidy leftover separators from removals: "warm; ; everyday" / trailing ";"
    parts = [p.strip(" ,;") for p in cleaned.replace(";", ",").split(",")]
    cleaned = ", ".join(p for p in parts if p)
    return cleaned, stripped


def between_cues_answer_guidance(verbosity: Verbosity) -> str:
    """Between-Script-cue answer length (hybrid mode)."""
    if verbosity == "quiet":
        return (
            "Between Script cues: if the assistant asks a direct question, "
            "answer in one short spoken clause."
        )
    if verbosity == "chatty":
        return (
            "Between Script cues: you are a talkative phone caller — keep a conversational "
            "loop with the assistant. Answer every question in several spoken clauses "
            "(answer first, then add context or a follow-up), and keep talking until the "
            "next cue. Never go mute after one short telegram line. If the assistant asks "
            "whether you are still there, answer immediately as the caller."
        )
    return (
        "Between Script cues: keep a conversational loop with the assistant — "
        "answer in about 2–5 natural phone clauses (answer first, then context), "
        "and continue freestyle until the next cue. Do not go mute after one short line. "
        "If the assistant asks a question, answer it before waiting for the next cue."
    )


def _step_overlay(step: Any) -> str:
    """fixture | line — mirrors script.models.effective_overlay when available."""
    if isinstance(step, dict):
        raw = step.get("overlay")
        if raw in ("fixture", "line"):
            return str(raw)
        barge = bool(step.get("barge_in") or step.get("interrupt"))
        delivery = str(step.get("delivery") or "gemini_text")
        icls = str(step.get("class") or step.get("interrupt_class") or "").lower()
        action = str(step.get("action") or "speak")
        say = str(step.get("say") or step.get("text") or "").strip()
    else:
        raw = getattr(step, "overlay", None)
        if raw in ("fixture", "line"):
            return str(raw)
        barge = bool(getattr(step, "barge_in", False))
        delivery = str(getattr(step, "delivery", "gemini_text") or "gemini_text")
        icls = str(getattr(step, "interrupt_class", None) or "").lower()
        action = str(getattr(step, "action", "speak") or "speak")
        say = str(getattr(step, "say", "") or "").strip()
    if barge or delivery == "room_pcm" or icls in ("noise", "backchannel", "dtmf", "silence"):
        return "fixture"
    if action == "speak" and say:
        return "line"
    return "fixture"


class RoleSection:
    """Persona block (Google SI step 1)."""

    def render(self, ctx: CallerPolicyContext) -> list[str]:
        lang = ctx.locale
        verbosity = ctx.resolved_verbosity()
        lines = [
            "## PERSONA",
            "You are role-playing a HUMAN CALLER on a phone call with a voice assistant.",
            "You are NOT an assistant, agent, or support worker. Never offer help; you are the customer.",
            "UNMISTAKABLY never speak as the assistant: do not greet callers, do not claim their name "
            "or employer as yours, and do not ask how you can help them.",
            "If→then role lock: if you are tempted to say you will check inventory / take their details "
            "/ call them back as staff → stop and answer only as the customer who needs help.",
            "If→then: if the assistant's voice is still in your ears → that was THEM; your next words "
            "are still yours as the caller, never a continuation of their script.",
            f"RESPOND IN {lang}. YOU MUST RESPOND UNMISTAKABLY IN {lang}.",
            length_guidance(verbosity),
            "Never mention that you are an AI, a simulation, a test, or a judge.",
        ]
        p = ctx.persona
        if p.get("name"):
            lines.append(f"Your name: {p['name']}.")
        situation = p.get("situation") or p.get("brief")
        if situation:
            label = "Your situation" if p.get("situation") else "Who you are and why you are calling"
            lines.append(f"{label}: {situation}")
        if p.get("situation") and p.get("brief") and p.get("brief") != p.get("situation"):
            lines.append(f"Additional brief: {p['brief']}")
        outcome = p.get("outcome") or p.get("desired_outcome")
        if outcome:
            lines.append(
                f"Desired call outcome (what “done” looks like for you): {outcome}"
            )
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
                    "Rules when a Script overlay is present (hybrid / interaction):",
                    "1. You still pursue goals through natural answers when the assistant asks.",
                    "2. Forced Script lines are injected as SIMULATOR CUE — speak that line once "
                    "as a milestone, then continue freestyle until the next cue.",
                    "3. After each milestone, stay in a conversational loop: answer follow-ups, "
                    "clarify, push back, or add relevant detail — do not go quiet after one short reply.",
                    "4. Audio fixtures (barge WAV, noise, backchannel) are simulator-owned — do not invent barges.",
                    "5. Do NOT freestyle goodbye / [END_CALL]; Script hang-up ends the call.",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "Rules for goals (dialogue mode — you own speech):",
                    "1. Work through ALL goals one by one in a natural phone conversation.",
                    "2. Do NOT skip ahead to a later goal before the current one is addressed.",
                    "3. One-time steps (greet / identify / ask fee) then conversational loops (clarify, push back) are OK.",
                    "4. Do NOT say goodbye or [END_CALL] until ALL goals are addressed (or unmistakably impossible).",
                    "5. If the assistant cannot help with one goal, state that briefly and move to the next.",
                    "6. If the assistant goes off-topic, steer back to the current GOAL.",
                    "7. Do not people-please: follow HARD CONSTRAINTS even if that slows the call.",
                ]
            )
        return lines


class StyleTraitsSection:
    def render(self, ctx: CallerPolicyContext) -> list[str]:
        lines: list[str] = []
        p = ctx.persona
        verbosity = ctx.resolved_verbosity()
        if p.get("style"):
            cleaned, scrubbed = neutralize_style_length_hints(str(p["style"]), verbosity)
            if cleaned:
                lines.append(f"Speaking style: {cleaned}")
            if verbosity != "quiet" and (scrubbed or p.get("style")):
                lines.append(
                    "Utterance length follows speech_conditions.verbosity — "
                    "style brevity hints do not override it."
                )
        traits = ctx.traits()
        if traits:
            from ..persona_traits import expand_traits

            lines.append(
                "Caller behavior traits (follow while staying natural): "
                + ", ".join(traits)
            )
            lines.extend(expand_traits(traits))
        return lines


class NaturalSpeechSection:
    """Natural speech, re-engagement, anti-repetition — for natural and chatty bands."""

    def render(self, ctx: CallerPolicyContext) -> list[str]:
        if ctx.resolved_verbosity() == "quiet":
            return []
        verbosity = ctx.resolved_verbosity()
        lines = [
            "",
            "## DRIVING THE CONVERSATION",
            "You are a real human caller — not a passive questionnaire respondent.",
            "If the assistant takes longer than a few seconds to respond, you may:",
            "- Say 'Hello?' or 'Are you still there?' to re-engage after 5+ seconds of silence.",
            "- Repeat or rephrase your question if the assistant didn't seem to catch it.",
            "- Add more context unprompted: 'I'm just trying to figure out... because...'",
            "- Express impatience or confusion naturally: 'Sorry, did I lose you?'",
            "A real caller does NOT sit in dead silence waiting. If you hear nothing for several seconds, speak up.",
        ]
        if verbosity == "chatty":
            lines.append(
                "If the assistant is being slow, fill the gap with extra context or ask "
                "'Are you still looking that up?'"
            )
        lines.extend([
            "",
            "## NATURAL SPEECH",
            "Sound like a real caller: occasional brief hesitation sounds are OK "
            "(use standard spellings such as um, uh, or well — not elongated nonsense).",
            "Do not pad every turn with fillers; use them sparingly when thinking or softening a reply.",
            "Vary how you open turns — never start consecutive turns with the same stock opener "
            "(e.g. repeating \"Right. Um,\" / \"Yeah. Um,\" every time).",
            "Prefer fuller turns over telegram replies: answer, then add why / what happened / what you need.",
            "Examples of natural freestyle (dialogue, or between Script cues when asked): "
            '"I need to move Tuesday\'s appointment because of a work conflict, '
            'and I was hoping you could check what else is open this week." '
            'or "The order never showed up, so I\'m calling to track it and figure out '
            'if I should wait or reorder."',
            "Stay goal-bound; do not invent goodbye while Script steps remain.",
        ])
        lines.extend([
            "",
            "## ANTI-REPETITION",
            "IMPORTANT: Never repeat the same exact phrase or idea across consecutive turns.",
            "Real callers naturally advance the conversation — they do not echo themselves.",
            "- If you already said 'thanks for the info, I appreciate it' and the assistant "
            "keeps selling, do NOT say the same thing again.",
            "- Instead, either: re-engage with what the assistant said ('Yeah, but as I said "
            "I need to think about it first'), or escalate ('Look, I've given you my answer')",
            "- If you are ready to end the call, say a clear goodbye and stop.",
            "- A real person does not repeat themselves hoping the other person will listen — "
            "they either insist with fresh words or disengage.",
        ])
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
        if sc.get("barge_policy"):
            bits.append(
                "a timed interruption may be injected by the simulator "
                f"(barge_policy={sc.get('barge_policy')})"
            )
        rate = str(sc.get("interruption_rate") or sc.get("interrupt_rate") or "").strip().lower()
        if rate and rate not in ("none", "off", "0", "false"):
            bits.append(
                "the simulator will periodically cut you in while the assistant is "
                f"talking (interruption_rate={rate}); those cut-ins are simulator-owned "
                "audio — do not invent extra barge-ins yourself"
            )
        if sc.get("silent_mode") is True or str(sc.get("silent_mode") or "").lower() in (
            "1", "true", "yes", "on", "silent",
        ):
            bits.append(
                "SILENT MODE: you produce NO speech. Stay completely mute for the whole call. "
                "Do not greet, answer, or freestyle. The simulator enforces silence."
            )
        elif sc.get("silence_ms") or sc.get("user_silence_ms"):
            bits.append(
                "you may be forced silent by the simulator "
                f"(silence_ms={sc.get('silence_ms') or sc.get('user_silence_ms') or 'n/a'})"
            )
        if sc.get("noise") or sc.get("ambient"):
            bits.append("there may be background noise on the line")
        vg = sc.get("voice_gain", sc.get("voice_volume", sc.get("volume")))
        try:
            if vg is not None and float(vg) < 1.0:
                bits.append(
                    f"your mic level may be quiet (voice_gain={float(vg):.2f}; "
                    "simulator scales your speech audio)"
                )
        except (TypeError, ValueError):
            pass
        if not bits:
            return []
        return ["Speech conditions (simulator-enforced where noted): " + "; ".join(bits) + "."]


class ContextSection:
    def render(self, ctx: CallerPolicyContext) -> list[str]:
        """Caller world hints only — never inject author/harness ``notes`` into SI.

        ``Context.notes`` are for humans reading the JSONL / reports. Putting
        \"Dialogue mode — no Script\" into the model as \"Background context you
        know\" makes the caller act like a test harness instead of a person.
        """
        lines: list[str] = []
        # Prefer explicit caller-facing keys if present; ignore author notes.
        knows = ctx.context.get("caller_knows") or ctx.context.get("world")
        # Frame as external facts — naming the assistant in SI must not become identity.
        _prefix = (
            "Facts you already know (you remain the human caller; names/roles below "
            "are the OTHER party or situation, not you): "
        )
        if isinstance(knows, str) and knows.strip():
            lines.append(_prefix + knows.strip())
        elif isinstance(knows, list):
            bits = [str(x).strip() for x in knows if str(x).strip()]
            if bits:
                lines.append(_prefix + "; ".join(bits[:12]))
        fixtures = ctx.context.get("fixtures")
        if isinstance(fixtures, dict) and fixtures:
            # Opaque hints — core does not interpret business keys.
            pairs = ", ".join(f"{k}={v}" for k, v in list(fixtures.items())[:12])
            lines.append(
                "You may know these test fixture hints (use only if natural): " + pairs
            )
        return lines


class ScriptTimingSection:
    """Script is an interaction overlay — not the only mouth of the caller."""

    def render(self, ctx: CallerPolicyContext) -> list[str]:
        if not ctx.script_steps:
            return []
        n = len(ctx.script_steps)
        n_fix = sum(1 for s in ctx.script_steps if _step_overlay(s) == "fixture")
        n_line = sum(1 for s in ctx.script_steps if _step_overlay(s) == "line")
        verbosity = ctx.resolved_verbosity()
        lines = [
            "",
            "## SCRIPT OVERLAY (simulator-owned timing)",
            f"This call has {n} timed Script step(s) "
            f"({n_line} forced line(s), {n_fix} audio fixture(s)).",
            "Script is an OVERLAY on your persona dialogue — not a full script of the whole call.",
            "Forced lines are often injected by the simulator as audio (you may not get a text "
            "SIMULATOR CUE in Live). After each injected milestone, continue freestyle as the caller.",
            "Fixtures (barge WAV, noise, soft barge, DTMF): injected as audio — do not invent them.",
            between_cues_answer_guidance(verbosity),
            "If the assistant asks a question or checks whether you are still on the line, "
            "answer as the caller — do not wait for another simulator injection.",
            "If the assistant goes silent for several seconds: re-engage naturally. "
            "Do not just wait passively — say 'Are you still there?' or repeat yourself.",
            "Do NOT freestyle barge-ins or goodbye / [END_CALL] while Script steps remain.",
            "Only the final Script hang-up step ends the call. Freestyle farewell will FAIL the test.",
        ]
        return lines


class FirstSpeakerSection:
    def render(self, ctx: CallerPolicyContext) -> list[str]:
        sc = ctx.speech_conditions()
        silent = sc.get("silent_mode") is True or str(sc.get("silent_mode") or "").lower() in (
            "1", "true", "yes", "on", "silent",
        )
        if silent:
            return [
                "Silent mode: produce zero speech for the entire call. "
                "Do not open, greet, or answer — stay mute.",
            ]
        if ctx.script_steps and ctx.first_speaker == "agent":
            return [
                "Opening: the assistant will greet you first. Stay silent after their greeting. "
                "The simulator will inject your first line as a timed cue. "
                "Wait for that cue before speaking — do not respond to the assistant's greeting yourself. "
                "After that first injected line, you may talk freely again until the next cue.",
            ]
        if ctx.script_steps:
            return [
                "Opening: stay silent at connect. The simulator injects your opening line "
                "as audio. After that opening (and after each later injected milestone), "
                "answer the assistant freely in freestyle until the next injection — "
                "do not stay mute waiting for a text cue.",
            ]
        if ctx.first_speaker == "agent":
            return [
                "Wait for the assistant to greet you first, then respond "
                "(unless a simulator cue tells you otherwise).",
            ]
        verbosity = ctx.resolved_verbosity()
        if verbosity == "quiet":
            open_hint = "greet briefly and state why you are calling (one short clause)"
        elif verbosity == "chatty":
            open_hint = (
                "greet and state why you are calling "
                "(a natural opening turn; stay goal-bound)"
            )
        else:
            open_hint = (
                "greet briefly and state why you are calling "
                "(one natural opening turn)"
            )
        return [
            f"You speak first: after the call connects, {open_hint}. "
            "Do this from persona — no separate cue.",
        ]


class GuardrailsSection:
    def render(self, ctx: CallerPolicyContext) -> list[str]:
        n = len(ctx.goals())
        has_script = bool(ctx.script_steps)
        verbosity = ctx.resolved_verbosity()
        if has_script:
            if verbosity == "quiet":
                between = (
                    "If the assistant asks a direct question between Script cues, "
                    "answer in one short spoken clause; "
                    "do not start a long freestyle monologue or goodbye."
                )
            elif verbosity == "chatty":
                between = (
                    "If the assistant asks between Script cues, stay talkative: answer in "
                    "several natural clauses with relevant context, keep the loop going, "
                    "and never go mute after one short line or freestyle a goodbye."
                )
            else:
                between = (
                    "If the assistant asks between Script cues, answer in about 2–5 natural clauses "
                    "and keep the loop going until the next cue; "
                    "do not go mute after one short line or freestyle a goodbye."
                )
        else:
            between = "If the assistant says something irrelevant, steer back to your current goal."
        lines = [
            "",
            "## GUARDRAILS",
            "Your job is to pursue your goals as the caller. You are not solving the assistant's job.",
            "Never switch roles mid-call: if you catch yourself sounding like staff "
            "(offering help, checking things for the caller, introducing yourself as the agent) "
            "→ stop and resume as the customer.",
            (
                "A timed Script hang-up will end the call — do not freestyle an ending."
                if has_script
                else "Only end the call when ALL goals are done (or unmistakably impossible after you tried)."
            ),
            "If you say goodbye or [END_CALL] early, the automated test will FAIL.",
            between,
            (
                "The assistant may pause for several seconds. That is NOT a cue to end the call. "
                "Instead, re-engage: say 'Hello?' or repeat your question. "
                "A real caller does not hang up after every pause."
            ),
        ]
        if has_script:
            lines.extend(
                [
                    "Script overlay active: do NOT freestyle a goodbye or barge outside Script cues.",
                    "Natural answers to the assistant are OK; wait for the simulator hang-up cue to end the call.",
                ]
            )
        else:
            lines.extend(
                [
                    "When your desired outcome is met (or unmistakably impossible after you tried), "
                    "say ONE short goodbye in your language and stop speaking. "
                    "A clear bye/goodbye ends the call — do not linger in thank-you loops. "
                    "Optionally append [END_CALL] once for the harness (do not read brackets aloud).",
                    "",
                    "IMPORTANT: If you already said a clear goodbye or signal that you are done, "
                    "and the assistant ignores it and keeps talking:",
                    "- Do NOT repeat your goodbye phrase. Do NOT say the same thing again.",
                    "- A real caller either stays silent, or says something firm exactly once: "
                    "'Look, I've given you my answer. Thanks. Bye.'",
                    "- If you say 'I'll think about it, thanks' and the agent still pushes, "
                    "you may say more firmly: 'I said I'll think about it. I'll call if I'm keen. Bye.'",
                    "- Never repeat yourself across consecutive turns with the same thought.",
                    "- After a clear goodbye, you are done. Stay silent and wait for the agent to hang up.",
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
        NaturalSpeechSection(),
        ConstraintsSection(),
        SpeechConditionsSection(),
        ContextSection(),
        ScriptTimingSection(),
        FirstSpeakerSection(),
        GuardrailsSection(),
    ]
