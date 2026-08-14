//! MCP server for livekit-agent-simulator — same 21 tools as the Python
//! `mcp_server.py` (P5). Every tool takes `project_root` except `guide`.
//!
//! Data-plane tools (guide, init/validate/export/list/convert, get_run_*,
//! compare_runs, list_runs, scenario_from_run) are fully wired to `lks-core`
//! ops. The execute-family (execute_scenario, execute_scenarios,
//! execute_scenario_dict, optimize_persona, preflight, web) needs the P2/P3.5
//! run infrastructure (lks-livekit room/dispatch/callers + run.rs) and
//! returns an explicit error until then — fail-loud, never silently missing
//! (AGENTS.md no-dead-features).

use std::path::Path;

use lks_core::ops;
use rmcp::handler::server::wrapper::Parameters;
use rmcp::{schemars, tool, tool_router};
use serde::Deserialize;

fn root(p: &str) -> &Path {
    Path::new(p)
}

fn internal_error(msg: impl Into<String>) -> rmcp::ErrorData {
    rmcp::ErrorData::internal_error(msg.into(), None)
}

/// Serve an ops JSON object as the tool result (serialized to a text block).
fn ok_json<T: serde::Serialize>(v: T) -> Result<String, rmcp::ErrorData> {
    serde_json::to_string(&v).map_err(|e| internal_error(format!("serialization failed: {e}")))
}

fn err_json(msg: &str) -> serde_json::Value {
    serde_json::json!({"error": msg})
}

// ---------------------------------------------------------------------------
// Params (byte-exact names/optionality/defaults per mcp_server.py)
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize, schemars::JsonSchema)]
pub struct GuideParams {}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
pub struct ProjectRootParams {
    /// Absolute path of the repo under test that contains (or will contain) `.agent-sim/`.
    #[schemars(description = "Absolute path of the repo under test containing .agent-sim/")]
    pub project_root: String,
}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
pub struct WebParams {
    pub project_root: String,
    #[serde(default)]
    pub run_id: Option<String>,
    #[serde(default = "default_host")]
    pub host: String,
    #[serde(default = "default_port")]
    pub port: u16,
    #[serde(default = "default_true")]
    pub open_browser: bool,
}

fn default_host() -> String {
    "127.0.0.1".to_string()
}
fn default_port() -> u16 {
    8765
}
fn default_true() -> bool {
    true
}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
pub struct PreflightParams {
    pub project_root: String,
    #[serde(default = "default_true")]
    pub connectivity: bool,
    pub profile: Option<String>,
}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
pub struct ScenarioIdParams {
    pub project_root: String,
    pub scenario_id: String,
}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
pub struct ForceScenarioIdParams {
    pub project_root: String,
    pub scenario_id: String,
    #[serde(default)]
    pub force: bool,
}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
pub struct ExecuteParams {
    pub project_root: String,
    pub scenario_id: String,
    #[serde(default = "default_one")]
    pub repeat: i64,
    pub pass_at_k: Option<i64>,
    pub run_name: Option<String>,
    pub agent_name: Option<String>,
    pub optimized: Option<String>,
    pub profile: Option<String>,
}

fn default_one() -> i64 {
    1
}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
pub struct OptimizeParams {
    pub project_root: String,
    pub scenario_ids: Vec<String>,
    pub held_out: Option<String>,
    #[serde(default = "default_four")]
    pub candidates: i64,
    #[serde(default = "default_six")]
    pub max_candidates: i64,
    #[serde(default)]
    pub strict_judge: bool,
    #[serde(default = "default_one")]
    pub repeat: i64,
    pub pass_at_k: Option<i64>,
    pub agent_name: Option<String>,
    pub name: Option<String>,
    pub profile: Option<String>,
}

