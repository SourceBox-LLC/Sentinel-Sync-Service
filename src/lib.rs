//! Library surface so integration tests can build the same router the
//! binary serves, rather than re-declaring it and drifting from it.

pub mod api;
pub mod app;
pub mod config;
pub mod entitlements;
pub mod error;
pub mod ratelimit;

pub use app::{build_router, AppState, VERSION};
