//! Caller dialog policy — CallerPolicyContext + verbosity resolution (port of
//! `caller/policy.py`). Owns dialog text only; Script timing stays elsewhere.

use serde_json::{Map, Value as Json};

/// Verbosity length band.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verbosity {
    Quiet,
    Natural,
    Chatty,
}

impl Verbosity {
    pub fn as_str(&self) -> &'static str {
        match self {
            Verbosity::Quiet => "quiet",
            Verbosity::Natural => "natural",
            Verbosity::Chatty => "chatty",
        }
    }
}

const VERBOSITY_VALUES: [&str; 3] = ["quiet", "natural", "chatty"];
const QUIET_TRAITS: [&str; 3] = ["quiet", "silent", "terse"];

fn as_str(v: &Json) -> String {
    match v {
        Json::String(s) => s.clone(),
        Json::Number(n) => n.to_string(),
        Json::Bool(b) => b.to_string(),
        Json::Null => String::new(),
        other => other.to_string(),
    }
}

fn str_list(v: &Json) -> Vec<String> {
    match v {
        Json::String(s) => vec![s.clone()],
        Json::Array(a) => a
            .iter()
            .map(|x| match x {
                Json::String(s) => s.clone(),
                other => other.to_string(),
            })
            .collect(),
        _ => Vec::new(),
    }
}

/// Immutable-enough bag for policy builders (no I/O).
#[derive(Debug, Clone)]
pub struct CallerPolicyContext {
    pub persona: Map<String, Json>,
    pub locale: String,
    pub context: Map<String, Json>,
    pub script_steps: Vec<Json>,
    pub first_speaker: String,
}

impl CallerPolicyContext {
    pub fn goals(&self) -> Vec<String> {
        str_list(self.persona.get("goals").unwrap_or(&Json::Null))
            .into_iter()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect()
    }

    pub fn constraints(&self) -> Vec<String> {
        str_list(self.persona.get("constraints").unwrap_or(&Json::Null))
            .into_iter()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect()
    }

    pub fn traits(&self) -> Vec<String> {
        let raw = self
            .persona
            .get("traits")
            .or_else(|| self.persona.get("behaviors"))
            .unwrap_or(&Json::Null);
        str_list(raw)
            .into_iter()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect()
    }

    pub fn speech_conditions(&self) -> &Map<String, Json> {
        static EMPTY: std::sync::OnceLock<Map<String, Json>> = std::sync::OnceLock::new();
        self.persona
            .get("speech_conditions")
            .or_else(|| self.persona.get("speechConditions"))
            .and_then(|v| v.as_object())
            .unwrap_or_else(|| EMPTY.get_or_init(Map::new))
    }

    /// Resolve caller length band: speech_conditions.verbosity, then traits, else natural.
    pub fn resolved_verbosity(&self) -> Verbosity {
        let sc = self.speech_conditions();
        let raw = sc.get("verbosity");
        if let Some(raw) = raw {
            let s = as_str(raw).trim().to_string();
            if !s.is_empty() {
                let key = s.to_lowercase().replace('-', "_");
                if VERBOSITY_VALUES.contains(&key.as_str()) {
                    return match key.as_str() {
                        "quiet" => Verbosity::Quiet,
                        "chatty" => Verbosity::Chatty,
                        _ => Verbosity::Natural,
                    };
                }
                // unknown verbosity → fall through to natural (Python debug-logs)
            }
        }
        let trait_keys: std::collections::HashSet<String> = self
            .traits()
            .into_iter()
            .map(|t| t.to_lowercase().replace([' ', '-'], "_"))
            .collect();
        if trait_keys.contains("chatty") {
            return Verbosity::Chatty;
        }
        if trait_keys
            .iter()
            .any(|k| QUIET_TRAITS.contains(&k.as_str()))
        {
            return Verbosity::Quiet;
        }
        Verbosity::Natural
    }
}

/// Shared utterance-length band for Role / Script / Guardrails / midcall.
pub fn length_guidance(verbosity: Verbosity) -> String {
    match verbosity {
        Verbosity::Quiet => "Keep each utterance to about one short spoken clause \
             (sparse phone speech; no padding)."
            .to_string(),
        Verbosity::Chatty => {
            "Speak like a real phone caller: often 3–6 spoken clauses when explaining \
             or answering — give context (why you called, what went wrong, what you need), \
             stay on-intent, and keep a conversational loop going. No monologues."
                .to_string()
        }
        Verbosity::Natural => {
            "Speak the length a real person would on the phone: answer what was asked, then \
             naturally add the detail that comes to mind (why you need help, what already went \
             wrong, what you hope happens next). Let the conversation breathe — a short answer \
             when the moment is short, a fuller one when you have something to say. Never a \
             monologue, and never a robotic telegram unless the assistant only needs a yes/no."
                .to_string()
        }
    }
}

/// Between-Script-cue answer length (hybrid mode).
pub fn between_cues_answer_guidance(verbosity: Verbosity) -> String {
    match verbosity {
        Verbosity::Quiet => "Between Script cues: if the assistant asks a direct question, \
             answer in one short spoken clause."
            .to_string(),
        Verbosity::Chatty => {
            "Between Script cues: you are a talkative phone caller — keep a conversational \
             loop with the assistant. Answer every question in several spoken clauses \
             (answer first, then add context or a follow-up), and keep talking until the \
             next cue. Never go mute after one short telegram line. If the assistant asks \
             whether you are still there, answer immediately as the caller."
                .to_string()
        }
        Verbosity::Natural => {
            "Between Script cues: keep a conversational loop with the assistant — \
             answer in about 2–5 natural phone clauses (answer first, then context), \
             and continue freestyle until the next cue. Do not go mute after one short line. \
             If the assistant asks a question, answer it before waiting for the next cue."
                .to_string()
        }
    }
}

