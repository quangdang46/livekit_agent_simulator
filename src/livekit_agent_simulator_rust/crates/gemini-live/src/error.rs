//! Error types for each architectural layer.
//!
//! Errors are split by layer so callers can match on the granularity they need:
//! - [`CodecError`] — JSON serialization / deserialization failures.
//! - [`ConnectError`] — WebSocket connection establishment failures.
//! - [`SendError`] / [`RecvError`] — frame-level I/O on an established connection.
//! - [`SessionError`] — high-level session lifecycle errors (setup, reconnect, etc.).

use std::time::Duration;

// ── Codec layer ──────────────────────────────────────────────────────────────

/// Failure during JSON ↔ Rust conversion.
#[derive(Debug, thiserror::Error)]
pub enum CodecError {
    #[error("JSON serialization failed: {0}")]
    Serialize(#[source] serde_json::Error),
    #[error("JSON deserialization failed: {0}")]
    Deserialize(#[source] serde_json::Error),
}

// ── Auth helper errors ───────────────────────────────────────────────────────

/// Failure while obtaining an OAuth bearer token for a WebSocket handshake.
#[derive(Debug, thiserror::Error)]
#[error("{message}")]
pub struct BearerTokenError {
    message: String,
    #[source]
    source: Option<Box<dyn std::error::Error + Send + Sync>>,
}

impl BearerTokenError {
    /// Create a token error without an underlying source.
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            source: None,
        }
    }

    /// Create a token error with an underlying source error.
    pub fn with_source(
        message: impl Into<String>,
        source: impl std::error::Error + Send + Sync + 'static,
    ) -> Self {
        Self {
            message: message.into(),
            source: Some(Box::new(source)),
        }
    }
}

// ── Transport layer ──────────────────────────────────────────────────────────

/// Failure while establishing a WebSocket connection.
#[derive(Debug, thiserror::Error)]
pub enum ConnectError {
    #[error("invalid transport configuration: {0}")]
    Config(String),
    #[error("failed to obtain bearer token: {0}")]
    Auth(#[source] BearerTokenError),
    #[error("DNS resolution failed: {0}")]
    Dns(#[source] Box<dyn std::error::Error + Send + Sync>),
    #[error("TLS handshake failed: {0}")]
    Tls(#[source] Box<dyn std::error::Error + Send + Sync>),
    #[error("connection timed out after {0:?}")]
    Timeout(Duration),
    #[error("WebSocket handshake rejected: {status}")]
    Rejected { status: u16 },
    #[error("WebSocket error: {0}")]
    Ws(#[source] tokio_tungstenite::tungstenite::Error),
}

/// Failure while sending a WebSocket frame.
#[derive(Debug, thiserror::Error)]
pub enum SendError {
    #[error("connection closed")]
    Closed,
    #[error("WebSocket send failed: {0}")]
    Ws(#[source] tokio_tungstenite::tungstenite::Error),
}

/// Failure while receiving a WebSocket frame.
#[derive(Debug, thiserror::Error)]
pub enum RecvError {
    #[error("connection closed")]
    Closed,
    #[error("WebSocket receive failed: {0}")]
    Ws(#[source] tokio_tungstenite::tungstenite::Error),
}

// ── Session layer ────────────────────────────────────────────────────────────

/// High-level session error covering setup, runtime, and reconnection.
#[derive(Debug, thiserror::Error)]
pub enum SessionError {
    #[error("setup failed: {0}")]
    SetupFailed(String),
    #[error("setup timed out after {0:?}")]
    SetupTimeout(Duration),
    #[error("API error: {0}")]
    Api(String),
    #[error("connection lost and reconnection failed after {attempts} attempts")]
    ReconnectExhausted { attempts: u32 },
    #[error("session closed")]
    Closed,
    #[error(transparent)]
    Transport(#[from] SendError),
    #[error(transparent)]
    Codec(#[from] CodecError),
}
