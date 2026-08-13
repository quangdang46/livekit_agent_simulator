//! JSON ↔ Rust codec and semantic event decomposition.
//!
//! This module sits between the transport layer (raw WebSocket frames) and the
//! session layer (typed events).  It provides three operations:
//!
//! - [`encode`] — serialise a [`ClientMessage`] to a JSON string for sending.
//! - [`decode`] — parse a JSON string from the wire into a [`ServerMessage`].
//! - [`into_events`] — decompose one [`ServerMessage`] into a `Vec<ServerEvent>`.
//!
//! The split between `decode` and `into_events` allows the session layer to
//! inspect raw fields (e.g. `session_resumption_update`) before broadcasting
//! the higher-level events to the application.

use std::time::Duration;

use base64::Engine;

use crate::error::CodecError;
use crate::types::{ClientMessage, ServerEvent, ServerMessage};

/// Serialise a [`ClientMessage`] to its JSON wire representation.
///
/// **Performance note:** allocates a new `String` per call.  A future
/// `encode_into` variant accepting a reusable buffer is planned
/// (see `docs/roadmap.md` P-2).
pub fn encode(msg: &ClientMessage) -> Result<String, CodecError> {
    serde_json::to_string(msg).map_err(CodecError::Serialize)
}

/// Parse a JSON string from the wire into a [`ServerMessage`].
pub fn decode(json: &str) -> Result<ServerMessage, CodecError> {
    serde_json::from_str(json).map_err(CodecError::Deserialize)
}

/// Decompose a single [`ServerMessage`] into a sequence of semantic
/// [`ServerEvent`]s.
///
/// One wire message often carries several pieces of information (e.g.
/// `serverContent` with both a `modelTurn` and `inputTranscription`, plus
/// `usageMetadata`).  This function teases them apart into discrete,
/// easy-to-match events.
///
/// The event ordering follows a natural "content first, metadata last"
/// convention:
/// 1. `SetupComplete`
/// 2. Transcriptions (input, then output)
/// 3. Model content (text / audio parts in wire order)
/// 4. Flags (`Interrupted`, `GenerationComplete`, `TurnComplete`)
/// 5. Tool calls / cancellations
/// 6. Session lifecycle (`SessionResumption`, `GoAway`)
/// 7. `Usage`
/// 8. `Error`
pub fn into_events(msg: ServerMessage) -> Vec<ServerEvent> {
    let mut events = Vec::new();

    // 1. Setup handshake
    if msg.setup_complete.is_some() {
        events.push(ServerEvent::SetupComplete);
    }

    // 2–4. Server content
    if let Some(sc) = msg.server_content {
        if let Some(t) = sc.input_transcription
            && let Some(text) = t.text
        {
            events.push(ServerEvent::InputTranscription(text));
        }
        if let Some(t) = sc.output_transcription
            && let Some(text) = t.text
        {
            events.push(ServerEvent::OutputTranscription(text));
        }

        if let Some(turn) = sc.model_turn {
            for part in turn.parts {
                if let Some(text) = part.text {
                    events.push(ServerEvent::ModelText(text));
                }
                if let Some(blob) = part.inline_data {
                    match base64::engine::general_purpose::STANDARD.decode(&blob.data) {
                        Ok(bytes) => events.push(ServerEvent::ModelAudio(bytes)),
                        Err(e) => {
                            tracing::warn!(error = %e, "failed to base64-decode model audio");
                        }
                    }
                }
            }
        }

        if sc.interrupted == Some(true) {
            events.push(ServerEvent::Interrupted);
        }
        if sc.generation_complete == Some(true) {
            events.push(ServerEvent::GenerationComplete);
        }
        if sc.turn_complete == Some(true) {
            events.push(ServerEvent::TurnComplete);
        }
    }

    // 5. Tool calls
    if let Some(tc) = msg.tool_call {
        events.push(ServerEvent::ToolCall(tc.function_calls));
    }
    if let Some(tcc) = msg.tool_call_cancellation {
        events.push(ServerEvent::ToolCallCancellation(tcc.ids));
    }

    // 6. Session lifecycle
    if let Some(sr) = msg.session_resumption_update {
        events.push(ServerEvent::SessionResumption {
            new_handle: sr.new_handle,
            resumable: sr.resumable.unwrap_or(false),
        });
    }
    if let Some(ga) = msg.go_away {
        events.push(ServerEvent::GoAway {
            time_left: ga.time_left.as_deref().and_then(parse_protobuf_duration),
        });
    }

    // 7. Usage
    if let Some(usage) = msg.usage_metadata {
        events.push(ServerEvent::Usage(usage));
    }

    // 8. Error
    if let Some(err) = msg.error {
        events.push(ServerEvent::Error(err));
    }

    events
}

