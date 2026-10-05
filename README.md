# Sentinel Sync Service

Cloud data-sync mirror for self-hosted Sentinel Command Center installs (`AUTH_PROVIDER=local`). A self-hosted operator's local database (SQLite or Postgres) is always the source of truth — this service only ever receives a one-way push of changed rows, on the same opt-in license entitlement model as [Sentinel-License-Service](https://github.com/SourceBox-LLC/Sentinel-License-Service). It exists so someone running Command Center entirely on their own box can still opt into "my data is also backed up in the cloud" without giving up local-first operation.

A genuinely separate service from both Command Center and License-Service — its own codebase, its own deploy target, its own Postgres database. Self-hosted operators' copy of Command Center never contains this service's code.

All three hosted services run Postgres, and all three apply embedded sqlx migrations at startup. What sets this one apart is the schema: one generic `synced_rows` table (`tenant_key, table_name, row_id, payload jsonb, ...`) that does not track Command Center's models, so a push is always just an upsert and a new Command Center column needs no migration here.

## Run locally

Needs a real Postgres — there is no SQLite fallback here, because the `payload jsonb` column has no portable SQLite equivalent. (Command Center keeps a SQLite path, for self-hosted installs; License-Service, like this one, is Postgres only.)

```bash
docker run -d --name sentinel-sync-pg -e POSTGRES_USER=sync -e POSTGRES_PASSWORD=sync \
  -e POSTGRES_DB=sentinel_sync -p 5432:5432 postgres:16-alpine

cargo run                  # serves on :8000; DATABASE_URL defaults to that container
```

Migrations run at startup. Configuration is environment variables only — the binary does not read a `.env` file; `.env.example` lists what exists. Point `LICENSE_SERVICE_URL` at a local License-Service to push without the hosted one.

## Running tests

Also needs real Postgres — point `TEST_DATABASE_URL` (or `DATABASE_URL`) at a throwaway database; without either, the database tests skip:

```bash
docker run -d --name sentinel-sync-test-pg -e POSTGRES_USER=sync -e POSTGRES_PASSWORD=sync \
  -e POSTGRES_DB=sentinel_sync_test -p 55432:5432 postgres:16-alpine

TEST_DATABASE_URL=postgresql://sync:sync@localhost:55432/sentinel_sync_test cargo test
```

## API

`POST /v1/sync/push` — `Authorization: Bearer slk_<key>` (the same key Command Center already uses for Sentinel AI licensing — sync is a separate opt-in entitlement on that key, validated against License-Service's `GET /v1/licenses/entitlements` on each push, cached briefly). Body: `{table, rows: [{id, updated_at, data}], known_ids?: [...]}`. `known_ids`, when present, is the complete current set of local row ids for that table — anything previously synced under this tenant+table but missing from it gets tombstoned. Omitted entirely for high-volume log/event tables (motion events, etc.), whose local retention deletes must never propagate to the cloud copy. See `src/api.rs` and `src/entitlements.rs` for the full contract, and Command Center's `backend-rs/src/sync.rs` for the client side.

`GET /v1/sync/tables` — what this tenant has mirrored and how much of it. A restore plans against it, and it's the quickest way for an operator to confirm syncing is actually working *before* they need it.

`GET /v1/sync/rows?table=…` — one page of mirrored rows. Keyset pagination on `row_id` (`cursor` / `next_cursor`, `limit` up to 1000), not OFFSET: a restore walks whole tables — `motion_events` runs to 100K+ rows on an active install — and OFFSET makes every successive page more expensive than the last. Ordering by `row_id` also keeps paging stable under concurrent pushes.

Tombstoned rows are excluded by default, since a restore that resurrects cameras the operator deliberately deleted would be actively wrong. `include_deleted=true` is there for forensics, where "what was removed, and when" is the actual question.

Every route derives its tenant from the validated licence key server-side — never from anything the caller sends — so read and write share one tenant boundary by construction.

`GET /health` — pure liveness. `GET /health/ready` — 503 if the database is down.

## Restoring

This service only stores and serves the mirror; the restore itself runs on the Command Center being recovered:

```bash
sentinel-restore-from-cloud --list       # what's up here
sentinel-restore-from-cloud --dry-run    # what would be written
sentinel-restore-from-cloud              # do it
```

The tool ships in the Command Center image (`docker compose exec app sentinel-restore-from-cloud --list` on a Compose install).

Full procedure, including what deliberately isn't mirrored (node API keys, evidence blobs), is in Command Center's `docs/runbooks/DISASTER_RECOVERY.md` under "Self-hosted installs".

## Deploy

Single-stage `Dockerfile` (no frontend build — this service has no UI). There is no `release_command`: the service applies its migrations itself at startup, under a Postgres advisory lock, so two machines starting together cannot race (`fly.toml` says why).

**Deploys from CI.** Every push to `master` runs the tests against a real Postgres, then `flyctl deploy --ha=false`. (`--ha=false` because Fly provisions two machines by default; it did exactly that on the manual deploy and the extra had to be scaled away by hand. No `--strategy` override is needed here — unlike the sibling License service, this app has no volume.)

Deploy automation was deferred while this was new infrastructure. That turned out worse than what it avoided: `fly.toml` became a file that did nothing, and a scale-to-zero change merged with CI fully green on 2026-09-09 without ever reaching Fly.

**Scales to zero.** Self-hosted installs push on a 30-minute background tick, so this is idle ~95% of the time. Safe because boot is ~3s (inside the ~8s Fly's proxy waits for an auto-started machine to bind) and a failed push is fail-soft *and lossless*: `push_pending_changes` never raises, and cursors only advance on confirmed success, so a missed cycle's data simply waits for the next tick with the operator's local database authoritative throughout.

## Status

**Deployed and live** at `https://sentinel-sync.fly.dev`, on the shared `sentinel-postgres` cluster in its own `sentinel_sync` database, access-isolated by role from the other two.

Verified by the full test suite (unit tests against a real Postgres, including tenant-isolation and deletion-reconciliation regressions) plus a live cross-service integration check against a running License-Service instance: valid + sync-enabled key → 200 with the row correctly scoped by tenant; unknown key → 403; License-Service unreachable → 502.

This section previously read "Not yet deployed — no Postgres instance or Fly app provisioned yet", which stopped being true on 2026-09-07.

**No backup dump job, deliberately.** This database holds a *mirror*; every row was pushed from an operator's local SQLite, which stays the source of truth. Losing it entirely costs one sync cycle. It is covered by the cluster-level snapshot — see `DISASTER_RECOVERY.md` in the Command Center repo.


## Implementation

Rust (axum + sqlx), ported from the original Python/FastAPI service on
2026-09-14. The wire contract did not change — Command Center's
mirror (`backend-rs/src/sync.rs`) and `sentinel-restore-from-cloud` talk
to this exactly as before, and `tests/wire_contract.rs` pins the behaviours they depend
on.

Why the port: this service scales to zero, so startup time is a product
property rather than a footnote. Fly's proxy allows an auto-started
machine roughly 8s to bind its port, and the Python service was measured
answering in ~6.2s of that budget — about 1.5s of headroom, with ~3.4s of
it spent starting Python. The Rust binary starts in ~0.2s.

```bash
cargo run                   # needs DATABASE_URL
cargo test                  # set TEST_DATABASE_URL for the wire-contract tests
```
