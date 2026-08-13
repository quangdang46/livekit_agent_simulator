//! WebSocket transport layer.
//!
//! Handles raw connection establishment, frame I/O, and TLS (via `rustls`).
//! This is the lowest layer — it knows nothing about JSON or the Gemini
//! protocol.  The [`session`](crate::session) layer wraps [`Connection`] to
//! add protocol-level concerns.
//!
//! # Endpoint and Auth Routing
//!
//! Transport routing is modeled as two orthogonal choices:
//!
//! - [`Endpoint`] chooses the WebSocket host and RPC path.
//! - [`Auth`] chooses how credentials are attached to the handshake request.
//!
//! The currently supported first-class combinations are:
//!
//! | Endpoint | Auth | Wire behavior |
//! |---|---|---|
//! | [`Endpoint::GeminiApi`] | [`Auth::ApiKey`] | `wss://generativelanguage.googleapis.com/ws/…v1beta.GenerativeService.BidiGenerateContent?key=…` |
//! | [`Endpoint::GeminiApi`] | [`Auth::EphemeralToken`] | `wss://generativelanguage.googleapis.com/ws/…v1alpha.GenerativeService.BidiGenerateContentConstrained?access_token=…` |
//! | [`Endpoint::VertexAi`] | [`Auth::BearerToken`] or [`Auth::BearerTokenProvider`] | `wss://{location}-aiplatform.googleapis.com/ws/google.cloud.aiplatform.v1.LlmBidiService/BidiGenerateContent` + `Authorization: Bearer …` |
//!
//! [`Endpoint::Custom`] is the explicit escape hatch for tests, proxies, and
//! already-routed deployments.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use futures_util::stream::{SplitSink, SplitStream};
use futures_util::{SinkExt, StreamExt};
use tokio::net::TcpStream;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::{HeaderValue, Request, header::AUTHORIZATION};
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;
use tokio_tungstenite::tungstenite::{self, Message};
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream, connect_async_with_config};
use url::Url;

use crate::error::{BearerTokenError, ConnectError, RecvError, SendError};

#[cfg(feature = "vertex-auth")]
mod vertex_auth;

#[cfg(feature = "vertex-auth")]
pub use vertex_auth::VertexAiApplicationDefaultCredentials;

const GEMINI_API_HOST: &str = "wss://generativelanguage.googleapis.com";
const GEMINI_API_KEY_PATH: &str =
    "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent";
const GEMINI_EPHEMERAL_TOKEN_PATH: &str =
    "/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained";
const VERTEX_AI_PATH: &str = "/ws/google.cloud.aiplatform.v1.LlmBidiService/BidiGenerateContent";

// ── Endpoint ────────────────────────────────────────────────────────────────

/// WebSocket endpoint family.
///
/// This type is the canonical home for transport-level routing semantics.
/// It selects the host and RPC path only. Credential attachment is controlled
/// separately via [`Auth`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub enum Endpoint {
    /// Public Gemini API Live endpoint on `generativelanguage.googleapis.com`.
    #[default]
    GeminiApi,
    /// Vertex AI Live endpoint pinned to the current `v1` RPC path.
    ///
    /// `setup.model` must use the full Vertex resource name, for example:
    /// `projects/{project}/locations/{location}/publishers/google/models/{model}`.
    VertexAi { location: String },
    /// Explicit raw WebSocket URL for tests, proxies, or custom deployments.
    Custom(String),
}

// ── BearerTokenProvider ──────────────────────────────────────────────────────

type BearerTokenFuture<'a> =
    Pin<Box<dyn Future<Output = Result<String, BearerTokenError>> + Send + 'a>>;

trait DynBearerTokenProvider: Send + Sync {
    fn name(&self) -> &'static str;
    fn bearer_token(&self) -> BearerTokenFuture<'_>;
}

struct FnBearerTokenProvider<F> {
    name: &'static str,
    func: F,
}

