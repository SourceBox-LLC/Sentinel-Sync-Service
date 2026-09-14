//! HTTP error shape.
//!
//! FastAPI's `HTTPException` renders as `{"detail": "..."}` and Command
//! Center's restore script branches on the status code (401 vs 403). Both
//! are part of the wire contract, so this reproduces the body shape
//! exactly rather than inventing a nicer one.

use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use serde_json::json;

#[derive(Debug)]
pub struct ApiError {
    pub status: StatusCode,
    pub detail: String,
}

impl ApiError {
    pub fn new(status: StatusCode, detail: impl Into<String>) -> Self {
        Self {
            status,
            detail: detail.into(),
        }
    }

    /// Missing or malformed `Authorization` header. The message is copied
    /// verbatim from the Python service.
    pub fn unauthorized() -> Self {
        Self::new(
            StatusCode::UNAUTHORIZED,
            "Missing or malformed Authorization header",
        )
    }

    /// Checked, and the answer is no.
    pub fn forbidden(detail: impl Into<String>) -> Self {
        Self::new(StatusCode::FORBIDDEN, detail)
    }

    /// Could not check. Distinct from 403 on purpose: Command Center's
    /// sync_client treats this as "retry next cycle", not "permanently
    /// rejected". See entitlements.rs.
    pub fn bad_gateway(detail: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_GATEWAY, detail)
    }

    pub fn payload_too_large(detail: impl Into<String>) -> Self {
        Self::new(StatusCode::PAYLOAD_TOO_LARGE, detail)
    }

    /// FastAPI's answer to a bad query parameter. 422, not axum's default
    /// 400 — see the note in api.rs::list_rows.
    pub fn unprocessable(detail: impl Into<String>) -> Self {
        Self::new(StatusCode::UNPROCESSABLE_ENTITY, detail)
    }

    pub fn internal(detail: impl Into<String>) -> Self {
        Self::new(StatusCode::INTERNAL_SERVER_ERROR, detail)
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.status, Json(json!({ "detail": self.detail }))).into_response()
    }
}

/// A database failure is a 500 with a generic body — the underlying sqlx
/// error can name columns and constraints, which is not something to hand
/// to a caller. The detail goes to the log instead.
impl From<sqlx::Error> for ApiError {
    fn from(err: sqlx::Error) -> Self {
        tracing::error!(error = %err, "database error");
        ApiError::internal("database error")
    }
}