const STYLE_LENGTH_CONFLICTS: [&str; 9] = [
    "short turns",
    "terse replies",
    "brief replies",
    "brief answers",
    "one-word answers",
    "one word answers",
    "keep it short",
    "keep replies short",
    "keep answers short",
];

/// Strip known brevity phrases when verbosity is natural/chatty.
/// Returns (cleaned_style, did_strip). Quiet keeps style verbatim.
pub fn neutralize_style_length_hints(style: &str, verbosity: Verbosity) -> (String, bool) {
    let raw = style.trim().to_string();
    if raw.is_empty() || verbosity == Verbosity::Quiet {
        return (raw, false);
    }
    let mut cleaned = raw.clone();
    let mut stripped = false;
    let mut lower = cleaned.to_lowercase();
    for phrase in STYLE_LENGTH_CONFLICTS {
        let mut idx = lower.find(phrase);
        while let Some(i) = idx {
            stripped = true;
            let end = i + phrase.len();
            cleaned = format!("{}{}", &cleaned[..i], &cleaned[end..]);
            lower = cleaned.to_lowercase();
            idx = lower.find(phrase);
        }
    }
    // Tidy leftover separators: "warm; ; everyday" / trailing ";"
    let parts: Vec<String> = cleaned
        .replace(';', ",")
        .split(',')
        .map(|p| {
            p.trim_matches(' ')
                .trim_matches(',')
                .trim_matches(';')
                .to_string()
        })
        .filter(|p| !p.is_empty())
        .collect();
    (parts.join(", "), stripped)
}

/// Midcall text cue (bootstrap | reground | custom).
#[derive(Debug, Clone, PartialEq)]
pub struct MidcallCue {
    pub text: String,
    pub kind: String,
    pub label: String,
}

/// DefaultCallerPolicy — composes prompt sections + on-demand midcall cues.
pub struct DefaultCallerPolicy {
    pub policy_source: String,
}

impl DefaultCallerPolicy {
    pub fn new() -> Self {
        Self {
            policy_source: "builtin".into(),
        }
    }

    /// Full system instruction: join all section lines with "\n" (no trailing).
    pub fn build_system_instruction(&self, ctx: &CallerPolicyContext) -> String {
        let mut lines: Vec<String> = Vec::new();
        for part in crate::prompt_sections::all_sections(ctx) {
            for line in part {
                lines.push(line);
            }
        }
        lines.join("\n")
    }

    /// Optional connect kicks + reground texts. No bootstrap when Script owns opening.
    pub fn midcall_cues(&self, ctx: &CallerPolicyContext) -> Vec<MidcallCue> {
        let mut cues: Vec<MidcallCue> = Vec::new();
        let verbosity = ctx.resolved_verbosity();
        let script_owns_opening = ctx.script_steps.iter().any(|step| {
            let trigger = step
                .get("trigger")
                .and_then(|v| v.as_str())
                .map(String::from);
            let action = step
                .get("action")
                .and_then(|v| v.as_str())
                .map(String::from);
            let say = step
                .get("say")
                .map(|v| v.as_str().unwrap_or(&v.to_string()).to_string())
                .unwrap_or_default();
            (matches!(trigger.as_deref(), Some("time") | Some("silence") | None))
                && (action.is_none() || action.as_deref() == Some("speak"))
                && !say.trim().is_empty()
        });
        if ctx.first_speaker == "user" && !script_owns_opening {
            let open_hint = match verbosity {
                Verbosity::Quiet => {
                    "greet briefly and state why you are calling in one short clause"
                }
                Verbosity::Chatty => {
                    "greet and state why you are calling in a natural opening turn"
                }
                Verbosity::Natural => {
                    "greet briefly and state why you are calling in one natural turn"
                }
            };
            cues.push(MidcallCue {
                text: format!(
                    "(The call just connected. You speak first per PERSONA: {open_hint}.)"
                ),
                kind: "bootstrap".into(),
                label: "first_speaker_user".into(),
            });
        }
        let goals = ctx.goals();
        if !goals.is_empty() {
            let g0: String = goals[0].chars().take(120).collect();
            cues.push(MidcallCue {
                text: format!(
                    "(Stay on your caller goals. Current focus: GOAL 1 — {g0}. \
                     Do not end the call early. Do not switch into assistant mode.)"
                ),
                kind: "reground".into(),
                label: "goal_reground".into(),
            });
        }
        if !ctx.script_steps.is_empty() {
            let between = match verbosity {
                Verbosity::Quiet => "answer questions in one short spoken clause;",
                Verbosity::Chatty => {
                    "keep a conversational loop — answer in several spoken clauses \
                     with context when helpful; do not go mute after one short line;"
                }
                Verbosity::Natural => {
                    "keep a conversational loop — answer in about 2–5 natural spoken clauses \
                     with context when helpful; do not go mute after one short line;"
                }
            };
            cues.push(MidcallCue {
                text: format!(
                    "(Timed Script overlay is active. Do not say bye / goodbye / [END_CALL]. \
                     Between cues, {between} the simulator will hang up.)"
                ),
                kind: "reground".into(),
                label: "script_no_early_bye".into(),
            });
        }
        cues
    }
}

impl Default for DefaultCallerPolicy {
    fn default() -> Self {
        Self::new()
    }
}