impl<F, Fut> DynBearerTokenProvider for FnBearerTokenProvider<F>
where
    F: Fn() -> Fut + Send + Sync + 'static,
    Fut: Future<Output = Result<String, BearerTokenError>> + Send + 'static,
{
    fn name(&self) -> &'static str {
        self.name
    }

    fn bearer_token(&self) -> BearerTokenFuture<'_> {
        Box::pin((self.func)())
    }
}

/// Refreshable bearer-token source for header-authenticated endpoints.
///
/// The provider is queried on every WebSocket connect attempt. Implementations
/// should therefore handle their own token caching and refresh behavior if the
/// underlying token source is expensive or rate limited.
#[derive(Clone)]
pub struct BearerTokenProvider {
    inner: Arc<dyn DynBearerTokenProvider>,
}

impl BearerTokenProvider {
    fn new<P>(provider: P) -> Self
    where
        P: DynBearerTokenProvider + 'static,
    {
        Self {
            inner: Arc::new(provider),
        }
    }

    /// Create a provider from an async function or closure.
    pub fn from_fn<F, Fut>(name: &'static str, func: F) -> Self
    where
        F: Fn() -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<String, BearerTokenError>> + Send + 'static,
    {
        Self::new(FnBearerTokenProvider { name, func })
    }

    /// Fetch a bearer token for the next connection attempt.
    pub async fn bearer_token(&self) -> Result<String, BearerTokenError> {
        self.inner.bearer_token().await
    }

    #[cfg(feature = "vertex-auth")]
    /// Create a provider backed by Google Cloud Application Default
    /// Credentials with the `cloud-platform` scope.
    pub fn vertex_ai_application_default() -> Result<Self, BearerTokenError> {
        Ok(VertexAiApplicationDefaultCredentials::new()?.into_bearer_token_provider())
    }
}

impl std::fmt::Debug for BearerTokenProvider {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BearerTokenProvider")
            .field("kind", &self.inner.name())
            .finish()
    }
}

// ── Auth ─────────────────────────────────────────────────────────────────────

/// Authentication method for the WebSocket handshake.
#[derive(Clone)]
pub enum Auth {
    /// Send no credentials.
    ///
    /// This is mainly useful with [`Endpoint::Custom`] when targeting a mock
    /// server, a local test harness, or a proxy that already performs auth.
    None,
    /// Standard long-lived API key (sent as `?key=` query param).
    ApiKey(String),
    /// Short-lived token obtained via the ephemeral token endpoint (v1alpha).
    EphemeralToken(String),
    /// OAuth 2.0 bearer token sent via the `Authorization` header.
    ///
    /// This is the first-class auth mode for [`Endpoint::VertexAi`].
    BearerToken(String),
    /// Refreshable bearer-token provider.
    ///
    /// Use this for endpoints such as Vertex AI where reconnect logic should
    /// obtain a fresh token instead of reusing a previously captured string.
    BearerTokenProvider(BearerTokenProvider),
}

impl std::fmt::Debug for Auth {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::None => f.debug_tuple("None").finish(),
            Self::ApiKey(_) => f.debug_tuple("ApiKey").field(&"<redacted>").finish(),
            Self::EphemeralToken(_) => f
                .debug_tuple("EphemeralToken")
                .field(&"<redacted>")
                .finish(),
            Self::BearerToken(_) => f.debug_tuple("BearerToken").field(&"<redacted>").finish(),
            Self::BearerTokenProvider(provider) => f
                .debug_tuple("BearerTokenProvider")
                .field(provider)
                .finish(),
        }
    }
}

impl Auth {
    #[cfg(feature = "vertex-auth")]
    /// Build Vertex-compatible bearer auth from Google Cloud Application
    /// Default Credentials with the `cloud-platform` scope.
    pub fn vertex_ai_application_default() -> Result<Self, BearerTokenError> {
        Ok(Self::BearerTokenProvider(
            BearerTokenProvider::vertex_ai_application_default()?,
        ))
    }
}

// ── TransportConfig ──────────────────────────────────────────────────────────

