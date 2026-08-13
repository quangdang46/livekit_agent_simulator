//! Configuration types used within the `setup` message.
//!
//! These control model behaviour, audio/video input handling, VAD, session
//! resumption, context compression, and more.  All structs derive [`Default`]
//! so callers can use the `..Default::default()` pattern for partial init.
//!
//! When upstream model families differ, prefer documenting the difference on
//! the exact field or type that carries it.

use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};

// ── Generation config ────────────────────────────────────────────────────────

/// Controls how the model generates responses.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct GenerationConfig {
    /// Which modalities the model should produce (`AUDIO`, `TEXT`, or both).
    ///
    /// For voice-first clients this is typically `[AUDIO]` or
    /// `[AUDIO, TEXT]`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub response_modalities: Option<Vec<Modality>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub speech_config: Option<SpeechConfig>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thinking_config: Option<ThinkingConfig>,
    /// Image resolution hint sent to the model.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub media_resolution: Option<MediaResolution>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub temperature: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub top_p: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub top_k: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_output_tokens: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub candidate_count: Option<u32>,
}

/// Output modality requested from the model.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Modality {
    Audio,
    Text,
}

// ── Speech / Voice ───────────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SpeechConfig {
    pub voice_config: VoiceConfig,
    /// D4 fork patch: mirrors python-genai SpeechConfig.language_code.
    /// Serializes as `languageCode`; server accepts it. `None` → server auto-detect.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub language_code: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct VoiceConfig {
    pub prebuilt_voice_config: PrebuiltVoiceConfig,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PrebuiltVoiceConfig {
    /// Voice name, e.g. `"Kore"`, `"Puck"`, `"Charon"`, etc.
    pub voice_name: String,
}

// ── Thinking ─────────────────────────────────────────────────────────────────

/// Thinking / reasoning configuration.
///
/// Gemini 3.1 uses `thinking_level` (enum), while Gemini 2.5 uses
/// `thinking_budget` (token count).  Both may be set; the model ignores
/// the field it doesn't understand.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ThinkingConfig {
    /// Gemini 3.1: discrete level.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thinking_level: Option<ThinkingLevel>,
    /// Gemini 2.5: token budget.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thinking_budget: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub include_thoughts: Option<bool>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ThinkingLevel {
    Minimal,
    Low,
    Medium,
    High,
}

impl ThinkingLevel {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Minimal => "minimal",
            Self::Low => "low",
            Self::Medium => "medium",
            Self::High => "high",
        }
    }
}

impl fmt::Display for ThinkingLevel {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseThinkingLevelError {
    raw: String,
}

impl ParseThinkingLevelError {
    pub fn raw(&self) -> &str {
        &self.raw
    }
}

impl fmt::Display for ParseThinkingLevelError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "unsupported thinking level {:?}; expected one of: minimal, low, medium, high",
            self.raw
        )
    }
}

impl std::error::Error for ParseThinkingLevelError {}

impl FromStr for ThinkingLevel {
    type Err = ParseThinkingLevelError;

    fn from_str(raw: &str) -> Result<Self, Self::Err> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "minimal" => Ok(Self::Minimal),
            "low" => Ok(Self::Low),
            "medium" => Ok(Self::Medium),
            "high" => Ok(Self::High),
            _ => Err(ParseThinkingLevelError {
                raw: raw.to_string(),
            }),
        }
    }
}

// ── Media resolution ─────────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum MediaResolution {
    MediaResolutionLow,
    MediaResolutionHigh,
}

// ── Realtime input config ────────────────────────────────────────────────────

/// Controls how the server interprets real-time audio/video input.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RealtimeInputConfig {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub automatic_activity_detection: Option<AutomaticActivityDetection>,
    /// What happens when user activity is detected while the model is speaking.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub activity_handling: Option<ActivityHandling>,
    /// What audio is included in the user's turn.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub turn_coverage: Option<TurnCoverage>,
}

/// Server-side Voice Activity Detection parameters.
///
/// When `disabled` is `true`, the client must send `activityStart` /
/// `activityEnd` signals manually.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AutomaticActivityDetection {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub disabled: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub start_of_speech_sensitivity: Option<StartSensitivity>,
    /// Milliseconds of audio to retain before the detected speech onset.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub prefix_padding_ms: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub end_of_speech_sensitivity: Option<EndSensitivity>,
    /// Milliseconds of silence required to mark speech as ended.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub silence_duration_ms: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum StartSensitivity {
    StartSensitivityHigh,
    StartSensitivityLow,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum EndSensitivity {
    EndSensitivityHigh,
    EndSensitivityLow,
}

