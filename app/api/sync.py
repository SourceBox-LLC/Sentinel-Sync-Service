"""Sync endpoints: one write path in, two read paths back out.

`POST /push` receives mirrored rows from a self-hosted Command Center.
`GET /tables` and `GET /rows` hand them back — without those, this was
a write-only archive, which is to say not a backup at all: data went up
and nothing could get it out again.

See app/core/entitlements.py for auth/tenant-scoping (every route here
derives its tenant from the validated key, never from the request body)
and app/models/models.py for the generic mirror-table rationale.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.entitlements import EntitlementCheckUnavailable, EntitlementDenied, validate_key
from app.core.limiter import limiter
from app.models.models import SyncedRow
from app.schemas.schemas import (
    PushRequest,
    PushResponse,
    RowsResponse,
    StoredRow,
    TablesResponse,
    TableSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/sync", tags=["sync"])


def _extract_raw_key(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    return parts[1].strip()


async def _tenant_from_auth(authorization: str | None) -> str:
    """Validate the Bearer key and return the tenant it maps to.

    Shared by every route here, so read and write agree by construction
    on both the auth rules and the tenant boundary — the scoping key is
    always derived server-side from the validated key, never taken from
    anything the caller sends.
    """
    raw_key = _extract_raw_key(authorization)
    try:
        entitlement = await validate_key(raw_key)
    except EntitlementDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except EntitlementCheckUnavailable as exc:
        # 502, not 403: this means "we couldn't verify," not "we
        # verified and the answer is no." A well-behaved caller (Command
        # Center's sync_client.py) treats this as a failed attempt and
        # retries rather than treating it as a permanent rejection —
        # see entitlements.py's module docstring.
        raise HTTPException(status_code=502, detail=f"entitlement check unavailable: {exc}") from exc
    return entitlement.tenant_key


@router.post("/push", response_model=PushResponse)
@limiter.limit("120/minute")
async def push(
    request: Request,
    payload: PushRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> PushResponse:
    tenant_key = await _tenant_from_auth(authorization)

    if len(payload.rows) > settings.MAX_ROWS_PER_PUSH:
        raise HTTPException(
            status_code=413,
            detail=f"too many rows in one push (max {settings.MAX_ROWS_PER_PUSH})",
        )

    now = datetime.now(tz=UTC).replace(tzinfo=None)

    tombstoned = 0
    if payload.known_ids is not None:
        existing_ids = {
            r.row_id
            for r in db.query(SyncedRow.row_id)
            .filter_by(tenant_key=tenant_key, table_name=payload.table)
            .all()
        }
        keep = set(payload.known_ids)
        to_tombstone = existing_ids - keep
        to_revive = existing_ids & keep

        if to_tombstone:
            tombstoned = (
                db.query(SyncedRow)
                .filter(
                    SyncedRow.tenant_key == tenant_key,
                    SyncedRow.table_name == payload.table,
                    SyncedRow.row_id.in_(to_tombstone),
                )
                .update({"deleted": True, "synced_at": now}, synchronize_session=False)
            )
        if to_revive:
            db.query(SyncedRow).filter(
                SyncedRow.tenant_key == tenant_key,
                SyncedRow.table_name == payload.table,
                SyncedRow.row_id.in_(to_revive),
                SyncedRow.deleted.is_(True),
            ).update({"deleted": False, "synced_at": now}, synchronize_session=False)

    accepted = 0
    for row in payload.rows:
        stmt = insert(SyncedRow).values(
            tenant_key=tenant_key,
            table_name=payload.table,
            row_id=row.id,
            payload=row.data,
            source_updated_at=row.updated_at.replace(tzinfo=None),
            synced_at=now,
            deleted=False,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[SyncedRow.tenant_key, SyncedRow.table_name, SyncedRow.row_id],
            set_={
                "payload": stmt.excluded.payload,
                "source_updated_at": stmt.excluded.source_updated_at,
                "synced_at": stmt.excluded.synced_at,
                "deleted": False,
            },
        )
        db.execute(stmt)
        accepted += 1

    db.commit()

    return PushResponse(accepted=accepted, tombstoned=tombstoned)


@router.get("/tables", response_model=TablesResponse)
@limiter.limit("120/minute")
async def list_tables(
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> TablesResponse:
    """What this tenant has mirrored, and how much of it.

    Two jobs: a restore plans against it, and an operator can check
    that syncing is actually working — until this existed there was no
    way to answer "is my data really up there?" short of waiting for a
    disaster to find out.
    """
    tenant_key = await _tenant_from_auth(authorization)

    rows = (
        db.query(
            SyncedRow.table_name,
            func.count().label("total"),
            func.count().filter(SyncedRow.deleted.is_(True)).label("deleted"),
        )
        .filter(SyncedRow.tenant_key == tenant_key)
        .group_by(SyncedRow.table_name)
        .order_by(SyncedRow.table_name)
        .all()
    )
    return TablesResponse(
        tables=[
            TableSummary(table=r.table_name, rows=r.total, deleted=r.deleted) for r in rows
        ]
    )


@router.get("/rows", response_model=RowsResponse)
@limiter.limit("120/minute")
async def list_rows(
    request: Request,
    table: str = Query(max_length=100),
    cursor: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=500, ge=1, le=1000),
    include_deleted: bool = Query(default=False),
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> RowsResponse:
    """One page of mirrored rows for a table.

    Keyset pagination on row_id, not OFFSET: a restore walks entire
    tables (motion_events runs to 100K+ rows on an active install) and
    OFFSET makes every successive page more expensive than the last.
    Ordering by row_id also makes paging stable against concurrent
    pushes — a row arriving mid-walk can't shift rows the caller has
    already seen onto a later page.

    Tombstoned rows are excluded by default: a restore that resurrects
    cameras the operator deliberately deleted would be actively wrong.
    `include_deleted=true` is there for forensics, where "what was
    removed, and when" is the question being asked.
    """
    tenant_key = await _tenant_from_auth(authorization)

    query = db.query(SyncedRow).filter(
        SyncedRow.tenant_key == tenant_key,
        SyncedRow.table_name == table,
    )
    if not include_deleted:
        query = query.filter(SyncedRow.deleted.is_(False))
    if cursor is not None:
        query = query.filter(SyncedRow.row_id > cursor)

    # Fetch one extra to learn whether another page exists without a
    # second COUNT query.
    found = query.order_by(SyncedRow.row_id.asc()).limit(limit + 1).all()
    has_more = len(found) > limit
    page = found[:limit]

    return RowsResponse(
        table=table,
        rows=[
            StoredRow(
                id=r.row_id,
                data=r.payload,
                source_updated_at=r.source_updated_at,
                synced_at=r.synced_at,
                deleted=r.deleted,
            )
            for r in page
        ],
        next_cursor=page[-1].row_id if has_more and page else None,
    )
