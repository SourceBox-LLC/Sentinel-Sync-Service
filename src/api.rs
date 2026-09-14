//! Sync endpoints: one write path in, two read paths back out.
//!
//! Every route derives its tenant from the validated key, never from the
//! request body — that is the whole tenant-isolation boundary, and it is
//! why `tenant_key` is a parameter here rather than anything deserialised.

use axum::{
    extract::{Query, State},
    http::HeaderMap,
    Json,
};
use chrono::{NaiveDateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::app::AppState;
use crate::entitlements::{extract_bearer, EntitlementError};
use crate::error::ApiError;

// ---------------------------------------------------------------------
// Wire types. Field names, optionality and ordering all mirror the
// Pydantic models they replace; `next_cursor` is serialised even when
// null because Pydantic emitted it and the restore script calls
// `body.get("next_cursor")` against it.
// ---------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct PushRow {
    pub id: String,
    pub updated_at: NaiveDateTime,
    pub data: Value,
}

#[derive(Debug, Deserialize)]
pub struct PushRequest {
    pub table: String,
    #[serde(default)]
    pub rows: Vec<PushRow>,
    /// Present only for tables that want deletion reconciliation: the
    /// complete current set of local row ids. Absent means "don't touch
    /// tombstone state for this table", which is the deliberate default
    /// for high-volume log tables whose local retention deletes must
    /// never propagate. `None` and `[]` mean different things here.
    #[serde(default)]
    pub known_ids: Option<Vec<String>>,
}

#[derive(Debug, Serialize)]
pub struct PushResponse {
    pub accepted: i64,
    pub tombstoned: i64,
}

#[derive(Debug, Serialize)]
pub struct TableSummary {
    pub table: String,
    pub rows: i64,
    pub deleted: i64,
}

#[derive(Debug, Serialize)]
pub struct TablesResponse {
    pub tables: Vec<TableSummary>,
}

#[derive(Debug, Serialize)]
pub struct StoredRow {
    pub id: String,
    pub data: Value,
    pub source_updated_at: NaiveDateTime,
    pub synced_at: NaiveDateTime,
    pub deleted: bool,
}

#[derive(Debug, Serialize)]
pub struct RowsResponse {
    pub table: String,
    pub rows: Vec<StoredRow>,
    pub next_cursor: Option<String>,
}

/// Every field is optional at the deserialisation step on purpose.
///
/// axum's `Query` rejection is a **400**, but FastAPI answered **422** for
/// a missing or out-of-range query parameter, and the status code is part
/// of the contract. Accepting anything here and validating below keeps the
/// rejection ours to shape.
#[derive(Debug, Deserialize)]
pub struct RowsQuery {
    #[serde(default)]
    pub table: Option<String>,
    #[serde(default)]
    pub cursor: Option<String>,
    #[serde(default = "default_limit")]
    pub limit: i64,
    #[serde(default)]
    pub include_deleted: bool,
}

fn default_limit() -> i64 {
    500
}

// ---------------------------------------------------------------------

/// Validate the Bearer key and return the tenant it maps to.
///
/// Shared by every route so read and write agree by construction on both
/// the auth rules and the tenant boundary.
async fn tenant_from_auth(state: &AppState, headers: &HeaderMap) -> Result<String, ApiError> {
    let authorization = headers
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok());

    let raw_key = extract_bearer(authorization).ok_or_else(ApiError::unauthorized)?;

    match state.entitlements.validate(&raw_key).await {
        Ok(entitlement) => Ok(entitlement.tenant_key),
        Err(EntitlementError::Denied(reason)) => Err(ApiError::forbidden(reason)),
        Err(EntitlementError::Unavailable(reason)) => Err(ApiError::bad_gateway(format!(
            "entitlement check unavailable: {reason}"
        ))),
    }
}