/// What happens when user activity (speech) is detected while the model is
/// generating a response.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ActivityHandling {
    /// User speech interrupts the model (default).
    StartOfActivityInterrupts,
    /// Model continues uninterrupted.
    NoInterruption,
}

/// Which portions of the audio stream are included in the user's turn.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum TurnCoverage {
    /// Only detected speech activity (default on Gemini 2.5).
    TurnIncludesOnlyActivity,
    /// All audio including silence.
    TurnIncludesAllInput,
    /// Speech activity + all video frames (default on Gemini 3.1).
    TurnIncludesAudioActivityAndAllVideo,
}

// ── Session resumption ───────────────────────────────────────────────────────

/// Enables session resumption.  Include an empty struct to opt in; pass a
/// previous `handle` to resume a disconnected session.
///
/// The Live API currently documents resumption tokens as valid for **2 hours
/// after the last session termination**.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionResumptionConfig {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub handle: Option<String>,
}

// ── Context window compression ───────────────────────────────────────────────

/// Server-side context compression.  When the context grows past
/// `trigger_tokens`, the server compresses it down to
/// `sliding_window.target_tokens`.
///
/// This is not a presence-activated empty object. The Live API expects a
/// compression mechanism to be selected, so callers should normally send at
/// least `sliding_window: Some(SlidingWindow::default())`, which serializes as
/// `{"slidingWindow": {}}`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ContextWindowCompressionConfig {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sliding_window: Option<SlidingWindow>,
    /// Token count that triggers compression (default ≈ 80% of context limit).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub trigger_tokens: Option<u64>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SlidingWindow {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub target_tokens: Option<u64>,
}

// ── Transcription ────────────────────────────────────────────────────────────

/// Presence-activated config — include an empty `{}` to enable transcription
/// for the corresponding direction (input or output).
///
/// The server treats the field's presence as the signal; callers should send
/// `Some(AudioTranscriptionConfig {})`, not a boolean.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct AudioTranscriptionConfig {}

// ── Proactivity (v1alpha, Gemini 2.5) ────────────────────────────────────────

/// Gemini 2.5-only proactive audio settings (`v1alpha`).
///
/// New model families may ignore this entirely, so callers should treat it as
/// model-specific rather than universally available.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ProactivityConfig {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub proactive_audio: Option<bool>,
}

// ── History (Gemini 3.1) ─────────────────────────────────────────────────────

/// Controls how conversation history is bootstrapped.
///
/// On Gemini 3.1, `clientContent` can only be sent as initial history
/// (before the first `realtimeInput`).  Set `initial_history_in_client_content`
/// to `true` to enable this.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct HistoryConfig {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub initial_history_in_client_content: Option<bool>,
}

// ── Tool definitions ─────────────────────────────────────────────────────────

/// Tool declarations made available during `setup`.
///
/// Each entry maps to exactly one top-level tool field on the wire, for
/// example `{"googleSearch": {}}` or
/// `{"functionDeclarations": [{...}, {...}]}`.
///
/// Keep built-in Live tools as first-class enum variants here instead of
/// forcing callers through raw JSON.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum Tool {
    /// Custom client-side functions that the model may call.
    #[serde(rename = "functionDeclarations")]
    FunctionDeclarations(Vec<FunctionDeclaration>),
    /// Google-managed web search executed on the server side.
    #[serde(rename = "googleSearch")]
    GoogleSearch(GoogleSearchTool),
}

/// Enable the built-in Google Search tool.
///
/// This is a presence-activated empty object on the wire:
/// `{"googleSearch": {}}`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct GoogleSearchTool {}

/// A custom function the model may call during the session.
///
/// Keep this aligned with the official Live API tool docs. Gemini 3.1 and 2.5
/// differ in important ways, especially around asynchronous tool execution.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FunctionDeclaration {
    pub name: String,
    pub description: String,
    /// JSON Schema object describing the function's parameters.
    pub parameters: serde_json::Value,
    /// Crate-level scheduling field retained for Gemini 2.5 compatibility
    /// work.
    ///
    /// Official current Live API docs place response scheduling inside the
    /// tool-response payload, not here. Treat this field as under audit until
    /// roadmap item `F-9` is completed.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub scheduling: Option<FunctionScheduling>,
    /// Gemini 2.5: whether the function blocks model generation.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub behavior: Option<FunctionBehavior>,
}

/// Scheduling values historically associated with Gemini 2.5 tool execution.
///
/// See [`FunctionDeclaration::scheduling`] for the current crate caveat.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum FunctionScheduling {
    /// Immediately interrupt model output (default).
    Interrupt,
    /// Wait until the model is idle.
    WhenIdle,
    /// Run silently without interrupting.
    Silent,
}

/// Whether the function response blocks continued model generation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum FunctionBehavior {
    /// Model continues generating while awaiting the response.
    NonBlocking,
}
