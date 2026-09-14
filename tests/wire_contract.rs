//! The wire contract, as tests.
//!
//! This service was ported from Python, and the thing that could break
//! silently is not "does it compile" but "does it still answer exactly
//! what Command Center's sync_client.py and restore_from_cloud.py
//! expect". During the port both implementations were run against one
//! database and their responses diffed; these tests carry the cases that
//! mattered, so the contract stays pinned now that the Python is gone.
//!
//! Needs a Postgres. Set TEST_DATABASE_URL (or DATABASE_URL); skipped
//! with a clear message otherwise, so `cargo test` on a laptop without a
//! database still passes rather than failing for the wrong reason.

use std::net::SocketAddr;

use axum::{routing::get, Json, Router};
use http_body_util::BodyExt;
use serde_json::{json, Value};
use sqlx::postgres::PgPoolOptions;
use tower::ServiceExt;

use sentinel_sync_service::config::{normalize_database_url, Config};
use sentinel_sync_service::entitlements::Entitlements;
use sentinel_sync_service::{build_router, AppState};

fn database_url() -> Option<String> {
    std::env::var("TEST_DATABASE_URL")
        .or_else(|_| std::env::var("DATABASE_URL"))
        .ok()
        .map(|u| normalize_database_url(&u))
}

/// A stand-in License-Service. Keys steer the answer so the auth paths
/// can be exercised without the real service.
async fn spawn_stub_license() -> SocketAddr {
    async fn entitlements(headers: axum::http::HeaderMap) -> Json<Value> {
        let key = headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.split_once(' '))
            .map(|(_, k)| k.trim().to_string())
            .unwrap_or_default();
        Json(match key.as_str() {
            "denied-key" => json!({"valid": false, "reason": "license revoked"}),
            "nosync-key" => {
                json!({"valid": true, "license_key_hash": "t_nosync", "sync_enabled": false})
            }
            "nohash-key" => json!({"valid": true, "sync_enabled": true}),
            other => json!({
                "valid": true,
                "license_key_hash": format!("tenant_{other}"),
                "sync_enabled": true
            }),
        })
    }

    let app = Router::new().route("/v1/licenses/entitlements", get(entitlements));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    addr
}

async fn test_state(tenant_scope: &str) -> Option<(AppState, sqlx::PgPool)> {
    let url = database_url()?;
    let pool = PgPoolOptions::new()
        .max_connections(4)
        .connect(&url)
        .await
        .expect("connect to TEST_DATABASE_URL");
    sqlx::migrate!("./migrations").run(&pool).await.unwrap();

    // Each test owns a tenant prefix, so they can share a database
    // without a TRUNCATE race between them.
    sqlx::query("DELETE FROM synced_rows WHERE tenant_key = $1")
        .bind(format!("tenant_{tenant_scope}"))
        .execute(&pool)
        .await
        .unwrap();

    let addr = spawn_stub_license().await;
    let mut config = Config::from_env();
    config.license_service_url = format!("http://{addr}");
    config.entitlement_cache_seconds = 0;
    config.database_url = url;

    Some((
        AppState {
            pool: pool.clone(),
            entitlements: std::sync::Arc::new(Entitlements::new(
                config.license_service_url.clone(),
                0,
            )),
            config: std::sync::Arc::new(config),
            started_at: std::time::Instant::now(),
        },
        pool,
    ))
}

async fn call(
    state: &AppState,
    method: &str,
    uri: &str,
    auth: Option<&str>,
    body: Option<Value>,
) -> (u16, Value) {
    let mut req = axum::http::Request::builder().method(method).uri(uri);
    if let Some(a) = auth {
        req = req.header("authorization", a);
    }
    let req = match body {
        Some(b) => req
            .header("content-type", "application/json")
            .body(axum::body::Body::from(serde_json::to_vec(&b).unwrap()))
            .unwrap(),
        None => req.body(axum::body::Body::empty()).unwrap(),
    };

    let resp = build_router(state.clone()).oneshot(req).await.unwrap();
    let status = resp.status().as_u16();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, value)
}

macro_rules! skip_without_db {
    ($scope:expr) => {
        match test_state($scope).await {
            Some(v) => v,
            None => {
                eprintln!("skipping: set TEST_DATABASE_URL to run wire-contract tests");
                return;
            }
        }
    };
}

// --- auth ------------------------------------------------------------

#[tokio::test]
async fn auth_failures_map_to_the_codes_callers_branch_on() {
    let (state, _pool) = skip_without_db!("auth");

    // restore_from_cloud.py branches on 401 and 403 specifically.
    for header in [None, Some("abc"), Some("Basic abc"), Some("Bearer   ")] {
        let (status, body) = call(&state, "GET", "/v1/sync/tables", header, None).await;
        assert_eq!(status, 401, "header {header:?} should be 401");
        assert_eq!(body["detail"], "Missing or malformed Authorization header");
    }

    let (status, _) = call(
        &state,
        "GET",
        "/v1/sync/tables",
        Some("Bearer denied-key"),
        None,
    )
    .await;
    assert_eq!(status, 403, "a revoked license is 403, not 401");

    let (status, _) = call(
        &state,
        "GET",
        "/v1/sync/tables",
        Some("Bearer nosync-key"),
        None,
    )
    .await;
    assert_eq!(status, 403, "sync entitlement off is 403");
}