fn default_four() -> i64 {
    4
}
fn default_six() -> i64 {
    6
}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
pub struct ExecuteScenariosParams {
    pub project_root: String,
    pub scenario_ids: Option<Vec<String>>,
    pub tag: Option<String>,
    #[serde(default)]
    pub strict_judge: bool,
    #[serde(default = "default_true")]
    pub write_report: bool,
    #[serde(default = "default_one")]
    pub repeat: i64,
    pub pass_at_k: Option<i64>,
    #[serde(default = "default_one")]
    pub parallel: i64,
    #[serde(default)]
    pub wait_s: f64,
    pub agent_name: Option<String>,
    pub profile: Option<String>,
}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
pub struct ExecuteDictParams {
    pub project_root: String,
    pub scenario: serde_json::Value,
    pub run_name: Option<String>,
    pub agent_name: Option<String>,
    pub profile: Option<String>,
}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
pub struct ScenarioFromRunParams {
    pub project_root: String,
    pub run_id: String,
    pub scenario_id: Option<String>,
    #[serde(default)]
    pub write: bool,
}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
pub struct RunIdParams {
    pub project_root: String,
    pub run_id: String,
}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
pub struct GetRunLogParams {
    pub project_root: String,
    pub run_id: String,
    pub kind: Option<String>,
    pub turn: Option<i64>,
    pub source: Option<String>,
    pub since_mono_ms: Option<i64>,
    #[serde(default = "default_200")]
    pub limit: i64,
}

fn default_200() -> i64 {
    200
}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
pub struct CompareRunsParams {
    pub project_root: String,
    pub run_id_a: String,
    pub run_id_b: String,
    #[serde(default)]
    pub baseline: bool,
    #[serde(default = "default_1500")]
    pub max_ttfw_regression_ms: f64,
    #[serde(default = "default_2000")]
    pub max_turn_p95_regression_ms: f64,
    #[serde(default = "default_30000")]
    pub max_duration_regression_ms: f64,
    #[serde(default)]
    pub max_barge_recovery_drop: f64,
}

fn default_1500() -> f64 {
    1500.0
}
fn default_2000() -> f64 {
    2000.0
}
fn default_30000() -> f64 {
    30000.0
}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
pub struct ListRunsParams {
    pub project_root: String,
    #[serde(default = "default_20")]
    pub limit: i64,
    pub scenario_id: Option<String>,
}

fn default_20() -> i64 {
    20
}

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct SimServer;

