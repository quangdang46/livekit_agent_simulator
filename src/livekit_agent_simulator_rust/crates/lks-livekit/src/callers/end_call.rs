//! Hang-up control markers for the simulated caller — port of
//! `callers/end_call.py`. The OpenAI caller's output is the caller's speech;
//! when it contains a hang-up token or a soft farewell ("I'll call back
//! later", "goodbye"), the bridge must end the run instead of committing +
//! re-requesting a response (which would make the caller self-loop forever).

/// Harness token the persona prompt uses to request a hard hang-up.
pub const END_CALL_TOKEN: &str = "[END_CALL]";

/// Spoken English forms often uttered instead of the bracket token.
fn spoken_end_re() -> regex::Regex {
    regex::Regex::new(
        r"(?i)(?:\[\s*end[_\s\-]*call\s*\]|\bend[_\s\-]*call\b|\bhang[_\s\-]*up\b)[.!?]*",
    )
    .expect("spoken end regex")
}

/// Soft farewells that make the agent under test end the call even without
/// `[END_CALL]` (portable; includes a few Vietnamese forms like Python).
fn farewell_re() -> regex::Regex {
    regex::Regex::new(
        r"(?i)(?:\bgood\s*bye\b|\bgoodbye\b|\bbye[\s\-]?bye\b|\bbye\b|\bsee\s+you(?:\s+later)?\b|\btalk\s+later\b|\btalk\s+soon\b|\bthat'?s\s+all\b|\bthanks?\s+again\s+for\s+your\s+time\b|\bthank\s+you\s+for\s+your\s+time\b|\bi'?ll\s+(?:be\s+)?back\s+in\s+touch\b|tạm\s*biệt|kết\s*thúc|cúp\s*máy)[.!?]*",
    )
    .expect("farewell regex")
}

/// True when the text carries a hard end-call signal (token or spoken form).
pub fn contains_end_call_signal(text: &str) -> bool {
    if text.is_empty() {
        return false;
    }
    if text.contains(END_CALL_TOKEN) {
        return true;
    }
    spoken_end_re().is_match(text)
}

/// True for bye/goodbye-style closings (with or without the harness token).
pub fn contains_farewell_signal(text: &str) -> bool {
    if text.is_empty() {
        return false;
    }
    contains_end_call_signal(text) || farewell_re().is_match(text)
}

/// Strip the harness token and spoken hang-up phrases.
pub fn strip_end_call_signal(text: &str) -> String {
    let mut out = text.replace(END_CALL_TOKEN, " ");
    out = spoken_end_re().replace_all(&out, " ").to_string();
    collapse_punct(&out)
}

/// Strip harness markers + soft farewell words for transcript logging.
pub fn strip_farewell_signal(text: &str) -> String {
    let mut out = strip_end_call_signal(text);
    out = farewell_re().replace_all(&out, " ").to_string();
    collapse_punct(&out)
}

fn collapse_punct(s: &str) -> String {
    // Collapse whitespace-before-punct, drop trailing commas/space, collapse
    // runs of spaces (port of the regex chain in end_call.py).
    let re1 = regex::Regex::new(r"\s+([,.!?])").expect("punct regex");
    let out = re1.replace_all(s, "$1").to_string();
    let re2 = regex::Regex::new(r"[,\s]+$").expect("trail regex");
    let out = re2.replace_all(&out, "").to_string();
    out.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// True when the caller's freestyle text should tear down the room.
/// Port of `should_end_call_on_turn`.
pub fn should_end_call_on_turn(ended: bool, farewell: bool) -> bool {
    ended || farewell
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_token_and_spoken_forms() {
        assert!(contains_end_call_signal("[END_CALL]"));
        assert!(contains_end_call_signal("Please end call now."));
        assert!(contains_end_call_signal("I will hang up."));
        assert!(!contains_end_call_signal("Hello, how are you?"));
    }

    #[test]
    fn detects_farewells() {
        // Exact parity with Python end_call._FAREWELL_RE: "talk to you later"
        // / "talk to you soon" are NOT farewells (only "talk later"/"talk
        // soon"); "I'll call back later" alone is not either (only "I'll (be)
        // back in touch"). See the Python regex — these assertions mirror it.
        assert!(contains_farewell_signal("Goodbye, I'll call back later!"));
        assert!(contains_farewell_signal("Thanks for your time, bye."));
        assert!(contains_farewell_signal("Talk soon."));
        assert!(contains_farewell_signal("See you later."));
        assert!(!contains_farewell_signal("Talk to you later."));
        assert!(!contains_farewell_signal("I'll call back later"));
        assert!(!contains_farewell_signal("Can you repeat that please?"));
    }

    #[test]
    fn strips_markers() {
        assert_eq!(strip_end_call_signal("Bye [END_CALL]").trim(), "Bye");
        // Matches Python's exact output: "Goodbye" → space → collapse_punct
        // leaves a leading comma (Python does the same).
        let cleaned = strip_farewell_signal("Goodbye, I'll call back later!");
        assert_eq!(cleaned, ", I'll call back later!", "got: {cleaned:?}");
    }

    #[test]
    fn should_end_on_either() {
        assert!(should_end_call_on_turn(true, false));
        assert!(should_end_call_on_turn(false, true));
        assert!(!should_end_call_on_turn(false, false));
    }
}