/// Transport layer settings.
///
/// All fields have sensible defaults (see [`Default`] impl).  In most cases
/// [`endpoint`](Self::endpoint) and [`auth`](Self::auth) are the only
/// fields that need to be set explicitly.
#[derive(Debug, Clone)]
pub struct TransportConfig {
    /// WebSocket host and RPC path family.
    pub endpoint: Endpoint,
    /// Handshake credential strategy.
    pub auth: Auth,
    /// WebSocket write buffer size in bytes.  Default: 1 MB.
    pub write_buffer_size: usize,
    /// Maximum WebSocket frame size in bytes.  Default: 16 MB.
    pub max_frame_size: usize,
    /// Connection timeout.  Default: 10 s.
    pub connect_timeout: Duration,
}

impl Default for TransportConfig {
    fn default() -> Self {
        Self {
            endpoint: Endpoint::GeminiApi,
            auth: Auth::None,
            write_buffer_size: 1024 * 1024,
            max_frame_size: 16 * 1024 * 1024,
            connect_timeout: Duration::from_secs(10),
        }
    }
}

// ── RawFrame ─────────────────────────────────────────────────────────────────

/// A raw WebSocket frame received from the server.
#[derive(Debug, Clone, PartialEq)]
pub enum RawFrame {
    /// UTF-8 text frame (JSON on the Gemini Live protocol).
    Text(String),
    /// Binary frame.
    Binary(Vec<u8>),
    /// Close frame with optional (code, reason) — D4 fork patch surfaces the
    /// close code so the caller can classify (1006/1008/1011/1007 table).
    Close(Option<(Option<u16>, String)>),
}

// ── Connection ───────────────────────────────────────────────────────────────

type WsStream = WebSocketStream<MaybeTlsStream<TcpStream>>;

/// Low-level WebSocket connection handle.
///
/// Wraps a split `tokio-tungstenite` stream (sink + source) and provides
/// simple send/recv methods for raw frames.  This type is **not** meant to
/// be used directly by application code — the session layer manages it.
pub struct Connection {
    sink: SplitSink<WsStream, Message>,
    stream: SplitStream<WsStream>,
}

impl Connection {
    /// Establish a WebSocket connection (does **not** send the `setup` message).
    pub async fn connect(config: &TransportConfig) -> Result<Self, ConnectError> {
        install_rustls_crypto_provider();

        let request = build_request(config).await?;
        let mut ws_config = WebSocketConfig::default();
        ws_config.write_buffer_size = config.write_buffer_size;
        ws_config.max_write_buffer_size = config.write_buffer_size * 2;
        ws_config.max_frame_size = Some(config.max_frame_size);
        ws_config.max_message_size = Some(config.max_frame_size);

        let connect_fut = connect_async_with_config(request, Some(ws_config), false);

        let (ws_stream, _response) = tokio::time::timeout(config.connect_timeout, connect_fut)
            .await
            .map_err(|_| ConnectError::Timeout(config.connect_timeout))?
            .map_err(classify_connect_error)?;

        let (sink, stream) = ws_stream.split();
        tracing::debug!("WebSocket connection established");
        Ok(Self { sink, stream })
    }

    /// Send a text frame (typically a serialised JSON message).
    pub async fn send_text(&mut self, json: &str) -> Result<(), SendError> {
        self.sink
            .send(Message::text(json))
            .await
            .map_err(classify_send_error)
    }

    /// Send a binary frame.
    pub async fn send_binary(&mut self, data: &[u8]) -> Result<(), SendError> {
        self.sink
            .send(Message::binary(data.to_vec()))
            .await
            .map_err(classify_send_error)
    }

