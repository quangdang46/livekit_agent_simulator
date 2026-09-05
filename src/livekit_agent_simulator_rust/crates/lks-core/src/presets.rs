//! Builtin judge criteria presets — verbatim port of `evals/presets.py`.
//!
//! Keys usable as `PassCriteria.judges[].builtin` or `criteria: "builtin:<key>"`.

use serde_json::{json, Map, Value as Json};

/// Builtin dimensional criteria (Hamming dims + LiveKit-shaped instructions).
pub static PRESETS: &[(&str, &str)] = &[
    (
        "task_completion",
        "Task completion: Did the caller's goal finish with the correct final state \
         (completed, appropriately handed off, or correctly declined)? \
         Ignore tone; focus on outcome and tool results.",
    ),
    (
        "factual_accuracy",
        "Factual accuracy: Were facts, prices, eligibility, dates, and policy statements \
         consistent with tool outputs / evidence? Flag contradictions or unsupported claims.",
    ),
    (
        "policy_compliance",
        "Policy and compliance: Were required disclosures, refusals, consent statements, \
         and restricted topics handled correctly when applicable?",
    ),
    (
        "conversation_flow",
        "Conversation flow: Did the agent avoid useless loops, ignored context, \
         and excessive dead-air or overtalk relative to the caller's needs? \
         Flag: repeated re-asking of the same field, long delays (>5s per turn), \
         and the agent talking over the caller.",
    ),
    (
        "conversation_naturalness",
        "Conversational naturalness — review the call as a REAL HUMAN would. \
         Imagine you are a product manager listening to the call with a stopwatch \
         and a notepad, judging 'would a caller feel this is a natural phone \
         conversation, or that they are talking to a scripted bot?'. For each rule \
         below, quote the EXACT agent line that breaks it (verbatim, in the \
         caller's language) and explain the human impact (e.g. 'the caller \
         answered X but the agent ignored it and re-asked Y'). Do not just say \
         met/not met — a person should be able to act on the notes.\n\
         1. ONE question per turn — the agent asks a single question and waits. \
         Stacking two questions in one reply (Q1 + Q2) reads as rushed and \
         confusing — flag it.\n\
         2. Minimal confirmation — confirm a value ONLY when it is genuinely \
         risky to mis-hear (phone, email, ambiguous date/time). Repeated \
         thank-you / echo / 'is that correct?' per field makes the call feel \
         mechanical — flag low-value confirmations.\n\
         3. Acknowledge-then-redirect — when the caller volunteers a later field \
         before being asked, the agent should acknowledge it naturally and steer \
         back to the missing required field ('got it, online interview — and \
         before we continue, could you give me the emergency contact?'). Ignoring \
         what the caller just said feels like the agent is not listening — flag \
         it.\n\
         4. Relative dates — when the caller gives a relative date (next month, \
         next week, end of next month), the agent must echo it as stated, NOT \
         invent an absolute date unless the system is known to have resolved it. \
         Inventing a date out of thin air reads as a hallucination to a human — \
         flag it.\n\
         5. Turn latency — a human caller notices delay: under ~3s feels natural, \
         3–5s acceptable, over 5s feels 'stuck'. Quote the turn numbers and \
         timings for any turn over 5s.\n\
         Score how close the call feels to a natural human conversation, and \
         write the notes as if giving feedback to an engineer who must fix the \
         conversation quality.",
    ),
    (
        "empathy",
        "Empathy and professionalism: Was tone appropriate for the caller's situation \
         without replacing task or policy correctness?",
    ),
    (
        "escalation",
        "Escalation judgment: Did the agent transfer, hand off, or refuse at the right time \
         given severity and policy?",
    ),
    (
        "accuracy",
        "Accuracy (LiveKit-style): Verify the agent grounds claims in tool outputs; \
         catch hallucinations and contradictions with tool results.",
    ),
    (
        "coherence",
        "Coherence (LiveKit-style): Responses follow a logical structure and stay on-topic \
         across turns without ignoring the caller.",
    ),
];

fn preset(key: &str) -> Result<&'static str, String> {
    PRESETS
        .iter()
        .find(|(k, _)| *k == key)
        .map(|(_, v)| *v)
        .ok_or_else(|| format!("Unknown judge builtin preset: {key}"))
}

/// Port of `presets.expand_criterion`: expand a single `builtin:<key>` string.
pub fn expand_criterion(item: &str) -> Result<String, String> {
    let s = item.trim();
    if let Some(key) = s.strip_prefix("builtin:") {
        preset(key.trim()).map(String::from)
    } else {
        Ok(s.to_string())
    }
}

/// Port of `presets.expand_criteria`.
pub fn expand_criteria(items: &[String]) -> Result<Vec<String>, String> {
    items.iter().map(|c| expand_criterion(c)).collect()
}

/// Port of `presets.expand_judge_group`: copy with criteria expanded from
/// `builtin` (prepended) / `builtin:<key>` entries.
pub fn expand_judge_group(group: &Map<String, Json>) -> Result<Map<String, Json>, String> {
    let mut out = group.clone();
    let builtin = group
        .get("builtin")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .filter(|s| !s.is_empty());
    let criteria: Vec<String> = group
        .get("criteria")
        .and_then(|v| v.as_array())
        .map(|a| a.iter().map(|v| v.as_str().unwrap_or_default().to_string()).collect())
        .unwrap_or_default();
    let expanded = match builtin {
        Some(key) => {
            let text = preset(key)?;
            let mut all = vec![text.to_string()];
            all.extend(expand_criteria(&criteria)?);
            all
        }
        None => expand_criteria(&criteria)?,
    };
    out.insert("criteria".into(), json!(expanded));
    Ok(out)
}

/// Port of `presets.list_presets` (sorted keys).
pub fn list_presets() -> Vec<&'static str> {
    let mut keys: Vec<&'static str> = PRESETS.iter().map(|(k, _)| *k).collect();
    keys.sort_unstable();
    keys
}