/// Parse a protobuf Duration string (e.g. `"30s"`, `"1.5s"`) into a
/// [`std::time::Duration`].
fn parse_protobuf_duration(s: &str) -> Option<Duration> {
    let s = s.trim();
    let secs_str = s.strip_suffix('s')?;
    let secs: f64 = secs_str.parse().ok()?;
    Some(Duration::from_secs_f64(secs))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::*;

    // ── encode ───────────────────────────────────────────────────────────

    #[test]
    fn encode_setup_minimal() {
        let msg = ClientMessage::Setup(SetupConfig {
            model: "models/gemini-3.1-flash-live-preview".into(),
            ..Default::default()
        });
        let json = encode(&msg).unwrap();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(v["setup"]["model"], "models/gemini-3.1-flash-live-preview");
        // Optional fields should be absent, not null.
        assert!(v["setup"].get("generationConfig").is_none());
    }

    #[test]
    fn encode_setup_full() {
        let msg = ClientMessage::Setup(SetupConfig {
            model: "models/gemini-3.1-flash-live-preview".into(),
            generation_config: Some(GenerationConfig {
                response_modalities: Some(vec![Modality::Audio, Modality::Text]),
                speech_config: Some(SpeechConfig {
                    voice_config: VoiceConfig {
                        prebuilt_voice_config: PrebuiltVoiceConfig {
                            voice_name: "Kore".into(),
                        },
                    },
                    language_code: None,
                }),
                thinking_config: Some(ThinkingConfig {
                    thinking_level: Some(ThinkingLevel::Medium),
                    ..Default::default()
                }),
                ..Default::default()
            }),
            system_instruction: Some(Content {
                role: None,
                parts: vec![Part {
                    text: Some("You are a helpful assistant.".into()),
                    inline_data: None,
                }],
            }),
            input_audio_transcription: Some(AudioTranscriptionConfig {}),
            output_audio_transcription: Some(AudioTranscriptionConfig {}),
            session_resumption: Some(SessionResumptionConfig { handle: None }),
            context_window_compression: Some(ContextWindowCompressionConfig {
                sliding_window: Some(SlidingWindow::default()),
                trigger_tokens: None,
            }),
            ..Default::default()
        });
        let json = encode(&msg).unwrap();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();
        let setup = &v["setup"];
        assert_eq!(setup["generationConfig"]["responseModalities"][0], "AUDIO");
        assert_eq!(setup["generationConfig"]["responseModalities"][1], "TEXT");
        assert_eq!(
            setup["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"],
            "Kore"
        );
        assert_eq!(
            setup["generationConfig"]["thinkingConfig"]["thinkingLevel"],
            "medium"
        );
        assert_eq!(
            setup["systemInstruction"]["parts"][0]["text"],
            "You are a helpful assistant."
        );
        // Presence-activated configs should appear as `{}`
        assert_eq!(setup["inputAudioTranscription"], serde_json::json!({}));
        assert_eq!(setup["outputAudioTranscription"], serde_json::json!({}));
        assert_eq!(
            setup["contextWindowCompression"],
            serde_json::json!({ "slidingWindow": {} })
        );
    }

    #[test]
    fn encode_setup_with_builtin_and_function_tools() {
        let msg = ClientMessage::Setup(SetupConfig {
            model: "models/gemini-3.1-flash-live-preview".into(),
            tools: Some(vec![
                Tool::GoogleSearch(GoogleSearchTool {}),
                Tool::FunctionDeclarations(vec![FunctionDeclaration {
                    name: "read_file".into(),
                    description: "Read a file from the workspace.".into(),
                    parameters: serde_json::json!({
                        "type": "object",
                        "required": ["path"],
                        "properties": {
                            "path": { "type": "string" }
                        }
                    }),
                    scheduling: None,
                    behavior: None,
                }]),
            ]),
            ..Default::default()
        });
        let json = encode(&msg).unwrap();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();
        let tools = v["setup"]["tools"].as_array().expect("tools array");
        assert_eq!(tools[0]["googleSearch"], serde_json::json!({}));
        assert_eq!(tools[1]["functionDeclarations"][0]["name"], "read_file");
    }

    #[test]
    fn encode_client_content() {
        let msg = ClientMessage::ClientContent(ClientContent {
            turns: Some(vec![
                Content {
                    role: Some("user".into()),
                    parts: vec![Part {
                        text: Some("Hello".into()),
                        inline_data: None,
                    }],
                },
                Content {
                    role: Some("model".into()),
                    parts: vec![Part {
                        text: Some("Hi!".into()),
                        inline_data: None,
                    }],
                },
            ]),
            turn_complete: Some(true),
        });
        let json = encode(&msg).unwrap();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(v["clientContent"]["turns"][0]["role"], "user");
        assert_eq!(v["clientContent"]["turnComplete"], true);
    }

    #[test]
    fn encode_realtime_input_audio() {
        let msg = ClientMessage::RealtimeInput(RealtimeInput {
            audio: Some(Blob {
                data: "AAAA".into(),
                mime_type: "audio/pcm;rate=16000".into(),
            }),
            video: None,
            text: None,
            activity_start: None,
            activity_end: None,
            audio_stream_end: None,
        });
        let json = encode(&msg).unwrap();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(
            v["realtimeInput"]["audio"]["mimeType"],
            "audio/pcm;rate=16000"
        );
        // Other fields should be absent
        assert!(v["realtimeInput"].get("video").is_none());
    }

    #[test]
    fn encode_tool_response() {
        let msg = ClientMessage::ToolResponse(ToolResponseMessage {
            function_responses: vec![FunctionResponse {
                id: "call_123".into(),
                name: "get_weather".into(),
                response: serde_json::json!({"temperature": 72}),
            }],
        });
        let json = encode(&msg).unwrap();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(v["toolResponse"]["functionResponses"][0]["id"], "call_123");
        assert_eq!(
            v["toolResponse"]["functionResponses"][0]["response"]["temperature"],
            72
        );
    }

    // ── decode ───────────────────────────────────────────────────────────

    #[test]
    fn decode_setup_complete() {
        let json = r#"{"setupComplete":{}}"#;
        let msg = decode(json).unwrap();
        assert!(msg.setup_complete.is_some());
        assert!(msg.server_content.is_none());
    }

    #[test]
    fn decode_server_content_text() {
        let json = r#"{
            "serverContent": {
                "modelTurn": {
                    "parts": [{"text": "Hello there!"}]
                },
                "turnComplete": true
            }
        }"#;
        let msg = decode(json).unwrap();
        let sc = msg.server_content.unwrap();
        let turn = sc.model_turn.unwrap();
        assert_eq!(turn.parts[0].text.as_deref(), Some("Hello there!"));
        assert_eq!(sc.turn_complete, Some(true));
    }

    #[test]
    fn decode_server_content_with_transcription() {
        let json = r#"{
            "serverContent": {
                "inputTranscription": {"text": "What's the weather?"},
                "outputTranscription": {"text": "It's sunny today."}
            }
        }"#;
        let msg = decode(json).unwrap();
        let sc = msg.server_content.unwrap();
        assert_eq!(
            sc.input_transcription.unwrap().text.as_deref(),
            Some("What's the weather?")
        );
        assert_eq!(
            sc.output_transcription.unwrap().text.as_deref(),
            Some("It's sunny today.")
        );
    }

    #[test]
    fn decode_transcription_finished_without_text() {
        let json = r#"{
            "serverContent": {
                "outputTranscription": {"finished": true}
            }
        }"#;
        let msg = decode(json).unwrap();
        let sc = msg.server_content.unwrap();
        let transcription = sc.output_transcription.unwrap();
        assert_eq!(transcription.text, None);
        assert_eq!(transcription.finished, Some(true));
    }

    #[test]
    fn decode_tool_call() {
        let json = r#"{
            "toolCall": {
                "functionCalls": [{
                    "id": "call_abc",
                    "name": "get_weather",
                    "args": {"city": "Tokyo"}
                }]
            }
        }"#;
        let msg = decode(json).unwrap();
        let tc = msg.tool_call.unwrap();
        assert_eq!(tc.function_calls[0].id, "call_abc");
        assert_eq!(tc.function_calls[0].name, "get_weather");
        assert_eq!(tc.function_calls[0].args["city"], "Tokyo");
    }

    #[test]
    fn decode_tool_call_cancellation() {
        let json = r#"{"toolCallCancellation":{"ids":["call_1","call_2"]}}"#;
        let msg = decode(json).unwrap();
        let tcc = msg.tool_call_cancellation.unwrap();
        assert_eq!(tcc.ids, vec!["call_1", "call_2"]);
    }

    #[test]
    fn decode_go_away() {
        let json = r#"{"goAway":{"timeLeft":"30s"}}"#;
        let msg = decode(json).unwrap();
        assert_eq!(msg.go_away.unwrap().time_left.as_deref(), Some("30s"));
    }

    #[test]
    fn decode_session_resumption() {
        let json = r#"{"sessionResumptionUpdate":{"newHandle":"handle-xyz","resumable":true}}"#;
        let msg = decode(json).unwrap();
        let sr = msg.session_resumption_update.unwrap();
        assert_eq!(sr.new_handle.as_deref(), Some("handle-xyz"));
        assert_eq!(sr.resumable, Some(true));
    }

    #[test]
    fn decode_usage_metadata() {
        let json = r#"{
            "usageMetadata": {
                "promptTokenCount": 100,
                "responseTokenCount": 50,
                "totalTokenCount": 150
            }
        }"#;
        let msg = decode(json).unwrap();
        let u = msg.usage_metadata.unwrap();
        assert_eq!(u.prompt_token_count, 100);
        assert_eq!(u.response_token_count, 50);
        assert_eq!(u.total_token_count, 150);
        // Missing fields default to 0
        assert_eq!(u.cached_content_token_count, 0);
    }

    #[test]
    fn decode_error() {
        let json = r#"{"error":{"message":"rate limit exceeded"}}"#;
        let msg = decode(json).unwrap();
        assert_eq!(msg.error.unwrap().message, "rate limit exceeded");
    }

    #[test]
    fn decode_combined_content_and_usage() {
        let json = r#"{
            "serverContent": {
                "modelTurn": {"parts": [{"text": "hi"}]},
                "turnComplete": true
            },
            "usageMetadata": {"totalTokenCount": 42}
        }"#;
        let msg = decode(json).unwrap();
        assert!(msg.server_content.is_some());
        assert_eq!(msg.usage_metadata.unwrap().total_token_count, 42);
    }

    // ── into_events ──────────────────────────────────────────────────────

    #[test]
    fn into_events_setup_complete() {
        let msg = decode(r#"{"setupComplete":{}}"#).unwrap();
        let events = into_events(msg);
        assert_eq!(events.len(), 1);
        assert!(matches!(events[0], ServerEvent::SetupComplete));
    }

    #[test]
    fn into_events_model_text_and_turn_complete() {
        let msg = decode(
            r#"{"serverContent":{"modelTurn":{"parts":[{"text":"hello"}]},"turnComplete":true}}"#,
        )
        .unwrap();
        let events = into_events(msg);
        assert!(
            events
                .iter()
                .any(|e| matches!(e, ServerEvent::ModelText(t) if t == "hello"))
        );
        assert!(
            events
                .iter()
                .any(|e| matches!(e, ServerEvent::TurnComplete))
        );
    }

    #[test]
    fn into_events_model_audio_base64_decoded() {
        // "AQID" is base64 for bytes [1, 2, 3]
        let msg = decode(
            r#"{"serverContent":{"modelTurn":{"parts":[{"inlineData":{"data":"AQID","mimeType":"audio/pcm;rate=24000"}}]}}}"#,
        )
        .unwrap();
        let events = into_events(msg);
        assert!(
            events
                .iter()
                .any(|e| matches!(e, ServerEvent::ModelAudio(b) if b == &[1, 2, 3]))
        );
    }

    #[test]
    fn into_events_go_away_parses_duration() {
        let msg = decode(r#"{"goAway":{"timeLeft":"30s"}}"#).unwrap();
        let events = into_events(msg);
        assert!(
            events.iter().any(
                |e| matches!(e, ServerEvent::GoAway { time_left: Some(d) } if *d == std::time::Duration::from_secs(30))
            )
        );
    }

    #[test]
    fn into_events_combined_message() {
        let json = r#"{
            "serverContent": {
                "inputTranscription": {"text": "hey"},
                "modelTurn": {"parts": [{"text": "hi"}]},
                "turnComplete": true
            },
            "usageMetadata": {"totalTokenCount": 10}
        }"#;
        let msg = decode(json).unwrap();
        let events = into_events(msg);
        // Should have: InputTranscription, ModelText, TurnComplete, Usage
        assert_eq!(events.len(), 4);
        assert!(matches!(&events[0], ServerEvent::InputTranscription(t) if t == "hey"));
        assert!(matches!(&events[1], ServerEvent::ModelText(t) if t == "hi"));
        assert!(matches!(&events[2], ServerEvent::TurnComplete));
        assert!(matches!(&events[3], ServerEvent::Usage(_)));
    }

    #[test]
    fn into_events_ignores_transcription_markers_without_text() {
        let json = r#"{
            "serverContent": {
                "outputTranscription": {"finished": true},
                "turnComplete": true
            }
        }"#;
        let msg = decode(json).unwrap();
        let events = into_events(msg);

        assert_eq!(events.len(), 1);
        assert!(matches!(&events[0], ServerEvent::TurnComplete));
    }

    // ── parse_protobuf_duration ──────────────────────────────────────────

    #[test]
    fn parse_duration_integer_seconds() {
        assert_eq!(
            parse_protobuf_duration("30s"),
            Some(Duration::from_secs(30))
        );
    }

    #[test]
    fn parse_duration_fractional_seconds() {
        assert_eq!(
            parse_protobuf_duration("1.5s"),
            Some(Duration::from_secs_f64(1.5))
        );
    }

    #[test]
    fn parse_duration_invalid() {
        assert_eq!(parse_protobuf_duration("30m"), None);
        assert_eq!(parse_protobuf_duration("abc"), None);
    }
}