pub async fn push(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(payload): Json<PushRequest>,
) -> Result<Json<PushResponse>, ApiError> {
    let tenant_key = tenant_from_auth(&state, &headers).await?;

    if payload.rows.len() > state.config.max_rows_per_push {
        return Err(ApiError::payload_too_large(format!(
            "too many rows in one push (max {})",
            state.config.max_rows_per_push
        )));
    }

    let now: NaiveDateTime = Utc::now().naive_utc();

    // One transaction for the whole push: the Python version committed
    // once at the end, so a failure mid-batch left nothing behind. Keep
    // that, or a partial push could tombstone rows without writing the
    // replacements.
    let mut tx = state.pool.begin().await?;

    let mut tombstoned: i64 = 0;
    if let Some(known_ids) = payload.known_ids.as_ref() {
        // Tombstone everything this tenant has for the table that the
        // caller no longer holds locally...
        let res = sqlx::query(
            "UPDATE synced_rows
                SET deleted = TRUE, synced_at = $1
              WHERE tenant_key = $2
                AND table_name = $3
                AND NOT (row_id = ANY($4))",
        )
        .bind(now)
        .bind(&tenant_key)
        .bind(&payload.table)
        .bind(known_ids)
        .execute(&mut *tx)
        .await?;
        tombstoned = res.rows_affected() as i64;

        // ...and revive any it does hold that were previously tombstoned.
        sqlx::query(
            "UPDATE synced_rows
                SET deleted = FALSE, synced_at = $1
              WHERE tenant_key = $2
                AND table_name = $3
                AND row_id = ANY($4)
                AND deleted IS TRUE",
        )
        .bind(now)
        .bind(&tenant_key)
        .bind(&payload.table)
        .bind(known_ids)
        .execute(&mut *tx)
        .await?;
    }

    let mut accepted: i64 = 0;
    for row in &payload.rows {
        sqlx::query(
            "INSERT INTO synced_rows
                 (tenant_key, table_name, row_id, payload, source_updated_at, synced_at, deleted)
             VALUES ($1, $2, $3, $4, $5, $6, FALSE)
             ON CONFLICT (tenant_key, table_name, row_id) DO UPDATE
                SET payload           = EXCLUDED.payload,
                    source_updated_at = EXCLUDED.source_updated_at,
                    synced_at         = EXCLUDED.synced_at,
                    deleted           = FALSE",
        )
        .bind(&tenant_key)
        .bind(&payload.table)
        .bind(&row.id)
        .bind(&row.data)
        .bind(row.updated_at)
        .bind(now)
        .execute(&mut *tx)
        .await?;
        accepted += 1;
    }

    tx.commit().await?;

    Ok(Json(PushResponse {
        accepted,
        tombstoned,
    }))
}

pub async fn list_tables(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<TablesResponse>, ApiError> {
    let tenant_key = tenant_from_auth(&state, &headers).await?;

    let rows: Vec<(String, i64, i64)> = sqlx::query_as(
        "SELECT table_name,
                COUNT(*)                                        AS total,
                COUNT(*) FILTER (WHERE deleted IS TRUE)         AS deleted
           FROM synced_rows
          WHERE tenant_key = $1
          GROUP BY table_name
          ORDER BY table_name",
    )
    .bind(&tenant_key)
    .fetch_all(&state.pool)
    .await?;

    Ok(Json(TablesResponse {
        tables: rows
            .into_iter()
            .map(|(table, rows, deleted)| TableSummary {
                table,
                rows,
                deleted,
            })
            .collect(),
    }))
}

pub async fn list_rows(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(q): Query<RowsQuery>,
) -> Result<Json<RowsResponse>, ApiError> {
    let tenant_key = tenant_from_auth(&state, &headers).await?;

    // FastAPI enforced these with Query(...) constraints and answered 422.
    //
    // The status code is reproduced exactly; the body is not. FastAPI
    // emitted Pydantic's structured error array
    // (`[{"type":"greater_than_equal","loc":["query","limit"],...}]`), and
    // no caller reads it — Command Center's restore script branches on 401
    // and 403 and otherwise calls raise_for_status(). Mirroring Pydantic's
    // internal error shape would be copying an implementation detail, so
    // this says what is wrong in plain text instead.
    let Some(table) = q.table.filter(|t| !t.is_empty()) else {
        return Err(ApiError::unprocessable(
            "query parameter 'table' is required",
        ));
    };
    if table.len() > 100 {
        return Err(ApiError::unprocessable(
            "'table' must be at most 100 characters",
        ));
    }
    if q.cursor.as_ref().is_some_and(|c| c.len() > 64) {
        return Err(ApiError::unprocessable(
            "'cursor' must be at most 64 characters",
        ));
    }
    if !(1..=1000).contains(&q.limit) {
        return Err(ApiError::unprocessable(
            "'limit' must be between 1 and 1000",
        ));
    }

    // Keyset pagination on row_id, not OFFSET: a restore walks entire
    // tables (motion_events runs to 100K+ rows) and OFFSET makes every
    // successive page more expensive than the last. Ordering by row_id
    // also keeps paging stable against concurrent pushes.
    //
    // Fetch one extra row to learn whether another page exists without a
    // second COUNT.
    let fetch = q.limit + 1;

    let found: Vec<(String, Value, NaiveDateTime, NaiveDateTime, bool)> = sqlx::query_as(
        "SELECT row_id, payload, source_updated_at, synced_at, deleted
           FROM synced_rows
          WHERE tenant_key = $1
            AND table_name = $2
            AND ($3::bool OR deleted IS FALSE)
            AND ($4::text IS NULL OR row_id > $4::text)
          ORDER BY row_id ASC
          LIMIT $5",
    )
    .bind(&tenant_key)
    .bind(&table)
    .bind(q.include_deleted)
    .bind(q.cursor.as_deref())
    .bind(fetch)
    .fetch_all(&state.pool)
    .await?;

    let has_more = found.len() as i64 > q.limit;
    let page: Vec<StoredRow> = found
        .into_iter()
        .take(q.limit as usize)
        .map(
            |(id, data, source_updated_at, synced_at, deleted)| StoredRow {
                id,
                data,
                source_updated_at,
                synced_at,
                deleted,
            },
        )
        .collect();

    let next_cursor = if has_more {
        page.last().map(|r| r.id.clone())
    } else {
        None
    };

    Ok(Json(RowsResponse {
        table,
        rows: page,
        next_cursor,
    }))
}
