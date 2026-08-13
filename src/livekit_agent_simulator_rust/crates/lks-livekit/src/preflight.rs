//! Preflight connectivity check (P2 slice): verify the LiveKit server API
//! credentials by listing rooms (port of `preflight.py` livekit.api check).

use lks_core::config::SimConfig;
use lks_core::errors::RunError;

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
            RunError(format!(
                "Cannot reach LiveKit server API with the configured credentials: {e}"
            ))
        })
}
