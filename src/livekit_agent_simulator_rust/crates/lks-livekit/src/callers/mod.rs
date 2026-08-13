//! Caller bridges — the simulated human speaking with the agent (P2+).

pub mod gemini;
pub mod openai;

pub use gemini::GeminiCallerBridge;
pub use openai::OpenAiCallerBridge;
