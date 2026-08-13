# gemini-live-rs

[![crates.io](https://img.shields.io/crates/v/gemini-live.svg)](https://crates.io/crates/gemini-live)
[![docs.rs](https://docs.rs/gemini-live/badge.svg)](https://docs.rs/gemini-live)
[![CI](https://github.com/jacoblincool/gemini-live-rs/actions/workflows/ci.yml/badge.svg)](https://github.com/jacoblincool/gemini-live-rs/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

High-performance Rust client for the [Gemini Multimodal Live API](https://ai.google.dev/api/live) — real-time, bidirectional audio/video/text streaming over WebSocket.

## Features

- **Strongly typed** — every wire message has a Rust struct; serde handles the JSON mapping
- **Session management** — automatic reconnection with exponential backoff, session resumption, GoAway handling
- **Streaming-first** — `send_audio` / `send_video` / `send_text` for real-time input; event stream for output
- **Performance-conscious** — `AudioEncoder` reuses hot-path encoding buffers, and benchmark coverage exists for the hottest codec/audio paths
- **Tool calling** — typed function calls, cancellations, and built-in `googleSearch`; Gemini 2.5 async scheduling semantics remain under audit
- **Clone-friendly sessions** — `Session` is cheaply cloneable; multiple tasks can send and receive concurrently
- **Vertex-ready transport** — first-class Vertex AI Live routing via regional endpoints and bearer-token auth

## Demo

https://github.com/user-attachments/assets/745ef771-bae7-41ef-bd4f-baa994723a75

## Quick Start

Add to your `Cargo.toml`:

```toml
[dependencies]
gemini-live = "0.1"
tokio = { version = "1", features = ["full"] }
```

```rust
use gemini_live::session::{Session, SessionConfig, ReconnectPolicy};
use gemini_live::transport::{Auth, TransportConfig};
use gemini_live::types::*;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut session = Session::connect(SessionConfig {
        transport: TransportConfig {
            auth: Auth::ApiKey(std::env::var("GEMINI_API_KEY")?),
            ..Default::default()
        },
        setup: SetupConfig {
            model: "models/gemini-3.1-flash-live-preview".into(),
            generation_config: Some(GenerationConfig {
                response_modalities: Some(vec![Modality::Text]),
                ..Default::default()
            }),
            ..Default::default()
        },
        reconnect: ReconnectPolicy::default(),
    }).await?;

    session.send_text("Hello!").await?;

    while let Some(event) = session.next_event().await {
        match event {
            ServerEvent::ModelText(text) => print!("{text}"),
            ServerEvent::TurnComplete => println!("\n--- turn done ---"),
            _ => {}
        }
    }
    Ok(())
}
```

## Vertex AI

Use the Vertex transport endpoint with an OAuth access token. `setup.model`
must be the full Vertex model resource name.

```rust
use gemini_live::session::{ReconnectPolicy, Session, SessionConfig};
use gemini_live::transport::{Auth, Endpoint, TransportConfig};
use gemini_live::types::*;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut session = Session::connect(SessionConfig {
        transport: TransportConfig {
            endpoint: Endpoint::VertexAi {
                location: "us-central1".into(),
            },
            auth: Auth::BearerToken(std::env::var("VERTEX_AI_ACCESS_TOKEN")?),
            ..Default::default()
        },
        setup: SetupConfig {
            model: std::env::var("VERTEX_MODEL")?,
            generation_config: Some(GenerationConfig {
                response_modalities: Some(vec![Modality::Text]),
                ..Default::default()
            }),
            ..Default::default()
        },
        reconnect: ReconnectPolicy::default(),
    }).await?;

    session.send_text("Hello from Vertex!").await?;
    drop(session);
    Ok(())
}
```

If you want reconnect-safe token refresh from Google Cloud Application Default
Credentials, enable the optional `vertex-auth` feature:

```toml
[dependencies]
gemini-live = { version = "0.1", features = ["vertex-auth"] }
tokio = { version = "1", features = ["full"] }
```

Then build the auth mode from ADC instead of injecting a static token:

```rust
use gemini_live::transport::Auth;

let auth = Auth::vertex_ai_application_default()?;
```

## Architecture

```
Session  →  Transport  →  Codec  →  Types / Audio / Errors
```

| Layer | Module | What it does |
|-------|--------|--------------|
| **Session** | `session.rs` | Connection lifecycle, auto-reconnect, typed send/receive |
| **Transport** | `transport.rs` | WebSocket + rustls, frame I/O |
| **Codec** | `codec.rs` | JSON ↔ Rust conversion; `ServerMessage` → `ServerEvent` decomposition |
| **Audio** | `audio.rs` | Zero-allocation PCM encoder, format constants |
| **Types** | `types/` | All wire-format structs and enums |
| **Errors** | `error.rs` | Layered error types per architectural layer |

Each layer's public API and design notes are documented in source code doc comments — start from `lib.rs` and drill into modules.

## Workspace Crates

This repository now has seven focused crates instead of treating the CLI as the
accidental home for reusable host logic:

| Crate | Role |
|-------|------|
| `gemini-live` | Wire-level Live API client |
| `gemini-live-runtime` | Reusable staged-setup and managed runtime orchestration |
| `gemini-live-harness` | Durable harness state, passive notifications, and shared host-tool execution policy |
| `gemini-live-tools` | Reusable low-coupling tool families such as workspace inspection/execution |
| `gemini-live-io` | Reusable desktop mic / speaker / screen adapters |
| `gemini-live-cli` | Interactive desktop TUI built on the shared crates above |
| `gemini-live-discord` | Single-guild Discord host for a shared text/voice Gemini Live agent |

## Audio Streaming

For convenience:

```rust
session.send_audio(&pcm_i16_le_bytes).await?;
```

For lower-overhead audio encoding with encoder buffer reuse:

```rust
let mut enc = AudioEncoder::new();
loop {
    let b64 = enc.encode_i16_le(&pcm_chunk);
    let msg = ClientMessage::RealtimeInput(RealtimeInput {
        audio: Some(Blob { data: b64.to_owned(), mime_type: "audio/pcm;rate=16000".into() }),
        video: None, text: None, activity_start: None, activity_end: None,
        audio_stream_end: None,
    });
    session.send_raw(msg).await?;
}
```

This avoids the extra base64-string allocation in `Session::send_audio`, but
the current full message-building and JSON-encoding path still performs owned
allocations; see [`docs/roadmap.md`](docs/roadmap.md).

## Tool Calling

```rust
while let Some(event) = session.next_event().await {
    if let ServerEvent::ToolCall(calls) = event {
        let responses = calls.iter().map(|call| {
            let result = handle_function(&call.name, &call.args);
            FunctionResponse {
                id: call.id.clone(),
                name: call.name.clone(),
                response: result,
            }
        }).collect();
        session.send_tool_response(responses).await?;
    }
}
```

## CLI

[![crates.io](https://img.shields.io/crates/v/gemini-live-cli.svg)](https://crates.io/crates/gemini-live-cli)

An interactive TUI client with microphone, speaker, screen sharing, and file sending support. See [`docs/cli.md`](docs/cli.md) for full usage.

### Install

Pre-built binary (Linux / macOS):

```bash
curl -fsSL https://raw.githubusercontent.com/jacoblincool/gemini-live-rs/main/install.sh | bash
```

Or via Cargo:

```bash
cargo install gemini-live-cli
```

Build without audio/screen features for a minimal binary:

```bash
cargo install gemini-live-cli --no-default-features
```

### Usage

```bash
export GEMINI_API_KEY=your-key
gemini-live
```

Override the model:

```bash
GEMINI_MODEL=models/gemini-2.5-flash-native-audio-latest gemini-live
```

Run the CLI against Vertex AI with a static bearer token:

```bash
LIVE_BACKEND=vertex \
VERTEX_LOCATION=us-central1 \
VERTEX_MODEL='projects/PROJECT_ID/locations/us-central1/publishers/google/models/MODEL_ID' \
VERTEX_AI_ACCESS_TOKEN="$(gcloud auth application-default print-access-token)" \
gemini-live
```

Run the CLI against Vertex AI with Application Default Credentials:

```bash
LIVE_BACKEND=vertex \
VERTEX_LOCATION=us-central1 \
VERTEX_MODEL='projects/PROJECT_ID/locations/us-central1/publishers/google/models/MODEL_ID' \
VERTEX_AUTH=adc \
cargo run -p gemini-live-cli --features vertex-auth
```

### Commands

| Input | Action |
|-------|--------|
| `hello` | Send text to the model |
| `@photo.jpg` | Send an image file |
| `@recording.wav` | Send a WAV audio file |
| `@photo.jpg describe this` | Send image + text together |
| `/mic` | Toggle microphone input (with AEC) |
| `/speak` | Toggle speaker output (with AEC) |
| `/share-screen list` | List available capture targets |
| `/share-screen <id> [interval]` | Start sharing a monitor or window |
| `/share-screen` | Stop screen sharing |
| `/system ...` | Stage / inspect / apply the system instruction |
| `/tools ...` | Stage / inspect / apply the Live tool profile |

The CLI also supports persistent named profiles via `--profile <name>` and a
`gemini-live config` subcommand that prints the resolved config-file path. See
[`docs/cli.md`](docs/cli.md) for the full command surface and current feature
flags.

### Self-update

```bash
gemini-live update
```

### Feature Flags

| Feature | Dependencies | Enables |
|---------|-------------|---------|
| `mic` (default) | `cpal`, `webrtc-audio-processing` | `/mic` command with AEC |
| `speak` (default) | `cpal`, `webrtc-audio-processing` | `/speak` command with AEC |
| `share-screen` (default) | `xcap`, `image` | `/share-screen` command |

## Documentation

| File | Purpose |
|------|---------|
| `docs/cli.md` | CLI usage, commands, feature flags, and architecture |
| `docs/protocol.md` | Upstream API reference (endpoints, lifecycle, VAD, session limits, model differences) |
| `docs/design.md` | Architecture decisions and performance goals |
| `docs/runtime-sequence.md` | Cross-layer sequence diagram for host/runtime/harness/session behavior |
| `docs/roadmap.md` | Planned work, known gaps, tech debt |
| `docs/testing.md` | Test inventory and instructions |

## License

MIT

---

## Author's Note

This repository is also an experiment in **how to design a set of guiding principles that enable AI agents to autonomously maintain a client library over time**.

Maintaining a client library is not a one-shot code generation problem — it is an ongoing engineering challenge. The library must track upstream API changes, keep documentation in sync, preserve backward compatibility, expand test coverage, and maintain design consistency. These are exactly the kinds of tasks where AI agents could contribute meaningfully, if given the right structure to work within.

The core idea behind this project is to explore what that structure looks like: which conventions, workflows, and constraints help an AI agent maintain stable, extensible, and high-quality output with minimal human intervention. The documentation architecture here — `AGENTS.md` for general principles, `protocol.md` for upstream facts, `design.md` for our decisions, `roadmap.md` for tracking gaps — is designed so that an agent can orient itself, identify what needs to change, and act accordingly.

If these principles can be defined clearly enough, an AI agent becomes more than a tool that executes instructions — it becomes a collaborator capable of participating in long-term maintenance.
