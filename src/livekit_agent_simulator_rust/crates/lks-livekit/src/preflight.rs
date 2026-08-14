//! Preflight connectivity check (port of `preflight.py` full surface): the
//! lks-core `op_preflight_core` checks (config/url/timezone/folders/api_key/
//! telephony) plus the `livekit.api` list_rooms check gated on connectivity.

use lks_core::config::SimConfig;
use lks_core::errors::RunError;
use serde_json::{json, Map, Value as Json};

/// Check LiveKit API connectivity: list_rooms must succeed.
pub async fn check_livekit_api(cfg: &SimConfig) -> Result<(), RunError> {
    let api_host = cfg
        .livekit
        .url
        .replace("wss://", "https://")
        .replace("ws://", "https://");
    let service = livekit_api::services::room::RoomClient::with_api_key(
        &api_host,
        &cfg.livekit.api_key,
        &cfg.livekit.api_secret,
    );
    service
        .list_rooms(Vec::new())
        .await
        .map(|_| ())
        .map_err(|e| {
            // Python: "Cannot reach LiveKit server API with the configured credentials: {Type}: {msg}"
            RunError(format!(
                "Cannot reach LiveKit server API with the configured credentials: {}: {e}",
                livekit_api_error_type(&e)
            ))
        })
}

fn livekit_api_error_type(_e: &livekit_api::services::ServiceError) -> &'static str {
    // Rust livekit-api has no stable type name; approximate with the error
    // variant's display prefix. Python shows the exception class name.
    "LiveKitAPIError"
}

/// Full preflight (port of `preflight.py:run_preflight`): core checks from
/// lks-core plus the async `livekit.api` connectivity check when
/// `connectivity` is true and no prior check failed.
pub async fn op_preflight(
    project_root: &std::path::Path,
    connectivity: bool,
    profile: Option<&str>,
) -> Result<Map<String, Json>, lks_core::errors::ConfigError> {
    let (mut m, cfg) = lks_core::ops::op_preflight_core(project_root, profile)?;
    if connectivity {
        if let Some(cfg) = cfg {
            let still_ok = m.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
            if still_ok {
                match check_livekit_api(&cfg).await {
                    Ok(()) => {
                        let mut c = Map::new();
                        c.insert("name".into(), json!("livekit.api"));
                        c.insert("status".into(), json!("pass"));
                        c.insert("detail".into(), json!("list_rooms OK"));
                        m.get_mut("checks")
                            .and_then(|v| v.as_array_mut())
                            .unwrap()
                            .push(Json::Object(c));
                    }
                    Err(e) => {
                        let mut c = Map::new();
                        c.insert("name".into(), json!("livekit.api"));
                        c.insert("status".into(), json!("fail"));
                        c.insert(
                            "detail".into(),
                            json!(e.0), // check_livekit_api already formats the full message
                        );
                        m.get_mut("checks")
                            .and_then(|v| v.as_array_mut())
                            .unwrap()
                            .push(Json::Object(c));
                        m.insert("ok".into(), json!(false));
                    }
                }
            }
        }
    }
    Ok(m)
}
