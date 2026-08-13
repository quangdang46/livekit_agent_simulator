//! Prompt section builders — Composite pieces of the Live system instruction
//! (port of `caller/prompt_sections.py`). Each section renders zero+ lines from a
//! CallerPolicyContext; DefaultCallerPolicy composes them in Google Live order.

use serde_json::Value as Json;

use crate::caller_policy::{neutralize_style_length_hints, CallerPolicyContext, Verbosity};
use crate::persona_traits::expand_traits;

fn as_str(v: &Json) -> String {
    match v {
        Json::String(s) => s.clone(),
        Json::Number(n) => n.to_string(),
        Json::Bool(b) => b.to_string(),
        Json::Null => String::new(),
        other => other.to_string(),
    }
}

/// `_step_overlay`: fixture | line — mirrors script.models.effective_overlay.
fn step_overlay(step: &Json) -> &'static str {
    if let Some(o) = step.get("overlay").and_then(|v| v.as_str()) {
        if o == "fixture" || o == "line" {
            return if o == "line" { "line" } else { "fixture" };
        }
    }
    let barge = step
        .get("barge_in")
        .or_else(|| step.get("interrupt"))
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let delivery = step
        .get("delivery")
        .map(as_str)
        .unwrap_or_else(|| "gemini_text".to_string());
    let icls = step
        .get("class")
        .or_else(|| step.get("interrupt_class"))
        .map(as_str)
        .unwrap_or_default()
        .to_lowercase();
    let action = step
        .get("action")
        .map(as_str)
        .unwrap_or_else(|| "speak".to_string());
    let say = step
        .get("say")
        .or_else(|| step.get("text"))
        .map(as_str)
        .unwrap_or_default();
    let say = say.trim();
    if barge
        || delivery == "room_pcm"
        || matches!(icls.as_str(), "noise" | "backchannel" | "dtmf" | "silence")
    {
        "fixture"
    } else if action == "speak" && !say.is_empty() {
        "line"
    } else {
        "fixture"
    }
}

/// Persona block (Google SI step 1).
pub fn role_section(ctx: &CallerPolicyContext) -> Vec<String> {
    let lang = ctx.locale.clone();
    let verbosity = ctx.resolved_verbosity();
    let mut lines = vec![
        "## PERSONA".to_string(),
        "You are role-playing a HUMAN CALLER on a phone call with a voice assistant.".to_string(),
        "You are NOT an assistant, agent, or support worker. Never offer help; you are the customer."
            .to_string(),
        "UNMISTAKABLY never speak as the assistant: do not greet callers, do not claim their name \
         or employer as yours, and do not ask how you can help them."
            .to_string(),
        "If→then role lock: if you are tempted to say you will check inventory / take their details \
         / call them back as staff → stop and answer only as the customer who needs help."
            .to_string(),
        "If→then: if the assistant's voice is still in your ears → that was THEM; your next words \
         are still yours as the caller, never a continuation of their script."
            .to_string(),
        "IMPORTANT — MEMORY: If you already said something or asked a question, and the assistant \
         answered (even if in many words), you do NOT ask the same question again. Listen, \
         acknowledge, and move to your next topic. Repeating yourself is the #1 sign of a bad caller."
            .to_string(),
        format!("RESPOND IN {lang}. YOU MUST RESPOND UNMISTAKABLY IN {lang}."),
        crate::caller_policy::length_guidance(verbosity),
        "Never mention that you are an AI, a simulation, a test, or a judge.".to_string(),
    ];
    let p = &ctx.persona;
    if let Some(name) = p.get("name").map(as_str) {
        lines.push(format!("Your name: {name}."));
    }
    let situation = p
        .get("situation")
        .map(as_str)
        .or_else(|| p.get("brief").map(as_str));
    if let Some(sit) = situation {
        let label = if p.contains_key("situation") {
            "Your situation"
        } else {
            "Who you are and why you are calling"
        };
        lines.push(format!("{label}: {sit}"));
    }
    if p.contains_key("situation") {
        if let Some(brief) = p.get("brief").map(as_str) {
            let sit = p.get("situation").map(as_str).unwrap_or_default();
            if brief != sit {
                lines.push(format!("Additional brief: {brief}"));
            }
        }
    }
    let outcome = p
        .get("outcome")
        .map(as_str)
        .or_else(|| p.get("desired_outcome").map(as_str));
    if let Some(out) = outcome {
        lines.push(format!(
            "Desired call outcome (what “done” looks like for you): {out}"
        ));
    }
    lines
}

