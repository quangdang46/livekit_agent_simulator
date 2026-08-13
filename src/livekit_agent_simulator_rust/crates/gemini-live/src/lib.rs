//! # gemini-live
//!
//! High-performance, idiomatic Rust client for the
//! [Gemini Multimodal Live API](https://ai.google.dev/api/live).
//!
//! Designed for real-time audio/video streaming where every allocation
//! counts.  See [`audio::AudioEncoder`] for the zero-allocation hot path,
//! `docs/design.md` for performance goals, and `docs/roadmap.md` for
//! known gaps and planned work.
//!
//! ## Architecture
//!
//! The crate is organised in layers — each builds on the one below:
//!
//! | Layer         | Module        | Responsibility                                                  |
//! |---------------|---------------|-----------------------------------------------------------------|
//! | **Session**   | [`session`]   | Connection lifecycle, reconnection, typed send/receive          |
//! | **Transport** | [`transport`] | WebSocket connection, TLS, frame I/O                            |
//! | **Codec**     | [`codec`]     | JSON ↔ Rust; [`ServerMessage`] → [`ServerEvent`] decomposition  |
//! | **Audio**     | [`audio`]     | PCM encoding utilities and format constants                     |
//! | **Types**     | [`types`]     | Strongly-typed wire-format structs                              |
//! | **Errors**    | [`error`]     | Layered error enums                                             |
//!
//! ## Quick start
//!
//! ```rust,no_run
//! # async fn example() -> Result<(), Box<dyn std::error::Error>> {
//! use gemini_live::session::{Session, SessionConfig, ReconnectPolicy};
//! use gemini_live::transport::{Auth, TransportConfig};
//! use gemini_live::types::*;
//!
//! let mut session = Session::connect(SessionConfig {
//!     transport: TransportConfig {
//!         auth: Auth::ApiKey(std::env::var("GEMINI_API_KEY")?),
//!         ..Default::default()
//!     },
//!     setup: SetupConfig {
//!         model: "models/gemini-3.1-flash-live-preview".into(),
//!         generation_config: Some(GenerationConfig {
//!             response_modalities: Some(vec![Modality::Text]),
//!             ..Default::default()
//!         }),
//!         ..Default::default()
//!     },
//!     reconnect: ReconnectPolicy::default(),
//! }).await?;
//!
//! session.send_text("Hello!").await?;
//!
//! while let Some(event) = session.next_event().await {
//!     match event {
//!         ServerEvent::ModelText(text) => print!("{text}"),
//!         ServerEvent::TurnComplete => println!("\n--- turn done ---"),
//!         _ => {}
//!     }
//! }
//! # Ok(())
//! # }
//! ```
//!
//! For Vertex AI, switch the transport endpoint and auth mode:
//!
//! ```rust,no_run
//! # async fn example() -> Result<(), Box<dyn std::error::Error>> {
//! use gemini_live::session::{ReconnectPolicy, Session, SessionConfig};
//! use gemini_live::transport::{Auth, Endpoint, TransportConfig};
//! use gemini_live::types::*;
//!
//! let mut session = Session::connect(SessionConfig {
//!     transport: TransportConfig {
//!         endpoint: Endpoint::VertexAi {
//!             location: "us-central1".into(),
//!         },
//!         auth: Auth::BearerToken(std::env::var("VERTEX_AI_ACCESS_TOKEN")?),
//!         ..Default::default()
//!     },
//!     setup: SetupConfig {
//!         model: std::env::var("VERTEX_MODEL")?,
//!         generation_config: Some(GenerationConfig {
//!             response_modalities: Some(vec![Modality::Text]),
//!             ..Default::default()
//!         }),
//!         ..Default::default()
//!     },
//!     reconnect: ReconnectPolicy::default(),
//! })
//! .await?;
//! # drop(session);
//! # Ok(())
//! # }
//! ```
//!
//! Enable the optional `vertex-auth` crate feature if you want the library to
//! obtain Vertex bearer tokens from Google Cloud Application Default
//! Credentials instead of passing a static token string.

pub mod audio;
pub mod codec;
pub mod error;
pub mod session;
pub mod transport;
pub mod types;

// Re-export the most commonly used items at the crate root for convenience.
pub use error::*;
pub use session::{ReconnectPolicy, Session, SessionConfig, SessionStatus};
#[cfg(feature = "vertex-auth")]
pub use transport::VertexAiApplicationDefaultCredentials;
pub use transport::{Auth, BearerTokenProvider, Connection, Endpoint, RawFrame, TransportConfig};
pub use types::*;
