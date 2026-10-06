//! Per-client-IP rate limits — restored. The Python service had them
//! (slowapi: 120/min on each sync route) and the Rust port shipped
//! without. Every request here is checked against License-Service before
//! it is refused, so without a limit an anonymous caller could drive
//! unbounded traffic at that service through this one, and a key holder
//! could push without bound.
//!
//! The bucket is the client address, not the key: a caller is
//! identified only by the key it presents, so bucketing on it would let
//! a guesser open a fresh bucket per guess.
//!
//! In memory, one process — the deploy is a single machine, as the
//! Python's was when it ran without REDIS_URL.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use axum::extract::{ConnectInfo, Request};
use axum::http::{header, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::Json;

const WINDOW: Duration = Duration::from_secs(60);

/// Above this many live buckets, expired ones are swept on the next
/// request, so a scan from many addresses cannot grow the map forever.
const SWEEP_ABOVE: usize = 10_000;

struct Window {
    started: Instant,
    count: u32,
}

fn windows() -> &'static Mutex<HashMap<String, Window>> {
    static WINDOWS: OnceLock<Mutex<HashMap<String, Window>>> = OnceLock::new();
    WINDOWS.get_or_init(|| Mutex::new(HashMap::new()))
}

/// The real client address.
///
/// `Fly-Client-IP` first: Fly's edge sets it from the TCP connection and
/// a client cannot override it, unlike `X-Forwarded-For`, whose
/// left-most entry is whatever the caller wrote — the Python's limiter
/// was once defeated exactly that way. Then the connection itself.
pub fn client_ip(request: &Request) -> Option<String> {
    if let Some(ip) = request
        .headers()
        .get("fly-client-ip")
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|ip| !ip.is_empty())
    {
        return Some(ip.to_string());
    }
    request
        .extensions()
        .get::<ConnectInfo<SocketAddr>>()
        .map(|info| info.0.ip().to_string())
}

/// Spend one request from `key`'s window. False once `limit` is spent.
fn admit(key: String, limit: u32, now: Instant) -> bool {
    let mut windows = windows().lock().unwrap_or_else(|e| e.into_inner());
    if windows.len() > SWEEP_ABOVE {
        windows.retain(|_, w| now.duration_since(w.started) < WINDOW);
    }
    let window = windows.entry(key).or_insert(Window {
        started: now,
        count: 0,
    });
    if now.duration_since(window.started) >= WINDOW {
        *window = Window {
            started: now,
            count: 0,
        };
    }
    if window.count >= limit {
        return false;
    }
    window.count += 1;
    true
}

/// `@limiter.limit("N/minute")`, per route and client address.
///
/// A request with no address at all — only an in-process test, since
/// the server is always started with connect info — is not limited.
pub async fn per_minute<const N: u32>(request: Request, next: Next) -> Response {
    if let Some(ip) = client_ip(&request) {
        let key = format!("{}|{ip}", request.uri().path());
        if !admit(key, N, Instant::now()) {
            return too_many(N);
        }
    }
    next.run(request).await
}

/// The Python's 429, field for field.
fn too_many(limit: u32) -> Response {
    (
        StatusCode::TOO_MANY_REQUESTS,
        [(header::RETRY_AFTER, "60")],
        Json(serde_json::json!({
            "error": "rate_limit_exceeded",
            "message": "Too many requests. Back off and retry after the Retry-After window.",
            "limit": format!("{limit} per 1 minute"),
            "retry_after_seconds": 60,
        })),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_window_admits_exactly_the_limit_then_resets() {
        let start = Instant::now();
        let key = || "test|10.9.9.1".to_string();
        for _ in 0..3 {
            assert!(admit(key(), 3, start));
        }
        assert!(!admit(key(), 3, start));
        assert!(!admit(key(), 3, start + Duration::from_secs(59)));
        assert!(admit(key(), 3, start + Duration::from_secs(60)));
    }

    #[test]
    fn buckets_are_per_address_and_per_route() {
        let now = Instant::now();
        assert!(admit("/a|10.9.9.2".into(), 1, now));
        assert!(!admit("/a|10.9.9.2".into(), 1, now));
        assert!(admit("/a|10.9.9.3".into(), 1, now));
        assert!(admit("/b|10.9.9.2".into(), 1, now));
    }

    #[test]
    fn fly_client_ip_wins_and_forwarded_for_is_ignored() {
        let request = Request::builder()
            .header("x-forwarded-for", "6.6.6.6")
            .header("fly-client-ip", "1.2.3.4")
            .body(axum::body::Body::empty())
            .unwrap();
        assert_eq!(client_ip(&request).as_deref(), Some("1.2.3.4"));
        let spoofed = Request::builder()
            .header("x-forwarded-for", "6.6.6.6")
            .body(axum::body::Body::empty())
            .unwrap();
        assert_eq!(client_ip(&spoofed), None);
    }
}
