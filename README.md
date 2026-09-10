# Sentinel Sync Service

Cloud data-sync mirror for self-hosted Sentinel Command Center installs (`AUTH_PROVIDER=local`). A self-hosted operator's local SQLite database is always the source of truth — this service only ever receives a one-way push of changed rows, on the same opt-in license entitlement model as [Sentinel-License-Service](https://github.com/SourceBox-LLC/Sentinel-License-Service). It exists so someone running Command Center entirely on their own box can still opt into "my data is also backed up in the cloud" without giving up local-first operation.

A genuinely separate service from both Command Center and License-Service — its own codebase, its own deploy target, its own Postgres database. Self-hosted operators' copy of Command Center never contains this service's code.

All three hosted services run Postgres (Command Center and License-Service migrated on 2026-09-07), so that is no longer what sets this one apart. What still does is **real Alembic migrations** rather than the boot-time `ALTER TABLE` sweep its siblings use: the generic `synced_rows` table (`tenant_key, table_name, row_id, payload jsonb, ...`) doesn't need per-model migrations kept in lockstep with Command Center's own schema, so a push is always just an upsert.

## Run locally

Needs a real Postgres — there is no SQLite fallback here, because the `payload jsonb` column has no portable SQLite equivalent. (Command Center and License-Service do keep a SQLite path, for self-hosted installs.)

```bash
docker run -d --name sentinel-sync-pg -e POSTGRES_USER=sync -e POSTGRES_PASSWORD=sync \
  -e POSTGRES_DB=sentinel_sync -p 5432:5432 postgres:16-alpine

cp .env.example .env   # defaults work as-is for local dev
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

## Running tests

Also needs real Postgres — point `TEST_DATABASE_URL` at a throwaway database (defaults to `postgresql+psycopg://sync:sync@localhost:55432/sentinel_sync_test` if unset):

```bash
docker run -d --name sentinel-sync-test-pg -e POSTGRES_USER=sync -e POSTGRES_PASSWORD=sync \
  -e POSTGRES_DB=sentinel_sync_test -p 55432:5432 postgres:16-alpine

uv run pytest
```

## API

`POST /v1/sync/push` — `Authorization: Bearer slk_<key>` (the same key Command Center already uses for Sentinel AI licensing — sync is a separate opt-in entitlement on that key, validated against License-Service's `GET /v1/licenses/entitlements` on each push, cached briefly). Body: `{table, rows: [{id, updated_at, data}], known_ids?: [...]}`. `known_ids`, when present, is the complete current set of local row ids for that table — anything previously synced under this tenant+table but missing from it gets tombstoned. Omitted entirely for high-volume log/event tables (motion events, etc.), whose local retention deletes must never propagate to the cloud copy. See `app/api/sync.py` and `app/core/entitlements.py` for the full contract, and Command Center's `backend/app/core/sync_client.py` for the client side.

`GET /v1/sync/tables` — what this tenant has mirrored and how much of it. A restore plans against it, and it's the quickest way for an operator to confirm syncing is actually working *before* they need it.

`GET /v1/sync/rows?table=…` — one page of mirrored rows. Keyset pagination on `row_id` (`cursor` / `next_cursor`, `limit` up to 1000), not OFFSET: a restore walks whole tables — `motion_events` runs to 100K+ rows on an active install — and OFFSET makes every successive page more expensive than the last. Ordering by `row_id` also keeps paging stable under concurrent pushes.

Tombstoned rows are excluded by default, since a restore that resurrects cameras the operator deliberately deleted would be actively wrong. `include_deleted=true` is there for forensics, where "what was removed, and when" is the actual question.

Every route derives its tenant from the validated licence key server-side — never from anything the caller sends — so read and write share one tenant boundary by construction.

`GET /health` — pure liveness. `GET /health/ready` — 503 if the database is down.

## Restoring

This service only stores and serves the mirror; the restore itself runs on the Command Center being recovered:

```bash
cd backend
uv run python scripts/restore_from_cloud.py --list      # what's up here
uv run python scripts/restore_from_cloud.py --dry-run   # what would be written
uv run python scripts/restore_from_cloud.py             # do it
```

Full procedure, including what deliberately isn't mirrored (node API keys, evidence blobs), is in Command Center's `docs/runbooks/DISASTER_RECOVERY.md` under "Self-hosted installs".

## Deploy

Single-stage `Dockerfile` (no frontend build — this service has no UI). `fly.toml`'s `release_command` runs `alembic upgrade head` before each deploy starts serving traffic.

**Deploys from CI.** Every push to `master` runs the tests against a real Postgres, then `flyctl deploy --ha=false`. (`--ha=false` because Fly provisions two machines by default; it did exactly that on the manual deploy and the extra had to be scaled away by hand. No `--strategy` override is needed here — unlike the sibling License service, this app has no volume.)

Deploy automation was deferred while this was new infrastructure. That turned out worse than what it avoided: `fly.toml` became a file that did nothing, and a scale-to-zero change merged with CI fully green on 2026-09-09 without ever reaching Fly.

**Scales to zero.** Self-hosted installs push on a 30-minute background tick, so this is idle ~95% of the time. Safe because boot is ~3s (inside the ~8s Fly's proxy waits for an auto-started machine to bind) and a failed push is fail-soft *and lossless*: `push_pending_changes` never raises, and cursors only advance on confirmed success, so a missed cycle's data simply waits for the next tick with the operator's local SQLite authoritative throughout.

## Status

**Deployed and live** at `https://sentinel-sync.fly.dev`, on the shared `sentinel-postgres` cluster in its own `sentinel_sync` database, access-isolated by role from the other two.

Verified by the full test suite (unit tests against a real Postgres, including tenant-isolation and deletion-reconciliation regressions) plus a live cross-service integration check against a running License-Service instance: valid + sync-enabled key → 200 with the row correctly scoped by tenant; unknown key → 403; License-Service unreachable → 502.

This section previously read "Not yet deployed — no Postgres instance or Fly app provisioned yet", which stopped being true on 2026-09-07.

**No backup dump job, deliberately.** This database holds a *mirror*; every row was pushed from an operator's local SQLite, which stays the source of truth. Losing it entirely costs one sync cycle. It is covered by the cluster-level snapshot — see `DISASTER_RECOVERY.md` in the Command Center repo.
