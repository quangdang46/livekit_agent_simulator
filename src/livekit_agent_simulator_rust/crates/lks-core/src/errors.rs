//! Core error types mirroring the Python package's exceptions.
//!
//! Python surface (config.py / scenario.py / run_orchestrator.py):
//!   ConfigError    — config.yaml load/validation failures (fail-fast _require)
//!   ScenarioError  — scenario parse/validate failures
//!   RunError       — runtime run failures
//!
//! Error message strings are BYTE-EXACT contracts (plan Invariant I3 / Appendix D
//! §1) — the CLI/MCP surface and golden tests depend on them. Do not reword.

use thiserror::Error;

/// Config load/validation error (mirrors Python `ConfigError`).
#[derive(Debug, Error)]
#[error("{0}")]
pub struct ConfigError(pub String);

/// Scenario parse/validate error (mirrors Python `ScenarioError`).
#[derive(Debug, Error)]
#[error("{0}")]
pub struct ScenarioError(pub String);

/// Runtime run error (mirrors Python `RunError` / raw exceptions).
#[derive(Debug, Error)]
#[error("{0}")]
pub struct RunError(pub String);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn config_error_prints_message() {
        let e = ConfigError("Missing `livekit.api_key` in .agent-sim/config.yaml.".into());
        assert_eq!(
            e.to_string(),
            "Missing `livekit.api_key` in .agent-sim/config.yaml."
        );
    }

    #[test]
    fn scenario_error_prints_message() {
        let e = ScenarioError("{path}: empty scenario file".into());
        assert_eq!(e.to_string(), "{path}: empty scenario file");
    }
}