    /// Receive the next meaningful frame, skipping ping/pong control frames.
    pub async fn recv(&mut self) -> Result<RawFrame, RecvError> {
        loop {
            match self.stream.next().await {
                Some(Ok(msg)) => {
                    tracing::trace!(msg_type = ?std::mem::discriminant(&msg), "raw ws frame received");
                    match msg {
                        Message::Text(text) => return Ok(RawFrame::Text(text.to_string())),
                        Message::Binary(data) => return Ok(RawFrame::Binary(data.to_vec())),
                        Message::Close(frame) => {
                            let code_reason =
                                frame.map(|f| (Some(f.code.into()), f.reason.to_string()));
                            return Ok(RawFrame::Close(code_reason));
                        }
                        // Ping/Pong are handled at the tungstenite protocol level.
                        Message::Ping(_) | Message::Pong(_) | Message::Frame(_) => continue,
                    }
                }
                Some(Err(e)) => return Err(RecvError::Ws(e)),
                None => return Err(RecvError::Closed),
            }
        }
    }

    /// Send a close frame without consuming the connection.
    pub(crate) async fn send_close(&mut self) -> Result<(), SendError> {
        self.sink
            .send(Message::Close(None))
            .await
            .map_err(classify_send_error)
    }

    /// Gracefully close the connection by sending a close frame.
    pub async fn close(mut self) -> Result<(), SendError> {
        self.send_close().await
    }
}

// ── helpers ──────────────────────────────────────────────────────────────────

pub(crate) fn install_rustls_crypto_provider() {
    // Idempotent. Needed both for WebSocket TLS and feature-gated token
    // helpers that perform HTTPS token refreshes before the socket exists.
    let _ = rustls::crypto::ring::default_provider().install_default();
}

async fn build_request(config: &TransportConfig) -> Result<Request<()>, ConnectError> {
    validate_transport_config(config)?;

    let url = build_url(config)?;
    let mut request = url
        .as_str()
        .into_client_request()
        .map_err(|e| ConnectError::Config(format!("invalid websocket request: {e}")))?;

    if let Some(header) = build_bearer_header(&config.auth).await? {
        request.headers_mut().insert(AUTHORIZATION, header);
    }

    Ok(request)
}

fn validate_transport_config(config: &TransportConfig) -> Result<(), ConnectError> {
    match (&config.endpoint, &config.auth) {
        (Endpoint::GeminiApi, Auth::ApiKey(_) | Auth::EphemeralToken(_)) => Ok(()),
        (Endpoint::GeminiApi, Auth::None) => Err(ConnectError::Config(
            "Endpoint::GeminiApi requires Auth::ApiKey or Auth::EphemeralToken".into(),
        )),
        (Endpoint::GeminiApi, Auth::BearerToken(_) | Auth::BearerTokenProvider(_)) => Err(
            ConnectError::Config(
                "Endpoint::GeminiApi does not use bearer auth; use Auth::ApiKey or Auth::EphemeralToken".into(),
            ),
        ),
        (
            Endpoint::VertexAi { location },
            Auth::BearerToken(_) | Auth::BearerTokenProvider(_),
        ) => {
            if location.trim().is_empty() {
                return Err(ConnectError::Config(
                    "Endpoint::VertexAi location must not be empty".into(),
                ));
            }
            Ok(())
        }
        (Endpoint::VertexAi { .. }, _) => Err(ConnectError::Config(
            "Endpoint::VertexAi requires Auth::BearerToken or Auth::BearerTokenProvider"
                .into(),
        )),
        (Endpoint::Custom(url), _) => {
            Url::parse(url).map_err(|e| ConnectError::Config(format!("invalid custom endpoint URL: {e}")))?;
            Ok(())
        }
    }
}

fn build_url(config: &TransportConfig) -> Result<Url, ConnectError> {
    let mut url = match &config.endpoint {
        Endpoint::GeminiApi => {
            Url::parse(&format!(
                "{}{}",
                GEMINI_API_HOST,
                gemini_path_for_auth(&config.auth)
            ))
        }
        .map_err(|e| ConnectError::Config(format!("invalid Gemini API endpoint URL: {e}")))?,
        Endpoint::VertexAi { location } => Url::parse(&format!(
            "wss://{location}-aiplatform.googleapis.com{VERTEX_AI_PATH}"
        ))
        .map_err(|e| ConnectError::Config(format!("invalid Vertex AI endpoint URL: {e}")))?,
        Endpoint::Custom(url) => Url::parse(url)
            .map_err(|e| ConnectError::Config(format!("invalid custom endpoint URL: {e}")))?,
    };

    match &config.auth {
        Auth::ApiKey(key) => {
            url.query_pairs_mut().append_pair("key", key);
        }
        Auth::EphemeralToken(token) => {
            url.query_pairs_mut().append_pair("access_token", token);
        }
        Auth::None | Auth::BearerToken(_) | Auth::BearerTokenProvider(_) => {}
    }

    Ok(url)
}