#[tokio::test]
async fn a_license_service_contract_break_is_502_not_403() {
    // 502 means "could not check"; 403 means "checked, no". Command
    // Center's sync loop retries the first and gives up on the second,
    // so collapsing them would turn a blip into an apparent revocation.
    let (state, _pool) = skip_without_db!("nohash");
    let (status, body) = call(
        &state,
        "GET",
        "/v1/sync/tables",
        Some("Bearer nohash-key"),
        None,
    )
    .await;
    assert_eq!(status, 502);
    assert!(
        body["detail"]
            .as_str()
            .unwrap()
            .contains("license_key_hash"),
        "detail should name the missing field, got {body}"
    );
}

// --- tenant isolation -------------------------------------------------

#[tokio::test]
async fn one_tenant_cannot_see_anothers_rows() {
    let (state, _pool) = skip_without_db!("iso_a");
    let (state_b, _) = test_state("iso_b").await.unwrap();

    let seed = json!({"table": "cameras", "rows": [
        {"id": "cam_1", "updated_at": "2026-09-01T10:00:00", "data": {"secret": "a"}}
    ]});
    let (status, _) = call(
        &state,
        "POST",
        "/v1/sync/push",
        Some("Bearer iso_a"),
        Some(seed),
    )
    .await;
    assert_eq!(status, 200);

    // Same table name, different key -> a different tenant, no rows.
    let (status, body) = call(
        &state_b,
        "GET",
        "/v1/sync/rows?table=cameras",
        Some("Bearer iso_b"),
        None,
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(
        body["rows"].as_array().unwrap().len(),
        0,
        "tenant B must not see tenant A's rows"
    );
}

// --- push semantics ---------------------------------------------------

async fn seed(state: &AppState, key: &str) {
    let body = json!({"table": "cameras", "rows": [
        {"id": "cam_b", "updated_at": "2026-09-01T10:00:00",        "data": {"n": 2}},
        {"id": "cam_a", "updated_at": "2026-09-01T09:00:00.123456", "data": {"n": 1}},
        {"id": "cam_c", "updated_at": "2026-09-02T11:30:00",        "data": {"n": 3}}
    ]});
    let (status, _) = call(
        state,
        "POST",
        "/v1/sync/push",
        Some(&format!("Bearer {key}")),
        Some(body),
    )
    .await;
    assert_eq!(status, 200);
}

#[tokio::test]
async fn known_ids_absent_leaves_tombstones_alone() {
    // The None vs [] distinction is load-bearing: absent means "don't
    // touch tombstone state", which is the default for high-volume log
    // tables whose local retention deletes must never propagate.
    let (state, _pool) = skip_without_db!("kid_absent");
    seed(&state, "kid_absent").await;

    let body = json!({"table": "cameras", "rows": []});
    let (status, resp) = call(
        &state,
        "POST",
        "/v1/sync/push",
        Some("Bearer kid_absent"),
        Some(body),
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(
        resp["tombstoned"], 0,
        "absent known_ids must tombstone nothing"
    );
}

#[tokio::test]
async fn known_ids_empty_tombstones_everything() {
    let (state, _pool) = skip_without_db!("kid_empty");
    seed(&state, "kid_empty").await;

    let body = json!({"table": "cameras", "rows": [], "known_ids": []});
    let (status, resp) = call(
        &state,
        "POST",
        "/v1/sync/push",
        Some("Bearer kid_empty"),
        Some(body),
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(
        resp["tombstoned"], 3,
        "an empty known_ids list means all rows are gone locally"
    );
}

#[tokio::test]
async fn known_ids_tombstones_the_absent_and_revives_the_returned() {
    let (state, _pool) = skip_without_db!("revive");
    seed(&state, "revive").await;

    let body = json!({"table": "cameras", "rows": [], "known_ids": ["cam_a"]});
    let (_, resp) = call(
        &state,
        "POST",
        "/v1/sync/push",
        Some("Bearer revive"),
        Some(body),
    )
    .await;
    assert_eq!(resp["tombstoned"], 2);

    // A row coming back must un-tombstone rather than stay deleted.
    let body = json!({"table": "cameras", "rows": [], "known_ids": ["cam_a", "cam_b", "cam_c"]});
    let (_, resp) = call(
        &state,
        "POST",
        "/v1/sync/push",
        Some("Bearer revive"),
        Some(body),
    )
    .await;
    assert_eq!(resp["tombstoned"], 0);

    let (_, body) = call(
        &state,
        "GET",
        "/v1/sync/rows?table=cameras",
        Some("Bearer revive"),
        None,
    )
    .await;
    assert_eq!(
        body["rows"].as_array().unwrap().len(),
        3,
        "all three should be live again"
    );
}

#[tokio::test]
async fn a_push_over_the_row_cap_is_413() {
    let (state, _pool) = skip_without_db!("cap");
    let rows: Vec<Value> = (0..state.config.max_rows_per_push + 1)
        .map(|i| json!({"id": format!("r{i}"), "updated_at": "2026-09-01T10:00:00", "data": {}}))
        .collect();
    let (status, _) = call(
        &state,
        "POST",
        "/v1/sync/push",
        Some("Bearer cap"),
        Some(json!({"table": "cameras", "rows": rows})),
    )
    .await;
    assert_eq!(status, 413);
}

// --- read semantics ---------------------------------------------------

#[tokio::test]
async fn rows_paginate_by_keyset_and_report_next_cursor() {
    let (state, _pool) = skip_without_db!("page");
    seed(&state, "page").await;

    let (_, p1) = call(
        &state,
        "GET",
        "/v1/sync/rows?table=cameras&limit=2",
        Some("Bearer page"),
        None,
    )
    .await;
    let ids: Vec<&str> = p1["rows"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["id"].as_str().unwrap())
        .collect();
    assert_eq!(ids, vec!["cam_a", "cam_b"], "ordered by row_id ascending");
    assert_eq!(p1["next_cursor"], "cam_b");

    let (_, p2) = call(
        &state,
        "GET",
        "/v1/sync/rows?table=cameras&limit=2&cursor=cam_b",
        Some("Bearer page"),
        None,
    )
    .await;
    let ids: Vec<&str> = p2["rows"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["id"].as_str().unwrap())
        .collect();
    assert_eq!(ids, vec!["cam_c"]);
    assert!(
        p2["next_cursor"].is_null(),
        "last page reports a null cursor"
    );
    // Present-but-null, not omitted: restore_from_cloud.py does
    // body.get("next_cursor") and Pydantic always emitted the key.
    assert!(p2.as_object().unwrap().contains_key("next_cursor"));
}

#[tokio::test]
async fn tombstoned_rows_are_hidden_unless_asked_for() {
    // A restore that resurrects cameras the operator deliberately deleted
    // would be actively wrong; include_deleted exists for forensics.
    let (state, _pool) = skip_without_db!("tomb");
    seed(&state, "tomb").await;
    let body = json!({"table": "cameras", "rows": [], "known_ids": ["cam_a"]});
    call(
        &state,
        "POST",
        "/v1/sync/push",
        Some("Bearer tomb"),
        Some(body),
    )
    .await;

    let (_, hidden) = call(
        &state,
        "GET",
        "/v1/sync/rows?table=cameras",
        Some("Bearer tomb"),
        None,
    )
    .await;
    assert_eq!(hidden["rows"].as_array().unwrap().len(), 1);

    let (_, shown) = call(
        &state,
        "GET",
        "/v1/sync/rows?table=cameras&include_deleted=true",
        Some("Bearer tomb"),
        None,
    )
    .await;
    assert_eq!(shown["rows"].as_array().unwrap().len(), 3);
}

#[tokio::test]
async fn timestamps_serialise_the_way_python_did() {
    // restore_from_cloud.py parses these with datetime.fromisoformat. A
    // trailing "+00:00" or a "Z" would break it, and an offset-aware Rust
    // type would have added one.
    let (state, _pool) = skip_without_db!("ts");
    seed(&state, "ts").await;

    let (_, body) = call(
        &state,
        "GET",
        "/v1/sync/rows?table=cameras",
        Some("Bearer ts"),
        None,
    )
    .await;
    let row = &body["rows"][0];
    let source = row["source_updated_at"].as_str().unwrap();
    assert_eq!(
        source, "2026-09-01T09:00:00.123456",
        "microseconds preserved, no timezone suffix"
    );

    let synced = row["synced_at"].as_str().unwrap();
    assert!(
        !synced.ends_with('Z') && !synced.contains('+'),
        "naive, not offset-aware: {synced}"
    );
}

#[tokio::test]
async fn tables_reports_totals_and_tombstone_counts() {
    let (state, _pool) = skip_without_db!("tbl");
    seed(&state, "tbl").await;
    let body = json!({"table": "cameras", "rows": [], "known_ids": ["cam_a"]});
    call(
        &state,
        "POST",
        "/v1/sync/push",
        Some("Bearer tbl"),
        Some(body),
    )
    .await;

    let (status, body) = call(&state, "GET", "/v1/sync/tables", Some("Bearer tbl"), None).await;
    assert_eq!(status, 200);
    let t = &body["tables"][0];
    assert_eq!(t["table"], "cameras");
    assert_eq!(t["rows"], 3, "total counts tombstoned rows too");
    assert_eq!(t["deleted"], 2);
}

#[tokio::test]
async fn bad_query_parameters_are_422_like_fastapi() {
    // axum's own Query rejection is a 400; FastAPI answered 422 and the
    // status code is part of the contract.
    let (state, _pool) = skip_without_db!("q");
    for uri in [
        "/v1/sync/rows",
        "/v1/sync/rows?table=cameras&limit=0",
        "/v1/sync/rows?table=cameras&limit=1001",
    ] {
        let (status, _) = call(&state, "GET", uri, Some("Bearer q"), None).await;
        assert_eq!(status, 422, "{uri} should be 422");
    }
}
