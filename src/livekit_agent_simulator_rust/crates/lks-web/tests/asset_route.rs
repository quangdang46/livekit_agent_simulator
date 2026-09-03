//! Regression: /assets/<nested> served (axum {*name} wildcard; the old
//! {name} single-segment route 404'd Vite chunk assets → blank player).
use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use lks_web::{router, WebServer};
use std::sync::Arc;
use tower::ServiceExt;

#[tokio::test]
async fn assets_nested_route_serves_js() {
    let dir = tempfile::tempdir().unwrap();
    let dot = dir.path().join(".agent-sim");
    std::fs::create_dir_all(&dot).unwrap();
    std::fs::create_dir_all(dot.join("reports")).unwrap();
    std::fs::write(
        dot.join("config.yaml"),
        "livekit:\n  url: wss://example.livekit.cloud\n  api_key: test-key\n  api_secret: test-secret\n  agent_name: test-agent\nsimulator:\n  provider: openai\n  api_key: sk-test-key-1234567890\n",
    ).unwrap();
    let server = Arc::new(WebServer::new(dir.path()));
    // The repo web/dist is on disk — resolve player_dir to it.
    let pdir = server.player_dir.clone();
    eprintln!("DBG player_dir: {:?}", pdir);
    if !pdir.join("assets").is_dir() {
        eprintln!("skip: no web/dist assets on this checkout");
        return;
    }
    let app = router(server);
    let js = std::fs::read_dir(pdir.join("assets"))
        .unwrap()
        .flatten()
        .find(|e| e.path().extension().map(|x| x == "js").unwrap_or(false))
        .unwrap();
    let name = js.file_name().to_string_lossy().into_owned();
    let resp = app
        .oneshot(
            Request::builder()
                .uri(format!("/assets/{name}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        StatusCode::OK,
        "asset {name} must be 200, got {}",
        resp.status()
    );
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    assert!(bytes.len() > 1000, "asset body looks empty");
}