/// Ordered goals = conversational rules (Google SI step 2).
pub fn goals_section(ctx: &CallerPolicyContext) -> Vec<String> {
    let goals = ctx.goals();
    if goals.is_empty() {
        return Vec::new();
    }
    let mut lines = vec![
        "".to_string(),
        "## CONVERSATIONAL RULES — YOUR GOALS".to_string(),
        "Complete each goal before moving to the next. Treat this as a checklist.".to_string(),
    ];
    for (i, g) in goals.iter().enumerate() {
        lines.push(format!("GOAL {}: {}", i + 1, g));
    }
    if !ctx.script_steps.is_empty() {
        lines.extend(
            [
                "",
                "Rules when a Script overlay is present (hybrid / interaction):",
                "1. You still pursue goals through natural answers when the assistant asks.",
                "2. Forced Script lines are injected as SIMULATOR CUE — speak that line once \
                 as a milestone, then continue freestyle until the next cue.",
                "3. After each milestone, stay in a conversational loop: answer follow-ups, \
                 clarify, push back, or add relevant detail — do not go quiet after one short reply.",
                "4. Audio fixtures (barge WAV, noise, backchannel) are simulator-owned — do not invent barges.",
                "5. Do NOT freestyle goodbye / [END_CALL]; Script hang-up ends the call.",
            ]
            .map(String::from),
        );
    } else {
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
            .map(String::from),
        );
    }
    lines
}

/// Style traits.
pub fn style_traits_section(ctx: &CallerPolicyContext) -> Vec<String> {
    let mut lines: Vec<String> = Vec::new();
    let p = &ctx.persona;
    let verbosity = ctx.resolved_verbosity();
    if let Some(style) = p.get("style").map(as_str) {
        let (cleaned, scrubbed) = neutralize_style_length_hints(&style, verbosity);
        if !cleaned.is_empty() {
            lines.push(format!("Speaking style: {cleaned}"));
        }
        if verbosity != Verbosity::Quiet && (scrubbed || !style.is_empty()) {
            lines.push(
                "Utterance length follows speech_conditions.verbosity — \
                 style brevity hints do not override it."
                    .to_string(),
            );
        }
    }
    let traits = ctx.traits();
    if !traits.is_empty() {
        lines.push(format!(
            "Caller behavior traits (follow while staying natural): {}",
            traits.join(", ")
        ));
        let trait_json: Json = traits.iter().map(|t| Json::String(t.clone())).collect();
        lines.extend(expand_traits(&trait_json));
    }
    lines
}

/// Hard constraints (Google SI step 4).
pub fn constraints_section(ctx: &CallerPolicyContext) -> Vec<String> {
    let constraints = ctx.constraints();
    if constraints.is_empty() {
        return Vec::new();
    }
    let mut lines = vec![
        "".to_string(),
        "## HARD CONSTRAINTS (do not violate)".to_string(),
        "These override being helpful or agreeable. If a constraint conflicts with the assistant's request, follow the constraint."
            .to_string(),
    ];
    for c in &constraints {
        lines.push(format!("- {c}"));
    }
    lines.extend(
        [
            "",
            "Examples of correct behavior:",
            "- If the assistant asks for a payment card number and a constraint forbids it: refuse briefly and do not invent digits.",
            "- If the assistant asks you to restart a full menu and a constraint forbids it: refuse or ask for a supervisor; do not restart.",
            "- If you are tempted to agree just to finish the call: re-read HARD CONSTRAINTS first.",
        ]
        .map(String::from),
    );
    lines
}

