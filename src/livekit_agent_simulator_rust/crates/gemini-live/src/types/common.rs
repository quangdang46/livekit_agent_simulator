//! Shared types used by both client and server messages.
//!
//! These map directly to the Gemini Live API wire format — see
//! <https://ai.google.dev/api/live> for the canonical reference.

use serde::{Deserialize, Serialize};

/// A conversation turn or system instruction, composed of ordered [`Part`]s.
///
/// When used as a turn, `role` is `"user"` or `"model"`.
/// When used as `systemInstruction` in setup, `role` is typically omitted.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Content {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
    pub parts: Vec<Part>,
}

/// A single piece of content within a [`Content`] turn.
///
/// Exactly one field is populated per part on the wire, but we model it with
/// optional fields for forward-compatibility with future part types.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Part {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub inline_data: Option<Blob>,
}

/// Base64-encoded binary data with a MIME type.
///
/// Used for audio (`audio/pcm;rate=16000` input, `audio/pcm;rate=24000` output)
/// and images (`image/jpeg`, `image/png`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Blob {
    /// Base64-encoded bytes.
    pub data: String,
    /// MIME type, e.g. `"audio/pcm;rate=16000"` or `"image/jpeg"`.
    pub mime_type: String,
}

/// A JSON `{}` — used where the protocol signals intent by the field's
/// *presence* rather than its value (e.g. `activityStart`, `inputAudioTranscription`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EmptyObject {}
