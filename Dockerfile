# ---------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------
# Pinned to a specific Rust version rather than :latest so a toolchain
# release can't change what ships without a commit saying so.
FROM rust:1.98-slim-bookworm AS builder

WORKDIR /build

# Dependency layer first: Cargo.toml/lock change far less often than
# src/, so a source-only edit reuses the compiled dependency graph
# instead of rebuilding ~200 crates.
COPY Cargo.toml Cargo.lock ./
RUN mkdir src && echo 'fn main() {}' > src/main.rs \
 && cargo build --release --locked \
 && rm -rf src

COPY src ./src
COPY migrations ./migrations
# Cargo caches on mtime; the stub main.rs above means the real one can
# look "already built" without this.
RUN touch src/main.rs && cargo build --release --locked

# ---------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------
# debian-slim, not scratch/distroless: the binary needs glibc, and the
# few MB buys a shell for `fly ssh console`, which the backup and
# debugging runbooks both assume.
FROM debian:bookworm-slim

# ca-certificates only — TLS is rustls, so there is no OpenSSL to
# install, and Postgres is reached over sqlx's pure-Rust driver rather
# than libpq. The Python image needed libpq5 and curl; this one needs
# neither.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Unprivileged. The Python image ran as root; nothing here needs it.
#
# Named `syncsvc`, not `sync`: Debian already ships a system account
# called `sync` (uid 4, shell /bin/sync), so `useradd sync` fails with
# exit 9, "username already in use".
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin syncsvc
USER syncsvc

COPY --from=builder /build/target/release/sentinel-sync-service /usr/local/bin/sentinel-sync-service

EXPOSE 8000
CMD ["/usr/local/bin/sentinel-sync-service"]