/// Speech conditions (simulator-enforced).
pub fn speech_conditions_section(ctx: &CallerPolicyContext) -> Vec<String> {
    let sc = ctx.speech_conditions();
    if sc.is_empty() {
        return Vec::new();
    }
    let mut bits: Vec<String> = Vec::new();
    if sc
        .get("barge_policy")
        .is_some_and(|v| !as_str(v).is_empty())
    {
        bits.push(format!(
            "a timed interruption may be injected by the simulator (barge_policy={})",
            as_str(sc.get("barge_policy").unwrap())
        ));
    }
    let rate = as_str(
        sc.get("interruption_rate")
            .or_else(|| sc.get("interrupt_rate"))
            .unwrap_or(&Json::String("".into())),
    )
    .trim()
    .to_lowercase();
    if !rate.is_empty() && !matches!(rate.as_str(), "none" | "off" | "0" | "false") {
        bits.push(format!(
            "the simulator will periodically cut you in while the assistant is talking \
             (interruption_rate={rate}); those cut-ins are simulator-owned audio — do not \
             invent extra barge-ins yourself"
        ));
    }
    let silent = sc.get("silent_mode").and_then(|v| v.as_bool()) == Some(true)
        || matches!(
            sc.get("silent_mode")
                .map(as_str)
                .unwrap_or_default()
                .to_lowercase()
                .as_str(),
            "1" | "true" | "yes" | "on" | "silent"
        );
    if silent {
        bits.push(
            "SILENT MODE: you produce NO speech. Stay completely mute for the whole call. \
             Do not greet, answer, or freestyle. The simulator enforces silence."
                .to_string(),
        );
    } else {
        let sil = sc
            .get("silence_ms")
            .or_else(|| sc.get("user_silence_ms"))
            .map(as_str)
            .unwrap_or_default();
        if !sil.is_empty() {
            bits.push(format!(
                "you may be forced silent by the simulator (silence_ms={sil})"
            ));
        }
    }
    if sc.get("noise").is_some_and(|v| !as_str(v).is_empty())
        || sc.get("ambient").is_some_and(|v| !as_str(v).is_empty())
    {
        bits.push("there may be background noise on the line".to_string());
    }
    let vg = sc
        .get("voice_gain")
        .or_else(|| sc.get("voice_volume"))
        .or_else(|| sc.get("volume"));
    if let Some(vg) = vg {
        if let Some(f) = vg.as_f64() {
            if f < 1.0 {
                bits.push(format!(
                    "your mic level may be quiet (voice_gain={f:.2}; simulator scales your speech audio)"
                ));
            }
        }
    }
    if bits.is_empty() {
        return Vec::new();
    }
    vec![format!(
        "Speech conditions (simulator-enforced where noted): {}.",
        bits.join("; ")
    )]
}

/// First speaker opening.
pub fn first_speaker_section(ctx: &CallerPolicyContext) -> Vec<String> {
    let sc = ctx.speech_conditions();
    let silent = sc.get("silent_mode").and_then(|v| v.as_bool()) == Some(true)
        || matches!(
            sc.get("silent_mode")
                .map(as_str)
                .unwrap_or_default()
                .to_lowercase()
                .as_str(),
            "1" | "true" | "yes" | "on" | "silent"
        );
    if silent {
        return vec!["Silent mode: produce zero speech for the entire call. \
             Do not open, greet, or answer — stay mute."
            .to_string()];
    }
    if !ctx.script_steps.is_empty() && ctx.first_speaker == "agent" {
        return vec![
            "Opening: the assistant will greet you first. Stay silent after their greeting. \
             The simulator will inject your first line as a timed cue. \
             Wait for that cue before speaking — do not respond to the assistant's greeting yourself. \
             After that first injected line, you may talk freely again until the next cue."
                .to_string(),
        ];
    }
    if !ctx.script_steps.is_empty() {
        return vec![
            "Opening: stay silent at connect. The simulator injects your opening line \
             as audio. After that opening (and after each later injected milestone), \
             answer the assistant freely in freestyle until the next injection — \
             do not stay mute waiting for a text cue."
                .to_string(),
        ];
    }
    if ctx.first_speaker == "agent" {
        return vec!["Wait for the assistant to greet you first, then respond \
             (unless a simulator cue tells you otherwise)."
            .to_string()];
    }
    let open_hint = match ctx.resolved_verbosity() {
        Verbosity::Quiet => "greet briefly and state why you are calling (one short clause)",
        Verbosity::Chatty => {
            "greet and state why you are calling (a natural opening turn; stay goal-bound)"
        }
        Verbosity::Natural => {
            "greet briefly and state why you are calling (one natural opening turn)"
        }
    };
    vec![format!(
        "You speak first: after the call connects, {open_hint}. \
         Do this from persona — no separate cue."
    )]
}

