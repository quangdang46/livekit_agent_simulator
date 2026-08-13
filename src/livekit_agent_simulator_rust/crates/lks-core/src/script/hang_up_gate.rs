//! Gates for Script hang_up — byte-parity port of `script/hang_up_gate.py`.
//!
//! `agent_left_open_turn`: True when the last agent final still expects a caller
//! reply (defer Script hang_up so timed silence does not end an open dialogue).

const CLOSING_MARKERS: [&str; 12] = [
    "goodbye",
    "good bye",
    "bye for now",
    "bye.",
    "bye!",
    "have a great",
    "have a good",
    "take care",
    "thank you for calling",
    "thanks for calling",
    "call ended",
    "hanging up",
];

const OPEN_PROMPT_MARKERS: [&str; 27] = [
    "what's your",
    "what is your",
    "what was your",
    "which car",
    "what sort of",
    "may i have",
    "can i have",
    "could you",
    "can you tell",
    "can you give",
    "can you hear",
    "still there",
    "are you there",
    "please provide",
    "please tell",
    "your name",
    "full name",
    "phone number",
    "email address",
    "card number",
    "how can i help",
    "anything else",
    "shall we",
    "would you like",
    "do you want",
    "are you ready",
    "whereabouts",
];

/// True when the last agent final still expects a caller reply.
pub fn agent_left_open_turn(text: Option<&str>) -> bool {
    let Some(text) = text else {
        return false;
    };
    let t = text.trim();
    if t.is_empty() {
        return false;
    }
    let lower = t.to_lowercase();
    if CLOSING_MARKERS.iter().any(|m| lower.contains(m)) {
        return false;
    }
    // Mid-utterance questions still expect a reply.
    if t.contains('?') {
        return true;
    }
    OPEN_PROMPT_MARKERS.iter().any(|m| lower.contains(m))
}
