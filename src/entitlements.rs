//! Validates a caller's Bearer license key against Sentinel-License-Service.
//!
//! This service holds no license records of its own. The rules are carried
//! over from the Python implementation unchanged, because both of them are
//! load-bearing for callers:
//!
//! FAILS CLOSED, NOT OPEN. If License-Service is unreachable a push is
//! rejected for this cycle. Command Center's sync loop already treats a
//! failed push as "retry next cycle, cursor doesn't advance", so failing
//! closed costs a few minutes of sync latency — where failing open would
//! mean storing data from a caller we could not verify was entitled to
//! push it.
//!
//! DENIED AND UNAVAILABLE ARE DIFFERENT. `Denied` becomes a 403 ("we
//! checked, the answer is no"); `Unavailable` becomes a 502 ("we could not
//! check"). A well-behaved client distinguishes them — collapsing both
//! into 403 would turn a License-Service blip into what looks like a
//! permanent entitlement revocation.
//!
//! A short TTL cache keeps a large multi-batch push from calling out on
//! every batch, while still noticing a revoked license within
//! ENTITLEMENT_CACHE_SECONDS.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};

#[derive(Debug, Clone)]
pub struct Entitlement {
    /// License-Service's `key_hash` — the stable tenant id, derived
    /// server-side. Never taken from anything the caller sends; this is
    /// the entire tenant-isolation boundary.
    pub tenant_key: String,
    pub sync_enabled: bool,
}

#[derive(Debug)]
pub enum EntitlementError {
    /// Checked, and refused.
    Denied(String),
    /// Could not check.
    Unavailable(String),
}

pub struct Entitlements {
    client: reqwest::Client,
    license_service_url: String,
    ttl: Duration,
    cache: Mutex<HashMap<String, (Entitlement, Instant)>>,
}

impl Entitlements {
    pub fn new(license_service_url: String, ttl_seconds: u64) -> Self {
        Self {
            client: reqwest::Client::builder()
                .timeout(Duration::from_secs(10))
                .build()
                .expect("reqwest client builds with default TLS config"),
            license_service_url,
            ttl: Duration::from_secs(ttl_seconds),
            cache: Mutex::new(HashMap::new()),
        }
    }

    /// Hash the raw key for the cache index rather than holding it. This
    /// is a purely local index and is never compared against anything
    /// License-Service stores, so it need not match that service's hash.
    fn cache_index(raw_key: &str) -> String {
        let mut hasher = Sha256::new();
        hasher.update(raw_key.as_bytes());
        format!("{:x}", hasher.finalize())
    }

    fn cached(&self, index: &str) -> Option<Entitlement> {
        let cache = self.cache.lock().ok()?;
        let (entitlement, at) = cache.get(index)?;
        if at.elapsed() < self.ttl {
            Some(entitlement.clone())
        } else {
            None
        }
    }

    pub async fn validate(&self, raw_key: &str) -> Result<Entitlement, EntitlementError> {
        let index = Self::cache_index(raw_key);

        if let Some(entitlement) = self.cached(&index) {
            if !entitlement.sync_enabled {
                return Err(EntitlementError::Denied("sync not enabled (cached)".into()));
            }
            return Ok(entitlement);
        }

        let url = format!(
            "{}/v1/licenses/entitlements",
            self.license_service_url.trim_end_matches('/')
        );

        let resp = self
            .client
            .get(&url)
            .bearer_auth(raw_key)
            .send()
            .await
            .map_err(|e| EntitlementError::Unavailable(e.to_string()))?;

        if !resp.status().is_success() {
            // Matches the Python `raise_for_status()` path, which fell
            // into the `except` that raises Unavailable — a non-2xx from
            // License-Service means we could not get an answer, not that
            // the answer was no.
            return Err(EntitlementError::Unavailable(format!(
                "license service returned {}",
                resp.status()
            )));
        }

        let data: serde_json::Value = resp
            .json()
            .await
            .map_err(|e| EntitlementError::Unavailable(e.to_string()))?;

        if !data.is_object() {
            return Err(EntitlementError::Unavailable(format!(
                "expected a JSON object, got {}",
                json_type_name(&data)
            )));
        }

        if !data.get("valid").and_then(|v| v.as_bool()).unwrap_or(false) {
            let reason = data
                .get("reason")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
                .unwrap_or("invalid");
            return Err(EntitlementError::Denied(reason.to_string()));
        }

        let tenant_key = data
            .get("license_key_hash")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty());

        let Some(tenant_key) = tenant_key else {
            // A valid response must always carry the tenant id. A missing
            // one points at a License-Service bug or contract drift, not
            // an invalid caller — so Unavailable, not Denied.
            return Err(EntitlementError::Unavailable(
                "entitlements response missing license_key_hash".into(),
            ));
        };

        let entitlement = Entitlement {
            tenant_key: tenant_key.to_string(),
            sync_enabled: data
                .get("sync_enabled")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
        };

        if let Ok(mut cache) = self.cache.lock() {
            cache.insert(index, (entitlement.clone(), Instant::now()));
        }

        if !entitlement.sync_enabled {
            return Err(EntitlementError::Denied("sync not enabled".into()));
        }
        Ok(entitlement)
    }
}

fn json_type_name(v: &serde_json::Value) -> &'static str {
    match v {
        serde_json::Value::Null => "NoneType",
        serde_json::Value::Bool(_) => "bool",
        serde_json::Value::Number(_) => "number",
        serde_json::Value::String(_) => "str",
        serde_json::Value::Array(_) => "list",
        serde_json::Value::Object(_) => "dict",
    }
}

/// Pull the raw key out of an `Authorization: Bearer <key>` header.
///
/// Split on the first run of whitespace and require a non-empty remainder,
/// matching Python's `split(None, 1)`.
pub fn extract_bearer(authorization: Option<&str>) -> Option<String> {
    let header = authorization?;
    let mut parts = header.splitn(2, char::is_whitespace);
    let scheme = parts.next()?;
    if !scheme.eq_ignore_ascii_case("bearer") {
        return None;
    }
    let rest = parts.next()?.trim();
    if rest.is_empty() {
        None
    } else {
        Some(rest.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::extract_bearer;

    #[test]
    fn accepts_a_well_formed_header() {
        assert_eq!(
            extract_bearer(Some("Bearer abc123")).as_deref(),
            Some("abc123")
        );
    }

    #[test]
    fn scheme_is_case_insensitive() {
        assert_eq!(extract_bearer(Some("bearer abc")).as_deref(), Some("abc"));
        assert_eq!(extract_bearer(Some("BEARER abc")).as_deref(), Some("abc"));
    }

    #[test]
    fn trims_surrounding_whitespace_in_the_key() {
        assert_eq!(
            extract_bearer(Some("Bearer   abc  ")).as_deref(),
            Some("abc")
        );
    }

    #[test]
    fn rejects_everything_malformed() {
        for header in [
            None,
            Some(""),
            Some("abc123"),
            Some("Bearer"),
            Some("Bearer   "),
            Some("Basic abc"),
        ] {
            assert!(extract_bearer(header).is_none(), "should reject {header:?}");
        }
    }
}