fn gemini_path_for_auth(auth: &Auth) -> &'static str {
    match auth {
        Auth::EphemeralToken(_) => GEMINI_EPHEMERAL_TOKEN_PATH,
        Auth::None | Auth::ApiKey(_) | Auth::BearerToken(_) | Auth::BearerTokenProvider(_) => {
            GEMINI_API_KEY_PATH
        }
    }
}

async fn build_bearer_header(auth: &Auth) -> Result<Option<HeaderValue>, ConnectError> {
    match auth {
        Auth::BearerToken(token) => HeaderValue::from_str(&format!("Bearer {token}"))
            .map(Some)
            .map_err(|e| ConnectError::Config(format!("invalid bearer token header: {e}"))),
        Auth::BearerTokenProvider(provider) => {
            let token = provider.bearer_token().await.map_err(ConnectError::Auth)?;
            HeaderValue::from_str(&format!("Bearer {token}"))
                .map(Some)
                .map_err(|e| {
                    ConnectError::Auth(BearerTokenError::with_source(
                        "token provider returned an invalid bearer token",
                        e,
                    ))
                })
        }
        Auth::None | Auth::ApiKey(_) | Auth::EphemeralToken(_) => Ok(None),
    }
}

fn classify_connect_error(e: tungstenite::Error) -> ConnectError {
    match e {
        tungstenite::Error::Http(response) => ConnectError::Rejected {
            status: response.status().as_u16(),
        },
        other => ConnectError::Ws(other),
    }
}

