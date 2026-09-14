//! Environment configuration.
//!
//! Every name and default here is carried over verbatim from the Python
//! service's `app/core/config.py`. The Fly app's existing secrets are set
//! against these names, so changing one silently reverts it to a default
//! at the next deploy.

use std::env;

#[derive(Debug, Clone)]
pub struct Config {
    pub database_url: String,
    pub license_service_url: String,
    pub entitlement_cache_seconds: u64,
    pub max_rows_per_push: usize,
    pub port: u16,
}

fn var_or(key: &str, default: &str) -> String {
    env::var(key).unwrap_or_else(|_| default.to_string())
}

impl Config {
    pub fn from_env() -> Self {
        // The Python service ran SQLAlchemy, whose URL scheme carries a
        // driver suffix (`postgresql+psycopg://`). sqlx does not
        // understand that, and the Fly secret still holds the old form —
        // so normalise rather than require the secret to be rewritten.
        let raw = var_or(
            "DATABASE_URL",
            "postgresql://sync:sync@localhost:5432/sentinel_sync",
        );
        let database_url = normalize_database_url(&raw);

        Self {
            database_url,
            license_service_url: var_or("LICENSE_SERVICE_URL", "https://sentinel-license.fly.dev"),
            entitlement_cache_seconds: var_or("ENTITLEMENT_CACHE_SECONDS", "300")
                .parse()
                .unwrap_or(300),
            max_rows_per_push: var_or("MAX_ROWS_PER_PUSH", "1000").parse().unwrap_or(1000),
            port: var_or("PORT", "8000").parse().unwrap_or(8000),
        }
    }
}

/// Strip SQLAlchemy's `+driver` from a database URL scheme.
///
/// `postgresql+psycopg://host/db` -> `postgresql://host/db`. Left alone if
/// there is no `+`, so a plain URL passes through untouched.
pub fn normalize_database_url(url: &str) -> String {
    match url.split_once("://") {
        Some((scheme, rest)) => match scheme.split_once('+') {
            Some((base, _driver)) => format!("{base}://{rest}"),
            None => url.to_string(),
        },
        None => url.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::normalize_database_url;

    #[test]
    fn strips_sqlalchemy_driver_suffix() {
        assert_eq!(
            normalize_database_url("postgresql+psycopg://u:p@h:5432/db"),
            "postgresql://u:p@h:5432/db"
        );
    }

    #[test]
    fn leaves_a_plain_url_alone() {
        let plain = "postgres://u:p@h:5432/db";
        assert_eq!(normalize_database_url(plain), plain);
    }

    #[test]
    fn does_not_mangle_a_password_containing_a_plus() {
        // The `+` we care about is in the scheme, never the credentials.
        let url = "postgresql://user:pa+ss@host:5432/db";
        assert_eq!(normalize_database_url(url), url);
    }
}
