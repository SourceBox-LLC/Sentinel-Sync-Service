//! Sentinel-Sync-Service — cloud data-sync mirror for self-hosted
//! Command Center installs.
//!
//! Ported from the Python/FastAPI implementation. The wire contract is
//! the specification: Command Center's `sync_client.py` pushes here and
//! its `scripts/restore_from_cloud.py` reads back, and neither may notice
//! the change. Where a behaviour looked odd it was reproduced rather than
//! improved — the 403/502 split, tombstones-excluded-by-default, and
//! `next_cursor` being present-but-null all matter to a caller.

use sentinel_sync_service::config::Config;
use sentinel_sync_service::entitlements::Entitlements;
use sentinel_sync_service::{build_router, AppState, VERSION};

use std::sync::Arc;
use std::time::Instant;

use sqlx::postgres::PgPoolOptions;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();

    let config = Config::from_env();
    let port = config.port;

    // Small pool on purpose: this runs on a 256 MB shared-cpu-1x machine
    // that scales to zero, and Postgres connections are the scarce
    // resource across the three services sharing one cluster.
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&config.database_url)
        .await?;

    // Schema bring-up, replacing Alembic's release_command. sqlx takes a
    // Postgres advisory lock for the duration, so two machines starting
    // at once cannot race, and it keeps a version table so the next
    // schema change is a numbered migration rather than hand-edited DDL.
    // The one migration is IF NOT EXISTS throughout, so this is a
    // verified no-op against the table Alembic already created.
    sqlx::migrate!("./migrations").run(&pool).await?;

    let state = AppState {
        entitlements: Arc::new(Entitlements::new(
            config.license_service_url.clone(),
            config.entitlement_cache_seconds,
        )),
        config: Arc::new(config),
        pool,
        started_at: Instant::now(),
    };

    let app = build_router(state);

    let addr = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    tracing::info!(%addr, version = VERSION, "sentinel-sync-service listening");

    // With connect info, so the rate limiter can fall back to the peer
    // address when Fly-Client-IP is absent.
    axum::serve(
        listener,
        app.into_make_service_with_connect_info::<std::net::SocketAddr>(),
    )
    .with_graceful_shutdown(shutdown_signal())
    .await?;
    Ok(())
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    tracing::info!("shutting down");
}
