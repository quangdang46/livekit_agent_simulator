//! Portable caller trait library — byte-parity port of `persona_traits.py`.
//!
//! Scenarios use `Persona.spec.traits: ["impatient", "quiet", ...]`. Unknown tags
//! pass through as free-text behavior hints. Length-band aliases (quiet/silent/
//! terse → quiet, chatty → chatty) resolve in CallerPolicyContext.resolved_verbosity.

use serde_json::Value as Json;

/// Canonical trait id → instruction bullets for the sim caller (English).
pub fn trait_library() -> std::collections::BTreeMap<&'static str, &'static [&'static str]> {
    let mut m = std::collections::BTreeMap::new();
    m.insert(
        "polite",
        &["Stay courteous; use soft openers and thank the agent when appropriate."][..],
    );
    m.insert(
        "impatient",
        &[
            "You are short on time: speak a bit faster, push for a quick answer,",
            "and do not tolerate long monologues.",
        ][..],
    );
    m.insert(
        "interrupts",
        &[
            "You sometimes cut in while the agent is still talking with a brief correction",
            "or urgency (one short phrase). Do not monologue over them.",
        ][..],
    );
    m.insert(
        "quiet",
        &["You are reserved: leave pauses before answering; replies are very short."][..],
    );
    m.insert(
        "terse",
        &["Keep answers minimal: one short clause when possible; no extra detail."][..],
    );
    m.insert(
        "silent",
        &["You often go quiet after the agent speaks; only answer when necessary."][..],
    );
    m.insert(
        "confused",
        &["You misunderstand details once or twice and ask the agent to repeat simply."][..],
    );
    m.insert(
        "elderly",
        &["Speak slightly slower and ask the agent to speak clearly; prefer simple words."][..],
    );
    m.insert(
        "angry",
        &[
            "You are mildly frustrated (not abusive). Express annoyance briefly if the agent",
            "is unclear, but stay on topic.",
        ][..],
    );
    m.insert(
        "fast_speaker",
        &[
            "Speak quickly and denser than average phone speech; stay within the length band above.",
        ][..],
    );
    m.insert(
        "soft_spoken",
        &["Keep volume soft and wording gentle; avoid sharp interruptions."][..],
    );
    m.insert(
        "non_native",
        &["You are fluent enough but occasionally ask for clarification of complex words."][..],
    );
    m.insert(
        "skeptical",
        &["Question vague claims; ask for concrete next steps or confirmation."][..],
    );
    m.insert(
        "chatty",
        &["You add a small extra detail about your situation, but still finish goals."][..],
    );
    m.insert(
        "backchannel",
        &[
            "Occasionally acknowledge with a very short uh-huh / okay while listening,",
            "without stealing the full turn.",
        ][..],
    );
    m.insert(
        "hangup_threat",
        &[
            "If the agent is unhelpful or loops the menu, you may briefly threaten to hang up,",
            "then give one more chance before ending the call.",
        ][..],
    );
    m.insert(
        "code_switch",
        &["You mainly use the call language but may mix one short English phrase if stuck."][..],
    );
    m.insert(
        "urgent",
        &["State urgency early; prefer concrete next steps over long explanations."][..],
    );
    m
}

/// Return prompt lines for known traits + passthrough for unknown tags.
/// traits may be a JSON array of strings or a single string.
pub fn expand_traits(traits: &Json) -> Vec<String> {
    let raw_items: Vec<String> = match traits {
        Json::String(s) => vec![s.clone()],
        Json::Array(a) => a
            .iter()
            .map(|v| match v {
                Json::String(s) => s.clone(),
                other => other.to_string(),
            })
            .collect(),
        _ => Vec::new(),
    };
    let lib = trait_library();
    let mut lines: Vec<String> = Vec::new();
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut unknown: Vec<String> = Vec::new();
    for raw in &raw_items {
        let tag = raw.trim();
        if tag.is_empty() {
            continue;
        }
        let key = tag.to_lowercase().replace([' ', '-'], "_");
        if seen.contains(&key) {
            continue;
        }
        seen.insert(key.clone());
        if let Some(bullets) = lib.get(key.as_str()) {
            lines.extend(bullets.iter().map(|s| s.to_string()));
        } else {
            unknown.push(tag.to_string());
        }
    }
    if !unknown.is_empty() {
        lines.push(format!(
            "Additional caller behavior (follow naturally): {}.",
            unknown.join(", ")
        ));
    }
    lines
}

/// Sorted trait ids.
pub fn list_trait_ids() -> Vec<&'static str> {
    trait_library().keys().copied().collect()
}