#[tool_router(server_handler)]
impl SimServer {
    /// Setup and ops guide for coding agents (markdown text). Read before first setup if unfamiliar.
    #[tool(
        description = "Setup and ops guide for coding agents (markdown text). Read before first setup if unfamiliar."
    )]
    fn guide(
        &self,
        Parameters(GuideParams {}): Parameters<GuideParams>,
    ) -> Result<String, rmcp::ErrorData> {
        ok_json(
            serde_json::to_value(ops::guide().map_err(|e| internal_error(e.0))?)
                .unwrap_or_else(|_| err_json("guide serialization failed")),
        )
    }

    /// Start local report player (audio + transcript sync). Returns URL; server runs in background until process exits.
    #[tool(
        description = "Start local report player (audio + transcript sync). Returns URL; server runs in background until process exits."
    )]
    async fn web(&self, Parameters(p): Parameters<WebParams>) -> Result<String, rmcp::ErrorData> {
        let result = lks_web::start_web(
            root(&p.project_root),
            &p.host,
            p.port,
            p.run_id.as_deref(),
            p.open_browser,
        )
        .await
        .map_err(internal_error)?;
        ok_json(result)
    }

    /// Scaffold `.agent-sim/` (config.yaml + smoke scenario) in the target repo and gitignore it.
    #[tool(
        description = "Scaffold .agent-sim/ (config.yaml + smoke scenario) in the target repo and gitignore it."
    )]
    fn init_project(
        &self,
        Parameters(p): Parameters<ProjectRootParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let r = ops::op_init_project(root(&p.project_root)).map_err(|e| internal_error(e.0))?;
        ok_json(r)
    }

    /// Check config + folders + optional LiveKit API connectivity without running a scenario.
    ///
    /// ``profile`` (optional) selects a named ``simulator.profiles.<name>`` caller
    /// profile for the check; omitted → the legacy flat ``simulator:`` block.
    #[tool(
        description = "Check config + folders + optional LiveKit API connectivity without running a scenario."
    )]
    async fn preflight(
        &self,
        Parameters(p): Parameters<PreflightParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let r = lks_livekit::preflight::op_preflight(
            root(&p.project_root),
            p.connectivity,
            p.profile.as_deref(),
        )
        .await
        .map_err(|e| internal_error(e.0))?;
        ok_json(r)
    }

    /// List all scenarios in `.agent-sim/scenarios/*.yaml` (legacy `*.jsonl` still read) with id, tags, and validity.
    #[tool(
        description = "List all scenarios in .agent-sim/scenarios/*.yaml (legacy *.jsonl still read) with id, tags, and validity."
    )]
    fn list_scenarios(
        &self,
        Parameters(p): Parameters<ProjectRootParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let r = ops::op_list_scenarios(root(&p.project_root)).map_err(|e| internal_error(e.0))?;
        ok_json(r)
    }

    /// List registered verify plugins and local `.agent-sim/plugins/*.py` modules.
    #[tool(
        description = "List registered verify plugins and local .agent-sim/plugins/*.py modules."
    )]
    fn list_plugins(
        &self,
        Parameters(p): Parameters<ProjectRootParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let r = ops::op_list_plugins(root(&p.project_root)).map_err(|e| internal_error(e.0))?;
        ok_json(r)
    }

    /// List built-in room_pcm cues (noise.* + voice.* speech), target `.agent-sim/cues/` overrides, and config aliases.
    #[tool(
        description = "List built-in room_pcm cues (noise.* + voice.* speech), target .agent-sim/cues/ overrides, and config aliases."
    )]
    fn list_cues(
        &self,
        Parameters(p): Parameters<ProjectRootParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let r = ops::op_list_cues(root(&p.project_root)).map_err(|e| internal_error(e.0))?;
        ok_json(r)
    }

    /// Validate a scenario file: schema, required Persona brief, PassCriteria lint.
    #[tool(
        description = "Validate a scenario file: schema, required Persona brief, PassCriteria lint."
    )]
    fn validate_scenario(
        &self,
        Parameters(p): Parameters<ScenarioIdParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let r = ops::op_validate_scenario(root(&p.project_root), &p.scenario_id)
            .map_err(|e| internal_error(e.0))?;
        ok_json(r)
    }

    /// Export a parsed scenario (Persona, Execute, Dispatch flag, PassCriteria) as JSON.
    #[tool(
        description = "Export a parsed scenario (Persona, Execute, Dispatch flag, PassCriteria) as JSON."
    )]
    fn export_scenario(
        &self,
        Parameters(p): Parameters<ScenarioIdParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let r = ops::op_export_scenario(root(&p.project_root), &p.scenario_id)
            .map_err(|e| internal_error(e.0))?;
        ok_json(r)
    }

    /// Scaffold `.agent-sim/scenarios/<id>.yaml` with `#` guide comments + example sections.
    #[tool(
        description = "Scaffold .agent-sim/scenarios/<id>.yaml with # guide comments + example sections."
    )]
    fn init_scenario(
        &self,
        Parameters(p): Parameters<ForceScenarioIdParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let r = ops::op_init_scenario(root(&p.project_root), &p.scenario_id, p.force)
            .map_err(|e| internal_error(e.0))?;
        ok_json(r)
    }

    /// Convert a legacy `.jsonl` scenario to `.yaml` (keeps the .jsonl, idempotent).
    #[tool(
        description = "Convert a legacy .jsonl scenario to .yaml (keeps the .jsonl, idempotent)."
    )]
    fn convert_scenario(
        &self,
        Parameters(p): Parameters<ForceScenarioIdParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let r = ops::op_convert_scenario(root(&p.project_root), &p.scenario_id, p.force)
            .map_err(internal_error)?;
        ok_json(r)
    }

    /// Validate then execute one scenario from `.agent-sim/scenarios/*.yaml` (legacy `*.jsonl` still read).
    #[tool(
        description = "Validate then execute one scenario from .agent-sim/scenarios/*.yaml (legacy *.jsonl still read)."
    )]
    async fn execute_scenario(
        &self,
        Parameters(p): Parameters<ExecuteParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let opts = lks_livekit::run::ExecuteOptions {
            run_name: p.run_name.clone(),
            repeat: p.repeat,
            pass_at_k: p.pass_at_k,
            agent_name: p.agent_name.clone(),
            optimized: p.optimized.clone(),
            profile: p.profile.clone(),
        };
        let result =
            lks_livekit::run::execute_scenario(root(&p.project_root), &p.scenario_id, &opts)
                .await
                .map_err(|e| internal_error(e.to_string()))?;
        ok_json(result)
    }

    /// Run the persona-prompt optimizer over a dataset (live benchmark loop).
    #[tool(description = "Run the persona-prompt optimizer over a dataset (live benchmark loop).")]
    async fn optimize_persona(
        &self,
        Parameters(p): Parameters<OptimizeParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let opts = lks_livekit::ops_execute::OptimizeOptions {
            scenario_ids: p.scenario_ids.clone(),
            held_out: p.held_out.clone(),
            candidates: p.candidates,
            max_candidates: p.max_candidates,
            strict_judge: p.strict_judge,
            repeat: p.repeat,
            pass_at_k: p.pass_at_k,
            agent_name: p.agent_name.clone(),
            name: p.name.clone(),
            profile: p.profile.clone(),
        };
        let result = lks_livekit::ops_execute::op_optimize_persona(root(&p.project_root), &opts)
            .await
            .map_err(|e| internal_error(e.to_string()))?;
        ok_json(result)
    }

    /// Execute multiple scenarios; returns suite matrix + CI gate (hard: assert/script/status).
    #[tool(
        description = "Execute multiple scenarios; returns suite matrix + CI gate (hard: assert/script/status)."
    )]
    async fn execute_scenarios(
        &self,
        Parameters(p): Parameters<ExecuteScenariosParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let opts = lks_livekit::ops_execute::SuiteOptions {
            scenario_ids: p.scenario_ids.clone(),
            tag: p.tag.clone(),
            strict_judge: p.strict_judge,
            write_report: p.write_report,
            repeat: p.repeat,
            pass_at_k: p.pass_at_k,
            parallel: p.parallel,
            wait_s: p.wait_s,
            agent_name: p.agent_name.clone(),
            profile: p.profile.clone(),
        };
        let result = lks_livekit::ops_execute::op_execute_scenarios(root(&p.project_root), &opts)
            .await
            .map_err(|e| internal_error(e.to_string()))?;
        ok_json(result)
    }

    /// Validate then run an in-memory scenario dict (no JSONL file). Same fields as export_scenario.
    #[tool(
        description = "Validate then run an in-memory scenario dict (no JSONL file). Same fields as export_scenario."
    )]
    async fn execute_scenario_dict(
        &self,
        Parameters(p): Parameters<ExecuteDictParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let scenario = match p.scenario.as_object() {
            Some(m) => m.clone(),
            None => {
                return ok_json(err_json("scenario must be an object"));
            }
        };
        let result = lks_livekit::ops_execute::op_execute_scenario_dict(
            root(&p.project_root),
            &scenario,
            p.run_name.as_deref(),
            p.agent_name.as_deref(),
            p.profile.as_deref(),
        )
        .await
        .map_err(|e| internal_error(e.to_string()))?;
        ok_json(result)
    }

    /// Promote a finished run into a draft scenario YAML (fail → golden).
    #[tool(description = "Promote a finished run into a draft scenario YAML (fail → golden).")]
    fn scenario_from_run(
        &self,
        Parameters(p): Parameters<ScenarioFromRunParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let r = ops::op_scenario_from_run(
            root(&p.project_root),
            &p.run_id,
            p.scenario_id.as_deref(),
            p.write,
        )
        .map_err(internal_error)?;
        ok_json(r)
    }

    /// Status of a run from SQLite: running / done / failed, turn count, duration.
    #[tool(
        description = "Status of a run from SQLite: running / done / failed, turn count, duration."
    )]
    fn get_run_status(
        &self,
        Parameters(p): Parameters<RunIdParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let r = ops::op_get_run_status(root(&p.project_root), &p.run_id)
            .map_err(|e| internal_error(e.0))?;
        ok_json(r)
    }

    /// Read events.jsonl with filters. `kind` supports trailing `*` prefix match (e.g. `tool.*`).
    #[tool(
        description = "Read events.jsonl with filters. `kind` supports trailing `*` prefix match (e.g. `tool.*`)."
    )]
    fn get_run_log(
        &self,
        Parameters(p): Parameters<GetRunLogParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let r = ops::op_get_run_log(
            root(&p.project_root),
            &p.run_id,
            p.kind.as_deref(),
            p.turn,
            p.source.as_deref(),
            p.since_mono_ms,
            p.limit.max(0) as usize,
        )
        .map_err(|e| internal_error(e.0))?;
        ok_json(r)
    }

    /// Full report: summary (incl. caller.behavior_summary, script/assert verify), judge, suspicious turns, paths.
    #[tool(
        description = "Full report: summary (incl. caller.behavior_summary, script/assert verify), judge, suspicious turns, paths."
    )]
    fn get_run_report(
        &self,
        Parameters(p): Parameters<RunIdParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let r = ops::op_get_run_report(root(&p.project_root), &p.run_id)
            .map_err(|e| internal_error(e.0))?;
        ok_json(r)
    }

    /// Diff two runs. If baseline=True, run_id_a is golden and gate hard-fails regressions.
    #[tool(
        description = "Diff two runs. If baseline=True, run_id_a is golden and gate hard-fails regressions."
    )]
    fn compare_runs(
        &self,
        Parameters(p): Parameters<CompareRunsParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let r = if p.baseline {
            ops::op_compare_runs_with_baseline(
                root(&p.project_root),
                &p.run_id_a,
                &p.run_id_b,
                p.max_ttfw_regression_ms,
                p.max_turn_p95_regression_ms,
                p.max_duration_regression_ms,
                p.max_barge_recovery_drop,
            )
            .map_err(|e| internal_error(e.0))?
        } else {
            ops::op_compare_runs(root(&p.project_root), &p.run_id_a, &p.run_id_b)
                .map_err(|e| internal_error(e.0))?
        };
        ok_json(r)
    }

    /// Run history from SQLite, newest first. Optionally filter by scenario_id.
    #[tool(
        description = "Run history from SQLite, newest first. Optionally filter by scenario_id."
    )]
    fn list_runs(
        &self,
        Parameters(p): Parameters<ListRunsParams>,
    ) -> Result<String, rmcp::ErrorData> {
        let r = ops::op_list_runs(
            root(&p.project_root),
            p.limit.max(0),
            p.scenario_id.as_deref(),
        )
        .map_err(|e| internal_error(e.0))?;
        ok_json(r)
    }
}

/// Start the stdio MCP server (mirrors Python's `mcp.run()` / `lks mcp`).
pub async fn serve_stdio() -> anyhow::Result<()> {
    use rmcp::ServiceExt;
    let service = SimServer.serve(rmcp::transport::stdio()).await?;
    service.waiting().await?;
    Ok(())
}
