//! Strongly-typed representations of the Gemini Live wire protocol.
//!
//! All types use `#[serde(rename_all = "camelCase")]` so Rust code stays
//! `snake_case` while JSON is `camelCase`.  Optional fields are `Option<T>` +
//! `#[serde(skip_serializing_if = "Option::is_none")]` to produce minimal JSON.
//!
//! # Module layout
//!
//! | Module            | Contents                                         |
//! |-------------------|--------------------------------------------------|
//! | [`common`]        | `Content`, `Part`, `Blob`, `EmptyObject`         |
//! | [`config`]        | Generation, VAD, session, tool definition types  |
//! | [`client_message`]| `ClientMessage` enum and payload structs          |
//! | [`server_message`]| `ServerMessage`, `ServerEvent`, and related types|

pub mod client_message;
pub mod common;
pub mod config;
pub mod server_message;

// Re-export the types most callers need at a convenient depth.
pub use client_message::*;
pub use common::*;
pub use config::*;
pub use server_message::*;
