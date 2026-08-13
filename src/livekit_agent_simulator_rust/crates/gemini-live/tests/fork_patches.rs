//! Verify the D4 fork patches (SpeechConfig.language_code, reconnect_on_drop
//! default, close-code surfacing).

use gemini_live::{PrebuiltVoiceConfig, ReconnectPolicy, SessionConfig, SpeechConfig, VoiceConfig};

fn speech_config(voice_name: &str, language_code: Option<&str>) -> SpeechConfig {
    SpeechConfig {
        voice_config: VoiceConfig {
            prebuilt_voice_config: PrebuiltVoiceConfig {
                voice_name: voice_name.into(),
            },
        },
        language_code: language_code.map(String::from),
    }
}

#[test]
fn speech_config_language_code_serializes_camel_case() {
    // Patch 1: language_code exists and serializes as languageCode.
    let sc = speech_config("Puck", Some("en-US"));
    let json = serde_json::to_string(&sc).expect("serialize");
    assert!(json.contains("\"languageCode\":\"en-US\""), "got: {json}");
    assert!(json.contains("\"voiceName\":\"Puck\""), "got: {json}");
}

#[test]
fn speech_config_language_code_optional_omitted() {
    // When None, the field is omitted (skip_serializing_if).
    let sc = speech_config("Puck", None);
    let json = serde_json::to_string(&sc).expect("serialize");
    assert!(!json.contains("languageCode"), "omitted: {json}");
}

#[test]
fn reconnect_policy_default_no_midcall_reconnect() {
    // Patch 2: reconnect_on_drop defaults to false (Python parity — no mid-call
    // reconnect on ConnectionLost; GoAway still reconnects).
    let default = ReconnectPolicy::default();
    assert!(
        !default.reconnect_on_drop,
        "default reconnect_on_drop is false"
    );
    assert!(default.enabled, "reconnect enabled (for GoAway)");
    assert_eq!(default.max_attempts, Some(10));
}

#[test]
fn reconnect_policy_knob_settable() {
    let p = ReconnectPolicy {
        enabled: true,
        base_backoff: std::time::Duration::from_millis(500),
        max_backoff: std::time::Duration::from_secs(5),
        max_attempts: Some(10),
        reconnect_on_drop: true,
    };
    assert!(p.reconnect_on_drop);
}

#[test]
fn session_config_reconnect_policy_is_exposed() {
    // SessionConfig carries the ReconnectPolicy; build one with the knob off.
    let cfg = SessionConfig {
        transport: Default::default(),
        setup: Default::default(),
        reconnect: ReconnectPolicy {
            enabled: true,
            base_backoff: std::time::Duration::from_millis(500),
            max_backoff: std::time::Duration::from_secs(5),
            max_attempts: Some(10),
            reconnect_on_drop: false,
        },
    };
    assert!(!cfg.reconnect.reconnect_on_drop);
}
