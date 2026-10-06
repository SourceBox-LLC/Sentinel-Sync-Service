//! Application state, route table, and the non-sync endpoints.
//!
//! Split out of `main.rs` so `tests/` can build the exact router the
//! binary serves — an integration test that re-declares its own routes
//! tests a copy, and the copy drifts.

use std::sync::Arc;
use std::time::Instant;

use axum::{
    middleware::from_fn,
    routing::{get, post},
    Json, Router,
};
use chrono::Utc;
use serde_json::json;
use tower_http::limit::RequestBodyLimitLayer;

use crate::config::Config;
use crate::entitlements::Entitlements;

#[derive(Clone)]
pub struct AppState {
    pub pool: sqlx::PgPool,
    pub entitlements: Arc<Entitlements>,
    pub config: Arc<Config>,
    pub started_at: Instant,
}

pub const VERSION: &str = "0.1.0";

/// Body cap for a push. MAX_ROWS_PER_PUSH bounds the row count, but not
/// the size of each row's JSON payload, so an oversized body could still
/// arrive. axum defaults to 2 MiB; a full 1000-row push of real Command
/// Center rows runs larger than that.
const MAX_BODY_BYTES: usize = 32 * 1024 * 1024;

/// The whole route table, separated from `main` so integration tests can
/// drive it with `tower::ServiceExt::oneshot` instead of binding a port.
pub fn build_router(state: AppState) -> Router {
    Router::new()
        .route("/", get(root))
        .route("/health", get(health))
        .route("/health/ready", get(health_ready))
        .route(
            "/v1/sync/push",
            post(crate::api::push).route_layer(from_fn(crate::ratelimit::per_minute::<120>)),
        )
        .route(
            "/v1/sync/tables",
            get(crate::api::list_tables).route_layer(from_fn(crate::ratelimit::per_minute::<120>)),
        )
        .route(
            "/v1/sync/rows",
            get(crate::api::list_rows).route_layer(from_fn(crate::ratelimit::per_minute::<120>)),
        )
        .layer(RequestBodyLimitLayer::new(MAX_BODY_BYTES))
        .with_state(state)
}

async fn root() -> Json<serde_json::Value> {
    // The Python service emitted a naive UTC isoformat with a "Z" glued
    // on. Reproduced exactly, trailing Z and all.
    let now = Utc::now().naive_utc();
    Json(json!({
        "service": "sentinel-sync-service",
        "time": format!("{}Z", now.format("%Y-%m-%dT%H:%M:%S%.6f")),
    }))
}

/// Pure liveness — must never be slow. This is what Fly's health check
/// polls; a slow dependency must not pull the only machine out of
/// rotation.
async fn health() -> Json<serde_json::Value> {
    Json(json!({ "status": "healthy", "version": VERSION }))
}

/// Readiness — 503 if the database probe fails, 200 otherwise.
async fn health_ready(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> (axum::http::StatusCode, Json<serde_json::Value>) {
    let started = Instant::now();
    let probe = sqlx::query("SELECT 1").execute(&state.pool).await;
    let latency_ms = (started.elapsed().as_secs_f64() * 1000.0 * 100.0).round() / 100.0;

    let (ready, database) = match probe {
        Ok(_) => (true, json!({ "status": "ok", "latency_ms": latency_ms })),
        Err(err) => {
            tracing::error!(error = %err, "readiness: database probe failed");
            (
                false,
                json!({ "status": "critical", "error": err.to_string() }),
            )
        }
    };

    let status = if ready {
        axum::http::StatusCode::OK
    } else {
        axum::http::StatusCode::SERVICE_UNAVAILABLE
    };

    (
        status,
        Json(json!({
            "ready": ready,
            "checks": { "database": database },
            "version": VERSION,
            "uptime_seconds": (state.started_at.elapsed().as_secs_f64() * 10.0).round() / 10.0,
        })),
    )
}
