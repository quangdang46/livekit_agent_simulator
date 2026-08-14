//! REST API server (`lks serve`) — port of `web/api.py`: the same public ops
//! as CLI/MCP over HTTP/JSON under `/api/v1`. 10-route set with Python-identical
//! error mapping and body shapes.

use std::path::Path;
use std::sync::Arc;

use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{json, Value};

use lks_core::ops;

pub const DEFAULT_HOST: &str = "127.0.0.1";
pub const DEFAULT_PORT: u16 = 8787;
pub const PREFIX: &str = "/api/v1";

/// REST API server state.
#[derive(Clone)]
pub struct ApiServer {
    pub project_root: PathBuf,
}

impl ApiServer {
    pub fn new(project_root: &Path) -> Self {
        Self {
            project_root: project_root.to_path_buf(),
        }
    }
}

type PathBuf = std::path::PathBuf;

/// JSON helper: `{"error": msg}` (no indent, ensure_ascii=false).
fn err_body(msg: &str) -> Json<Value> {
    Json(json!({"error": msg}))
}

/// Run an ops fn that may be sync or async (Python `asyncio.run` per-request
/// equivalent — the API runs each request on a fresh current-thread runtime,
/// spawned off the axum worker so block_on never nests).
async fn run_op_async<F, Fut>(f: F) -> Result<Value, String>
where
    F: FnOnce() -> Fut + Send + 'static,
    Fut: std::future::Future<Output = Result<Value, String>> + Send,
{
    tokio::task::spawn_blocking(move || {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|e| e.to_string())?;
        rt.block_on(f())
    })
    .await
    .map_err(|e| format!("api task panicked: {e}"))?
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

async fn health(State(s): State<Arc<ApiServer>>) -> (StatusCode, Json<Value>) {
    (
        StatusCode::OK,
        Json(json!({"ok": true, "root": s.project_root.display().to_string()})),
    )
}

async fn list_runs(
    State(s): State<Arc<ApiServer>>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    match ops::op_list_runs(&s.project_root, 20, None) {
        Ok(rows) => Ok(Json(Value::Array(
            rows.into_iter().map(Value::Object).collect(),
        ))),
        Err(e) => Err((StatusCode::NOT_FOUND, err_body(&e.0))),
    }
}

async fn run_status(
    State(s): State<Arc<ApiServer>>,
    axum::extract::Path(run_id): axum::extract::Path<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    if run_id.is_empty() {
        return Err((StatusCode::BAD_REQUEST, err_body("missing run id")));
    }
    match ops::op_get_run_status(&s.project_root, &run_id) {
        Ok(m) => Ok(Json(Value::Object(m))),
        Err(e) => Err((StatusCode::NOT_FOUND, err_body(&e.0))),
    }
}

async fn run_report(
    State(s): State<Arc<ApiServer>>,
    axum::extract::Path(run_id): axum::extract::Path<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    match ops::op_get_run_report(&s.project_root, &run_id) {
        Ok(m) => Ok(Json(Value::Object(m))),
        Err(e) => Err((StatusCode::NOT_FOUND, err_body(&e.0))),
    }
}

async fn list_scenarios(
    State(s): State<Arc<ApiServer>>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    match ops::op_list_scenarios(&s.project_root) {
        Ok(rows) => Ok(Json(Value::Array(
            rows.into_iter().map(Value::Object).collect(),
        ))),
        Err(e) => Err((StatusCode::NOT_FOUND, err_body(&e.0))),
    }
}

async fn export_scenario(
    State(s): State<Arc<ApiServer>>,
    axum::extract::Path(scenario_id): axum::extract::Path<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    if scenario_id.is_empty() {
        return Err((StatusCode::BAD_REQUEST, err_body("missing scenario id")));
    }
    match ops::op_export_scenario(&s.project_root, &scenario_id) {
        Ok(m) => Ok(Json(Value::Object(m))),
        Err(e) => Err((StatusCode::NOT_FOUND, err_body(&e.0))),
    }
}

async fn validate_scenario(
    State(s): State<Arc<ApiServer>>,
    Json(body): Json<Value>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let sid = body.get("scenario_id").and_then(|v| v.as_str());
    let Some(sid) = sid else {
        return Err((
            StatusCode::BAD_REQUEST,
            err_body("validate needs scenario_id"),
        ));
    };
    match ops::op_validate_scenario(&s.project_root, sid) {
        Ok(m) => Ok(Json(Value::Object(m))),
        Err(e) => Err((StatusCode::NOT_FOUND, err_body(&e.0))),
    }
}