/// ContextSection: caller world hints only, never author notes.
pub fn context_section(ctx: &CallerPolicyContext) -> Vec<String> {
    let mut lines: Vec<String> = Vec::new();
    let knows = ctx
        .context
        .get("caller_knows")
        .or_else(|| ctx.context.get("world"));
    let prefix = "Facts you already know (you remain the human caller; names/roles below \
                   are the OTHER party or situation, not you): ";
    if let Some(k) = knows {
        match k {
            Json::String(s) if !s.trim().is_empty() => lines.push(format!("{prefix}{}", s.trim())),
            Json::Array(a) => {
                let bits: Vec<String> = a
                    .iter()
                    .map(|x| x.as_str().unwrap_or(&x.to_string()).to_string())
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
                    .take(12)
                    .collect();
                if !bits.is_empty() {
                    lines.push(format!("{prefix}{}", bits.join("; ")));
                }
            }
            _ => {}
        }
    }
    if let Some(fixtures) = ctx.context.get("fixtures").and_then(|v| v.as_object()) {
        if !fixtures.is_empty() {
            let pairs: Vec<String> = fixtures
                .iter()
                .take(12)
                .map(|(k, v)| format!("{k}={}", as_str(v)))
                .collect();
            lines.push(format!(
                "You may know these test fixture hints (use only if natural): {}",
                pairs.join(", ")
            ));
        }
    }
    lines
}

/// ScriptTimingSection — script is an interaction overlay.
pub fn script_timing_section(ctx: &CallerPolicyContext) -> Vec<String> {
    if ctx.script_steps.is_empty() {
        return Vec::new();
    }
    let n = ctx.script_steps.len();
    let n_fix = ctx
        .script_steps
        .iter()
        .filter(|s| step_overlay(s) == "fixture")
        .count();
    let n_line = ctx
        .script_steps
        .iter()
        .filter(|s| step_overlay(s) == "line")
        .count();
    let verbosity = ctx.resolved_verbosity();
    vec![
        "".to_string(),
        "## SCRIPT OVERLAY (simulator-owned timing)".to_string(),
        format!(
            "This call has {n} timed Script step(s) ({n_line} forced line(s), {n_fix} audio fixture(s))."
        ),
        "Script is an OVERLAY on your persona dialogue — not a full script of the whole call."
            .to_string(),
        "Forced lines are often injected by the simulator as audio (you may not get a text \
         SIMULATOR CUE in Live). After each injected milestone, continue freestyle as the caller."
            .to_string(),
        "Fixtures (barge WAV, noise, soft barge, DTMF): injected as audio — do not invent them."
            .to_string(),
        crate::caller_policy::between_cues_answer_guidance(verbosity),
        "If the assistant asks a question or checks whether you are still on the line, \
         answer as the caller — do not wait for another simulator injection."
            .to_string(),
        "If the assistant goes silent for several seconds: re-engage naturally. \
         Do not just wait passively — say 'Are you still there?' or repeat yourself."
            .to_string(),
        "Do NOT freestyle barge-ins or goodbye / [END_CALL] while Script steps remain."
            .to_string(),
        "Only the final Script hang-up step ends the call. Freestyle farewell will FAIL the test."
            .to_string(),
    ]
}

/// NaturalSpeechSection — anti-repetition + driving conversation (natural/chatty).
pub fn natural_speech_section(ctx: &CallerPolicyContext) -> Vec<String> {
    if ctx.resolved_verbosity() == Verbosity::Quiet {
        return Vec::new();
    }
    let verbosity = ctx.resolved_verbosity();
    let mut lines = vec![
        "".to_string(),
        "## CRITICAL: NEVER REPEAT YOURSELF".to_string(),
        "If you just asked a question and the assistant answered it, do NOT ask the same question again.".to_string(),
        "If the assistant gave a long answer: listen, acknowledge it briefly, and move to your next point.".to_string(),
        "Repeating the exact same question or statement across turns sounds like a recording, not a person.".to_string(),
        "If you are unsure what the assistant said, ask a CLARIFYING question — never echo your own prior turn.".to_string(),
        "".to_string(),
        "## DRIVING THE CONVERSATION".to_string(),
        "You are a real human caller — not a passive questionnaire respondent.".to_string(),
        "If the assistant takes longer than a few seconds to respond, you may:".to_string(),
        "- Say 'Hello?' or 'Are you still there?' to re-engage after 5+ seconds of silence.".to_string(),
        "- Repeat or rephrase your question if the assistant didn't seem to catch it.".to_string(),
        "- Add more context unprompted: 'I'm just trying to figure out... because...'".to_string(),
        "- Express impatience or confusion naturally: 'Sorry, did I lose you?'".to_string(),
        "A real caller does NOT sit in dead silence waiting. If you hear nothing for several seconds, speak up.".to_string(),
    ];
    if verbosity == Verbosity::Chatty {
        lines.push(
            "If the assistant is being slow, fill the gap with extra context or ask \
             'Are you still looking that up?'"
                .to_string(),
        );
    }
    lines.extend([
        "",
        "## NATURAL SPEECH",
        "You are on a phone call, NOT writing an email or chat message. Real callers speak the \
         way people actually talk: with filler words, restarts, soft pauses, and sentences that \
         meander a little.",
        "Speech patterns that make you sound human:",
        "- Start turns with natural openers and vary them; never repeat the same opener twice in a row.",
        "- Use occasional filler where a real person would — sparingly, when thinking or softening.",
        "- Restart mid-sentence like people do.",
        "- Use contractions always: \"I'm,\" \"that's,\" \"don't,\" \"can't\" — never \"I am going to\" in spoken turns.",
        "- Ask real follow-up questions instead of just answering.",
        "The ONE rule: sound like an ordinary person on the phone, not a script.",
        "Stay goal-bound; do not invent goodbye while Script steps remain.",
    ]
    .map(String::from));
    lines.extend([
        "",
        "## ANTI-REPETITION",
        "IMPORTANT: Never repeat the same exact phrase or idea across consecutive turns.",
        "Real callers naturally advance the conversation — they do not echo themselves.",
        "- If the assistant keeps selling after you declined, re-engage or escalate with fresh words.",
        "- If you are ready to end the call, say a clear goodbye and stop.",
        "- A real person does not repeat themselves hoping the other person will listen.",
    ]
    .map(String::from));
    lines
}