fn classify_send_error(e: tungstenite::Error) -> SendError {
    match e {
        tungstenite::Error::ConnectionClosed | tungstenite::Error::AlreadyClosed => {
            SendError::Closed
        }
        other => SendError::Ws(other),
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use super::*;

    #[tokio::test]
    async fn request_gemini_api_key_uses_query_auth() {
        let config = TransportConfig {
            endpoint: Endpoint::GeminiApi,
            auth: Auth::ApiKey("test-key-123".into()),
            ..Default::default()
        };
        let request = build_request(&config).await.expect("request");
        let uri = request.uri().to_string();

        assert!(uri.starts_with("wss://generativelanguage.googleapis.com"));
        assert!(uri.contains("BidiGenerateContent?key=test-key-123"));
        assert!(!uri.contains("v1alpha"));
        assert!(request.headers().get(AUTHORIZATION).is_none());
    }

    #[tokio::test]
    async fn request_gemini_ephemeral_token_uses_constrained_path() {
        let config = TransportConfig {
            endpoint: Endpoint::GeminiApi,
            auth: Auth::EphemeralToken("tok-abc".into()),
            ..Default::default()
        };
        let request = build_request(&config).await.expect("request");
        let uri = request.uri().to_string();

        assert!(uri.contains("v1alpha"));
        assert!(uri.contains("BidiGenerateContentConstrained?access_token=tok-abc"));
        assert!(request.headers().get(AUTHORIZATION).is_none());
    }

    #[tokio::test]
    async fn request_vertex_ai_uses_bearer_header() {
        let config = TransportConfig {
            endpoint: Endpoint::VertexAi {
                location: "us-central1".into(),
            },
            auth: Auth::BearerToken("vertex-token".into()),
            ..Default::default()
        };
        let request = build_request(&config).await.expect("request");

        assert_eq!(
            request.uri(),
            "wss://us-central1-aiplatform.googleapis.com/ws/google.cloud.aiplatform.v1.LlmBidiService/BidiGenerateContent"
        );
        assert_eq!(
            request
                .headers()
                .get(AUTHORIZATION)
                .expect("authorization header"),
            "Bearer vertex-token"
        );
    }

    #[tokio::test]
    async fn request_custom_endpoint_can_skip_auth() {
        let config = TransportConfig {
            endpoint: Endpoint::Custom("wss://custom.example.com/ws".into()),
            auth: Auth::None,
            ..Default::default()
        };
        let request = build_request(&config).await.expect("request");

        assert_eq!(request.uri(), "wss://custom.example.com/ws");
        assert!(request.headers().get(AUTHORIZATION).is_none());
    }

    #[tokio::test]
    async fn request_vertex_ai_provider_fetches_token_per_connect() {
        let calls = Arc::new(AtomicUsize::new(0));
        let provider = BearerTokenProvider::from_fn("test-sequence", {
            let calls = Arc::clone(&calls);
            move || {
                let calls = Arc::clone(&calls);
                async move {
                    let next = calls.fetch_add(1, Ordering::Relaxed) + 1;
                    Ok(format!("token-{next}"))
                }
            }
        });

        let config = TransportConfig {
            endpoint: Endpoint::VertexAi {
                location: "us-central1".into(),
            },
            auth: Auth::BearerTokenProvider(provider),
            ..Default::default()
        };

        let first = build_request(&config).await.expect("first request");
        let second = build_request(&config).await.expect("second request");

        assert_eq!(
            first.headers().get(AUTHORIZATION).expect("first auth"),
            "Bearer token-1"
        );
        assert_eq!(
            second.headers().get(AUTHORIZATION).expect("second auth"),
            "Bearer token-2"
        );
        assert_eq!(calls.load(Ordering::Relaxed), 2);
    }

    #[tokio::test]
    async fn request_vertex_ai_provider_error_bubbles() {
        let config = TransportConfig {
            endpoint: Endpoint::VertexAi {
                location: "us-central1".into(),
            },
            auth: Auth::BearerTokenProvider(BearerTokenProvider::from_fn(
                "always-fails",
                || async { Err(BearerTokenError::new("boom")) },
            )),
            ..Default::default()
        };

        let err = build_request(&config)
            .await
            .expect_err("provider failure should bubble");

        assert!(matches!(err, ConnectError::Auth(source) if source.to_string() == "boom"));
    }

    #[tokio::test]
    async fn invalid_vertex_auth_is_rejected_before_connect() {
        let config = TransportConfig {
            endpoint: Endpoint::VertexAi {
                location: "us-central1".into(),
            },
            auth: Auth::ApiKey("not-vertex".into()),
            ..Default::default()
        };
        let err = build_request(&config).await.expect_err("config error");

        assert!(
            matches!(err, ConnectError::Config(message) if message == "Endpoint::VertexAi requires Auth::BearerToken or Auth::BearerTokenProvider")
        );
    }

    #[tokio::test]
    async fn invalid_gemini_bearer_auth_is_rejected_before_connect() {
        let config = TransportConfig {
            endpoint: Endpoint::GeminiApi,
            auth: Auth::BearerTokenProvider(BearerTokenProvider::from_fn("wrong", || async {
                Ok("wrong".into())
            })),
            ..Default::default()
        };
        let err = build_request(&config).await.expect_err("config error");

        assert!(
            matches!(err, ConnectError::Config(message) if message.contains("does not use bearer auth"))
        );
    }

    #[test]
    fn default_config_values() {
        let config = TransportConfig::default();

        assert_eq!(config.endpoint, Endpoint::GeminiApi);
        assert!(matches!(config.auth, Auth::None));
        assert_eq!(config.write_buffer_size, 1024 * 1024);
        assert_eq!(config.max_frame_size, 16 * 1024 * 1024);
        assert_eq!(config.connect_timeout, Duration::from_secs(10));
    }
}
