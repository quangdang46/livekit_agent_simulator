//! Generate prost types from the livekit agent_session proto.
//!
//! The proto source is the official livekit/protocol `protobufs/agent/
//! livekit_agent_session.proto` (fetched 2026-08-13), matching the Python
//! `livekit.protocol.agent_pb.agent_session` descriptor. The published Rust
//! `livekit-protocol` crate does NOT include AgentSessionMessage (verified),
//! so this crate is the source of those types.
//!
//! Proto files live at `proto/` (import path `logger/options.proto` requires
//! the `proto/` root as the include dir).

fn main() {
    // Use a vendored prebuilt protoc binary (no system protoc, no C++ compile).
    let protoc = protoc_bin_vendored::protoc_bin_path().expect("vendored protoc");
    let protos = ["proto/agent/livekit_agent_session.proto"];
    prost_build::Config::new()
        .protoc_executable(&protoc)
        .compile_protos(&protos, &["proto/"])
        .expect("prost-build compile");
    println!("cargo:rerun-if-changed=proto/agent/livekit_agent_session.proto");
    println!("cargo:rerun-if-changed=proto/logger/options.proto");
}
