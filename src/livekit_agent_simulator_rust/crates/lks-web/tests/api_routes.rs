//! REST API parity — port of `tests/test_rest_api.py` vectors: /api/v1 route
//! shapes, error mapping (400/404/500), int(repeat) coercion, bool(connectivity).

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use lks_web::api_server::{api_router, ApiServer};
use std::sync::Arc;
use tower::ServiceExt;

fn tmp_root() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    let dot = dir.path().join(".agent-sim");
    std::fs::create_dir_all(&dot).unwrap();
    std::fs::create_dir_all(dot.join("scenarios")).unwrap();
    std::fs::create_dir_all(dot.join("reports")).unwrap();
    std::fs::write(
        dot.join("config.yaml"),
        "livekit:\n  url: wss://example.livekit.cloud\n  api_key: test-key\n  api_secret: test-secret\n  agent_name: test-agent\nsimulator:\n  provider: openai\n  api_key: sk-test-key-1234567890\n",
    )
    .unwrap();
    dir
}

async fn body_json(resp: axum::response::Response) -> serde_json::Value {
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    serde_json::from_slice(&bytes).unwrap()
}

#[tokio::test]
async fn health_no_version_key() {
    let dir = tmp_root();
    let app = api_router(Arc::new(ApiServer::new(dir.path())));
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/health")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let v = body_json(resp).await;
    assert_eq!(v.get("ok").and_then(|x| x.as_bool()), Some(true));
    assert!(v.get("root").is_some());
    assert!(v.get("version").is_none(), "no version key: {v}");
}

#[tokio::test]
async fn unknown_route_404() {
    let dir = tmp_root();
    let app = api_router(Arc::new(ApiServer::new(dir.path())));
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/bogus")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn execute_missing_scenario_id_400() {
    let dir = tmp_root();
    let app = api_router(Arc::new(ApiServer::new(dir.path())));
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/execute")
                .header("Content-Type", "application/json")
                .body(Body::from("{}"))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    let v = body_json(resp).await;
    assert_eq!(
        v.get("error").and_then(|x| x.as_str()),
        Some("execute needs scenario_id")
    );
}

#[tokio::test]
async fn execute_bad_repeat_400() {
    let dir = tmp_root();
    let app = api_router(Arc::new(ApiServer::new(dir.path())));
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/execute")
                .header("Content-Type", "application/json")
                .body(Body::from(r#"{"scenario_id":"x","repeat":"abc"}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    let v = body_json(resp).await;
    assert!(
        v.get("error")
            .and_then(|x| x.as_str())
            .unwrap_or("")
            .contains("invalid literal"),
        "{v}"
    );
}

#[tokio::test]
async fn validate_missing_scenario_id_400() {
    let dir = tmp_root();
    let app = api_router(Arc::new(ApiServer::new(dir.path())));
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/validate")
                .header("Content-Type", "application/json")
                .body(Body::from("{}"))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    let v = body_json(resp).await;
    assert_eq!(
        v.get("error").and_then(|x| x.as_str()),
        Some("validate needs scenario_id")
    );
}

#[tokio::test]
async fn preflight_connectivity_false_truthiness() {
    let dir = tmp_root();
    let app = api_router(Arc::new(ApiServer::new(dir.path())));
    // bool("false") is truthy in Python — connectivity stays true; but with a
    // valid config the check runs list_rooms → error (no server). We assert the
    // route responds 200 with checks rather than 400.
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/preflight")
                .header("Content-Type", "application/json")
                .body(Body::from(r#"{"connectivity":"false"}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let v = body_json(resp).await;
    assert!(v.get("checks").and_then(|x| x.as_array()).is_some(), "{v}");
}

#[tokio::test]
async fn runs_and_scenarios_lists() {
    let dir = tmp_root();
    // Write a scenario file so list works
    std::fs::write(
        dir.path().join(".agent-sim/scenarios/smoke.yaml"),
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: smoke\npersona:\n  name: Alex\n  brief: Test caller\n",
    )
    .unwrap();
    let server = Arc::new(ApiServer::new(dir.path()));
    let resp = api_router(server.clone())
        .oneshot(
            Request::builder()
                .uri("/api/v1/runs")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let v = body_json(resp).await;
    assert!(v.is_array(), "runs is array: {v}");

    let resp = api_router(server)
        .oneshot(
            Request::builder()
                .uri("/api/v1/scenarios")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let v = body_json(resp).await;
    assert!(v.is_array(), "scenarios is array: {v}");
}
