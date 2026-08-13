//! Tests for script farewell + hang_up_gate heuristics (vs end_call.py /
//! hang_up_gate.py).
use lks_core::script::farewell::{
    contains_end_call_signal, contains_farewell_signal, should_end_call_on_turn,
    strip_end_call_signal, strip_farewell_signal,
};
use lks_core::script::hang_up_gate::agent_left_open_turn;

#[test]
fn contains_end_call_signal_cases() {
    assert!(contains_end_call_signal("[END_CALL]"));
    assert!(contains_end_call_signal("let's end call now"));
    assert!(contains_end_call_signal("I will hang up now"));
    assert!(!contains_end_call_signal("hello there"));
    assert!(!contains_end_call_signal(""));
}

#[test]
fn contains_farewell_signal_cases() {
    assert!(contains_farewell_signal("bye"));
    assert!(contains_farewell_signal("goodbye"));
    assert!(contains_farewell_signal("thank you for your time"));
    assert!(contains_farewell_signal("see you later"));
    assert!(contains_farewell_signal("tạm biệt"));
    assert!(contains_farewell_signal("kết thúc"));
    // end_call signal also counts as farewell
    assert!(contains_farewell_signal("end call"));
    assert!(!contains_farewell_signal("hello"));
    assert!(!contains_farewell_signal(""));
}

#[test]
fn strip_end_call_signal_cleans() {
    assert_eq!(strip_end_call_signal("[END_CALL]"), "");
    assert_eq!(strip_end_call_signal("end call now"), "now");
    assert_eq!(
        strip_end_call_signal("please hang up. then go"),
        "please then go"
    );
    assert_eq!(strip_end_call_signal(""), "");
}

#[test]
fn strip_farewell_signal_cleans() {
    assert_eq!(strip_farewell_signal("okay goodbye"), "okay");
    // "bye" is a farewell; bare "thanks" (no "again for your time") is NOT in
    // the regex — Python leaves it too.
    assert_eq!(strip_farewell_signal("bye thanks"), "thanks");
    assert_eq!(strip_farewell_signal("thank you for your time"), "");
    assert_eq!(strip_farewell_signal(""), "");
}

#[test]
fn should_end_call_on_turn_cases() {
    // scripted_farewell → never end
    assert!(!should_end_call_on_turn(false, true, true, true));
    // pending_script → defer
    assert!(!should_end_call_on_turn(true, true, true, false));
    // no script, ended → end
    assert!(should_end_call_on_turn(false, true, false, false));
    // no script, farewell → end
    assert!(should_end_call_on_turn(false, false, true, false));
    // neither → no
    assert!(!should_end_call_on_turn(false, false, false, false));
}

#[test]
fn agent_left_open_turn_cases() {
    // closing markers → false (hang_up may proceed)
    assert!(!agent_left_open_turn(Some("Goodbye, have a great day")));
    assert!(!agent_left_open_turn(Some("thank you for calling")));
    // question → true
    assert!(agent_left_open_turn(Some("What's your name?")));
    // open prompt markers → true
    assert!(agent_left_open_turn(Some("Can you tell me your full name")));
    assert!(agent_left_open_turn(Some("May I have your phone number")));
    assert!(agent_left_open_turn(Some("How can I help you today")));
    // neutral → false
    assert!(!agent_left_open_turn(Some("I'll check that for you")));
    assert!(!agent_left_open_turn(Some("")));
    assert!(!agent_left_open_turn(None));
}
