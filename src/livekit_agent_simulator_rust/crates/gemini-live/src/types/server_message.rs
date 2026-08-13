//! Server → Client message types and the semantic [`ServerEvent`] enum.
//!
//! A single wire message ([`ServerMessage`]) is a flat struct where **multiple
//! fields may be present simultaneously** (e.g. `serverContent` +
//! `usageMetadata`).  The [`codec::into_events`](crate::codec::into_events)
//! function decomposes it into a sequence of [`ServerEvent`]s — the
//! application-facing abstraction.

use std::time::Duration;

use serde::Deserialize;

use super::common::{Content, EmptyObject};

// ── Wire-level struct ────────────────────────────────────────────────────────

/// Raw server message as received on the wire.
///
/// Multiple fields can be populated in the same message.  Use
/// [`codec::into_events`](crate::codec::into_events) to decompose this into
/// a `Vec<ServerEvent>`.
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ServerMessage {
    #[serde(default)]
    pub setup_complete: Option<EmptyObject>,
    #[serde(default)]
    pub server_content: Option<ServerContent>,
    #[serde(default)]
    pub tool_call: Option<ToolCallMessage>,
    #[serde(default)]
    pub tool_call_cancellation: Option<ToolCallCancellation>,
    #[serde(default)]
    pub go_away: Option<GoAway>,
    #[serde(default)]
    pub session_resumption_update: Option<SessionResumptionUpdate>,
    #[serde(default)]
    pub usage_metadata: Option<UsageMetadata>,
    #[serde(default)]
    pub error: Option<ApiError>,
}

/// Model output and associated metadata within a single wire message.
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ServerContent {
    pub model_turn: Option<Content>,
    #[serde(default)]
    pub turn_complete: Option<bool>,
    /// `true` once the model has finished generating (the turn may still be
    /// open for transcription, grounding, etc.).
    #[serde(default)]
    pub generation_complete: Option<bool>,
    #[serde(default)]
    pub interrupted: Option<bool>,
    pub input_transcription: Option<Transcription>,
    pub output_transcription: Option<Transcription>,
    /// Grounding metadata (schema may evolve — kept as opaque JSON).
    pub grounding_metadata: Option<serde_json::Value>,
    pub url_context_metadata: Option<serde_json::Value>,
}

/// Partial audio-transcription update from the server.
///
/// Vertex AI `v1` marks both fields as optional. The server may send a
/// terminal transcription marker with `finished=true` and no `text`, so the
/// client must not require text to be present on every update.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Transcription {
    #[serde(default)]
    pub text: Option<String>,
    #[serde(default)]
    pub finished: Option<bool>,
}

// ── Tool call ────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ToolCallMessage {
    pub function_calls: Vec<FunctionCallRequest>,
}

/// A function the server wants the client to execute.
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FunctionCallRequest {
    /// Server-assigned call ID — must be echoed back in [`FunctionResponse`](super::client_message::FunctionResponse).
    pub id: String,
    pub name: String,
    /// Arguments as a JSON object matching the function's declared schema.
    pub args: serde_json::Value,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ToolCallCancellation {
    pub ids: Vec<String>,
}

// ── Session lifecycle ────────────────────────────────────────────────────────

/// Sent when the server is about to terminate the connection.
/// The client should reconnect (with a session resumption handle if available).
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct GoAway {
    /// Remaining time before disconnect, as a protobuf Duration string (e.g. `"30s"`).
    pub time_left: Option<String>,
}

/// Periodic update with a fresh session resumption handle.
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionResumptionUpdate {
    pub new_handle: Option<String>,
    #[serde(default)]
    pub resumable: Option<bool>,
}

// ── Usage metadata ───────────────────────────────────────────────────────────

/// Token usage statistics.  Accompanies many server messages.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UsageMetadata {
    #[serde(default)]
    pub prompt_token_count: u32,
    #[serde(default)]
    pub cached_content_token_count: u32,
    #[serde(default)]
    pub response_token_count: u32,
    #[serde(default)]
    pub tool_use_prompt_token_count: u32,
    #[serde(default)]
    pub thoughts_token_count: u32,
    #[serde(default)]
    pub total_token_count: u32,
    #[serde(default)]
    pub prompt_tokens_details: Option<Vec<ModalityTokenCount>>,
    #[serde(default)]
    pub response_tokens_details: Option<Vec<ModalityTokenCount>>,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ModalityTokenCount {
    pub modality: String,
    pub token_count: u32,
}

/// An error returned by the API.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct ApiError {
    pub message: String,
}

// ── Semantic event (application-facing) ──────────────────────────────────────

/// High-level event produced by decomposing a [`ServerMessage`].
///
/// One wire message may yield **multiple** events (e.g. transcription + model
/// text + usage stats arrive in the same JSON frame).  The
/// [`codec::into_events`](crate::codec::into_events) function performs this
/// decomposition.
#[derive(Debug, Clone)]
pub enum ServerEvent {
    /// The server accepted the `setup` message.
    SetupComplete,

    /// A chunk of model-generated text.
    ModelText(String),
    /// A chunk of model-generated audio (base64-decoded raw PCM bytes, 24 kHz).
    ModelAudio(Vec<u8>),

    /// The model finished generating (turn may still be open for metadata).
    GenerationComplete,
    /// The model's turn is fully complete.
    TurnComplete,
    /// The model's generation was interrupted by user activity.
    Interrupted,

    /// Transcription of the user's spoken input.
    InputTranscription(String),
    /// Transcription of the model's spoken output.
    OutputTranscription(String),

    /// The server requests one or more function calls.
    ToolCall(Vec<FunctionCallRequest>),
    /// The server cancels previously requested function calls.
    ToolCallCancellation(Vec<String>),

    /// Updated session resumption state.
    SessionResumption {
        new_handle: Option<String>,
        resumable: bool,
    },

    /// The server will disconnect soon — the client should reconnect.
    GoAway { time_left: Option<Duration> },

    /// Token usage statistics.
    Usage(UsageMetadata),

    /// The WebSocket connection was closed.
    Closed { reason: String },

    /// An API-level error.
    Error(ApiError),
}