/// GuardrailsSection — caller guardrails + persona-prompt extension point.
pub fn guardrails_section(ctx: &CallerPolicyContext) -> Vec<String> {
    let n = ctx.goals().len();
    let has_script = !ctx.script_steps.is_empty();
    let verbosity = ctx.resolved_verbosity();
    let between = if has_script {
        match verbosity {
            Verbosity::Quiet => "If the assistant asks a direct question between Script cues, answer in one short spoken clause; do not start a long freestyle monologue or goodbye.".to_string(),
            Verbosity::Chatty => "If the assistant asks between Script cues, stay talkative: answer in several natural clauses with relevant context, keep the loop going, and never go mute after one short line or freestyle a goodbye.".to_string(),
            Verbosity::Natural => "If the assistant asks between Script cues, answer in about 2–5 natural clauses and keep the loop going until the next cue; do not go mute after one short line or freestyle a goodbye.".to_string(),
        }
    } else {
        "If the assistant says something irrelevant, steer back to your current goal.".to_string()
    };
    let mut lines = vec![
        "".to_string(),
        "## GUARDRAILS".to_string(),
        "Your job is to pursue your goals as the caller. You are not solving the assistant's job.".to_string(),
        "Never switch roles mid-call: if you catch yourself sounding like staff → stop and resume as the customer.".to_string(),
        if has_script {
            "A timed Script hang-up will end the call — do not freestyle an ending.".to_string()
        } else {
            "Only end the call when ALL goals are done (or unmistakably impossible after you tried).".to_string()
        },
        "If you say goodbye or [END_CALL] early, the automated test will FAIL.".to_string(),
        between,
        "The assistant may pause for several seconds. That is NOT a cue to end the call. Instead, re-engage: say 'Hello?' or repeat your question.".to_string(),
    ];
    if has_script {
        lines.extend([
            "Script overlay active: do NOT freestyle a goodbye or barge outside Script cues.".to_string(),
            "Natural answers to the assistant are OK; wait for the simulator hang-up cue to end the call.".to_string(),
        ]);
    } else {
        lines.extend([
            "When your desired outcome is met (or unmistakably impossible after you tried), say ONE short goodbye in your language and stop speaking.".to_string(),
            "NEVER pronounce the English words \"end call\", \"hang up\", or \"END CALL\", and do not read brackets aloud.".to_string(),
        ]);
    }
    if n > 0 && !has_script {
        lines.push(format!(
            "You have {n} numbered goal(s). Ending before they are addressed is a failure."
        ));
    }
    lines
}

/// Compose all sections in Google Live order (persona → rules → guardrails).
pub fn all_sections(ctx: &CallerPolicyContext) -> Vec<Vec<String>> {
    vec![
        role_section(ctx),
        goals_section(ctx),
        style_traits_section(ctx),
        natural_speech_section(ctx),
        constraints_section(ctx),
        speech_conditions_section(ctx),
        context_section(ctx),
        script_timing_section(ctx),
        first_speaker_section(ctx),
        guardrails_section(ctx),
    ]
}