async fn execute_scenario(
    State(s): State<Arc<ApiServer>>,
    Json(body): Json<Value>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let sid = body.get("scenario_id").and_then(|v| v.as_str());
    let Some(sid) = sid else {
        return Err((
            StatusCode::BAD_REQUEST,
            err_body("execute needs scenario_id"),
        ));
    };
    // int(repeat): float truncates toward zero, "3" works, "abc" → 400.
    let repeat: i64 = match body.get("repeat") {
        None | Some(Value::Null) => 1,
        Some(Value::Number(n)) => n.as_i64().unwrap_or(1),
        Some(Value::String(s)) => match s.trim().parse::<i64>() {
            Ok(n) => n,
            Err(_) => {
                return Err((
                    StatusCode::BAD_REQUEST,
                    err_body(&format!("invalid literal for int(): '{s}'")),
                ));
            }
        },
        Some(Value::Bool(_)) | Some(Value::Array(_)) | Some(Value::Object(_)) => {
            return Err((
                StatusCode::BAD_REQUEST,
                err_body("invalid literal for int()"),
            ));
        }
    };
    let pass_at_k = body.get("pass_at_k").and_then(|v| v.as_i64());
    let run_name = body.get("run_name").and_then(|v| v.as_str());
    let agent_name = body.get("agent_name").and_then(|v| v.as_str());
    let profile = body.get("profile").and_then(|v| v.as_str());
    let opts = lks_livekit::run::ExecuteOptions {
        run_name: run_name.map(String::from),
        repeat,
        pass_at_k,
        agent_name: agent_name.map(String::from),
        profile: profile.map(String::from),
        ..Default::default()
    };
    let root = s.project_root.clone();
    let sid = sid.to_string();
    let result = run_op_async(move || {
        let root = root.clone();
        let sid = sid.clone();
        let opts = opts.clone();
        async move {
            lks_livekit::run::execute_scenario(&root, &sid, &opts)
                .await
                .map(Value::Object)
                .map_err(|e| e.to_string())
        }
    })
    .await;
    match result {
        Ok(v) => Ok(Json(v)),
        Err(e) => Err((StatusCode::INTERNAL_SERVER_ERROR, err_body(&e))),
    }
}

async fn preflight(
    State(s): State<Arc<ApiServer>>,
    Json(body): Json<Value>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    // bool(connectivity): only JSON false/0/""/null are falsy; string "false" is truthy.
    let connectivity = match body.get("connectivity") {
        None | Some(Value::Null) => true,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_i64().unwrap_or(0) != 0,
        Some(Value::String(s)) => !s.is_empty(),
        Some(_) => true,
    };
    let profile = body.get("profile").and_then(|v| v.as_str());
    let root = s.project_root.clone();
    let profile = profile.map(String::from);
    let result = run_op_async(move || {
        let root = root.clone();
        let profile = profile.clone();
        async move {
            lks_livekit::preflight::op_preflight(&root, connectivity, profile.as_deref())
                .await
                .map(Value::Object)
                .map_err(|e| e.0)
        }
    })
    .await;
    match result {
        Ok(v) => Ok(Json(v)),
        Err(e) => Err((StatusCode::INTERNAL_SERVER_ERROR, err_body(&e))),
    }
}

/// Build the REST API router (under /api/v1).
pub fn api_router(server: Arc<ApiServer>) -> Router {
    Router::new()
        .route("/api/v1/health", get(health))
        .route("/api/v1/runs", get(list_runs))
        .route("/api/v1/runs/{run_id}", get(run_status))
        .route("/api/v1/runs/{run_id}/report", get(run_report))
        .route("/api/v1/scenarios", get(list_scenarios))
        .route("/api/v1/scenarios/{scenario_id}", get(export_scenario))
        .route("/api/v1/validate", post(validate_scenario))
        .route("/api/v1/execute", post(execute_scenario))
        .route("/api/v1/preflight", post(preflight))
        .with_state(server)
}

/// Start the REST API server on host:port; returns the bound address.
pub async fn serve_api(
    server: Arc<ApiServer>,
    host: &str,
    port: u16,
) -> anyhow::Result<std::net::SocketAddr> {
    let app = api_router(server);
    let listener = tokio::net::TcpListener::bind((host, port)).await?;
    let addr = listener.local_addr()?;
    axum::serve(listener, app).await?;
    Ok(addr)
}
