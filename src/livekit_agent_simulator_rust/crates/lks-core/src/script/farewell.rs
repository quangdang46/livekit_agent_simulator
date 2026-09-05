//! Hang-up control heuristics — byte-parity port of `callers/end_call.py`.
//!
//! Detects/strips the bracket token `[END_CALL]` and spoken hang-up / farewell
//! phrases so the bridge can mute leftover PCM and (when a Script is still
//! armed) defer freestyle hang-up.

use regex::Regex;

pub const END_CALL_TOKEN: &str = "[END_CALL]";

fn spoken_end_re() -> &'static Regex {
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"(?i)(?:\[\s*end[_\s\-]*call\s*\]|\bend[_\s\-]*call\b|\bhang[_\s\-]*up\b)[.!?]*",
        )
        .expect("regex")
    })
}

fn farewell_re() -> &'static Regex {
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"(?i)(?:\bgood\s*bye\b|\bgoodbye\b|\bbye[\s\-]?bye\b|\bbye\b|\bsee\s+you(?:\s+later)?\b|\btalk\s+later\b|\btalk\s+soon\b|\bthat'?s\s+all\b|\bthanks?\s+again\s+for\s+your\s+time\b|\bthank\s+you\s+for\s+your\s+time\b|\bi'?ll\s+(?:be\s+)?back\s+in\s+touch\b|tạm\s*biệt|kết\s*thúc|cúp\s*máy)[.!?]*",
        )
        .expect("regex")
    })
}

/// `contains_end_call_signal`: token present OR spoken end-call regex.
pub fn contains_end_call_signal(text: &str) -> bool {
    if text.is_empty() {
        return false;
    }
    if text.contains(END_CALL_TOKEN) {
        return true;
    }
    spoken_end_re().is_match(text)
}

/// `contains_farewell_signal`: bye/goodbye-style closings (with or without token).
pub fn contains_farewell_signal(text: &str) -> bool {
    if text.is_empty() {
        return false;
    }
    if contains_end_call_signal(text) {
        return true;
    }
    farewell_re().is_match(text)
}

/// Collapse whitespace and stray punctuation left by a substitute.
fn tidy(out: &str) -> String {
    // `\s+([,.!?])` → `$1`
    let mut s = Regex::new(r"\s+([,.!?])")
        .expect("re")
        .replace_all(out, "$1")
        .into_owned();
    // `[,\s]+$` → ""
    s = Regex::new(r"[,\s]+$")
        .expect("re")
        .replace_all(&s, "")
        .into_owned();
    // " ".join(out.split()).strip()
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// `strip_end_call_signal`: remove token + spoken end-call phrases.
pub fn strip_end_call_signal(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    let out = text.replace(END_CALL_TOKEN, " ");
    let out = spoken_end_re().replace_all(&out, " ").into_owned();
    tidy(&out)
}

/// `strip_farewell_signal`: strip token + spoken + soft farewells.
pub fn strip_farewell_signal(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    let out = strip_end_call_signal(text);
    let out = farewell_re().replace_all(&out, " ").into_owned();
    tidy(&out)
}

/// `should_end_call_on_turn`: soft bye alone is enough when no Script owns hang-up.
pub fn should_end_call_on_turn(
    pending_script: bool,
    ended: bool,
    farewell: bool,
    scripted_farewell: bool,
) -> bool {
    if scripted_farewell {
        return false;
    }
    if pending_script {
        return false;
    }
    ended || farewell
}

/// Locale-aware hang-up farewell text (port of `script/farewell.py`).
/// Uses `language` (normalized: lowercase, `_` → `-`, base fallback to `en`).
pub fn default_hangup_farewell(language: &str) -> &'static str {
    let lang = language.to_lowercase().replace('_', "-");
    let base = lang.split('-').next().unwrap_or("en");
    match base {
        "vi" => "Cảm ơn bạn. Tạm biệt.",
        "ja" => "ありがとうございます。失礼します。",
        _ => "Okay, thanks. Bye.",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn farewell_defaults() {
        assert_eq!(default_hangup_farewell("en"), "Okay, thanks. Bye.");
        assert_eq!(default_hangup_farewell("vi-VN"), "Cảm ơn bạn. Tạm biệt.");
        assert_eq!(
            default_hangup_farewell("ja-JP"),
            "ありがとうございます。失礼します。"
        );
        assert_eq!(default_hangup_farewell("ko"), "Okay, thanks. Bye."); // fallback en
    }
}
