//! Client → Server message types.
//!
//! The Gemini Live protocol defines **4 client message kinds**, each carrying
//! exactly one top-level field:
//!
//! | Variant          | Wire field        | When to send                     |
//! |------------------|-------------------|----------------------------------|
//! | `Setup`          | `setup`           | First message only               |
//! | `ClientContent`  | `clientContent`   | Conversation history / turns     |
//! | `RealtimeInput`  | `realtimeInput`   | Streaming audio / video / text   |
//! | `ToolResponse`   | `toolResponse`    | Replies to server `toolCall`     |
//!
//! [`ClientMessage`] is serialised as a serde externally-tagged enum, which
//! naturally produces `{"setup": {...}}` etc.

use serde::Serialize;

use super::common::{Blob, Content, EmptyObject};
use super::config::*;

/// A message sent from client to server.
///
/// The protocol requires each message to carry **exactly one** top-level field.
/// Serde's externally-tagged enum representation satisfies this constraint
/// directly — `ClientMessage::Setup(cfg)` serialises to `{"setup": { ... }}`.
#[derive(Debug, Clone, Serialize)]
pub enum ClientMessage {
    #[serde(rename = "setup")]
    Setup(SetupConfig),
    #[serde(rename = "clientContent")]
    ClientContent(ClientContent),
    #[serde(rename = "realtimeInput")]
    RealtimeInput(RealtimeInput),
    #[serde(rename = "toolResponse")]
    ToolResponse(ToolResponseMessage),
}

// ── Setup ────────────────────────────────────────────────────────────────────

/// The first (and only) `setup` message, configuring the session.
///
/// `model` is the only required field. All others have sensible server
/// defaults when omitted.
///
/// This is the canonical home for setup-field semantics in the crate. Keep
/// model-family caveats and wire-format notes on these fields rather than
/// restating them in standalone docs.
#[derive(Debug, Clone, Default, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SetupConfig {
    /// Model resource name, e.g. `"models/gemini-3.1-flash-live-preview"`.
    pub model: String,
    /// Generation-time settings such as response modalities, voice, thinking,
    /// and sampling controls.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub generation_config: Option<GenerationConfig>,
    /// System prompt or instruction content applied at session setup time.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub system_instruction: Option<Content>,
    /// Tool definitions available to the model for this session.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tools: Option<Vec<Tool>>,
    /// Real-time audio/video interpretation settings including VAD behaviour.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub realtime_input_config: Option<RealtimeInputConfig>,
    /// Opts the session into server-issued resume handles.
    ///
    /// The session layer patches `handle` automatically during reconnects.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub session_resumption: Option<SessionResumptionConfig>,
    /// Server-managed context compression settings.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub context_window_compression: Option<ContextWindowCompressionConfig>,
    /// Presence-activated input speech transcription (`{}` to enable).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub input_audio_transcription: Option<AudioTranscriptionConfig>,
    /// Presence-activated output speech transcription (`{}` to enable).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub output_audio_transcription: Option<AudioTranscriptionConfig>,
    /// Proactive audio (v1alpha, Gemini 2.5 only).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub proactivity: Option<ProactivityConfig>,
    /// History bootstrapping (Gemini 3.1). This only affects how initial
    /// `clientContent` may be sent before the first `realtimeInput`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub history_config: Option<HistoryConfig>,
}

// ── ClientContent ────────────────────────────────────────────────────────────

/// Conversation history or incremental content.
///
/// On Gemini 2.5 this can be sent at any time during the session.
/// On Gemini 3.1 it can only be sent as initial history (before the first
/// `realtimeInput`), and requires `historyConfig.initialHistoryInClientContent = true`.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ClientContent {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub turns: Option<Vec<Content>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub turn_complete: Option<bool>,
}

// ── RealtimeInput ────────────────────────────────────────────────────────────

/// Streaming real-time input — audio, video, text, or VAD control signals.
///
/// Each message should carry only **one** of these fields.
///
/// # Audio format
/// 16-bit signed little-endian PCM, recommended 16 kHz sample rate.
/// Chunk size: 100–250 ms (3,200–8,000 bytes raw).
///
/// # Video format
/// JPEG or PNG, max 1 fps, recommended < 200 KB per frame.
#[derive(Debug, Clone, Default, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RealtimeInput {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub audio: Option<Blob>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub video: Option<Blob>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    /// Manual VAD: signal that user activity has started.
    /// Requires `automaticActivityDetection.disabled = true`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub activity_start: Option<EmptyObject>,
    /// Manual VAD: signal that user activity has ended.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub activity_end: Option<EmptyObject>,
    /// Auto VAD: notify server that the mic has been muted / stream ended.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub audio_stream_end: Option<bool>,
}

// ── ToolResponse ─────────────────────────────────────────────────────────────

/// Response to one or more server-initiated function calls.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ToolResponseMessage {
    pub function_responses: Vec<FunctionResponse>,
}

/// A single function call result, keyed by the server-assigned `id`.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct FunctionResponse {
    /// Must match the `id` from the corresponding [`FunctionCallRequest`](super::server_message::FunctionCallRequest).
    pub id: String,
    pub name: String,
    /// Arbitrary JSON result returned to the model.
    ///
    /// Keep the payload flexible: current Live API docs place some Gemini 2.5
    /// tool-response scheduling knobs inside this JSON object rather than as a
    /// top-level Rust field.
    pub response: serde_json::Value,
}
