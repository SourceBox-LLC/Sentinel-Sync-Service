"""Sync push endpoint — the one thing this service does. See
app/core/entitlements.py for auth/tenant-scoping and
app/models/models.py for the generic mirror-table rationale.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.entitlements import EntitlementCheckUnavailable, EntitlementDenied, validate_key
from app.core.limiter import limiter
from app.models.models import SyncedRow
from app.schemas.schemas import PushRequest, PushResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/sync", tags=["sync"])


def _extract_raw_key(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    return parts[1].strip()


@router.post("/push", response_model=PushResponse)
@limiter.limit("120/minute")
async def push(
    request: Request,
    payload: PushRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> PushResponse:
    raw_key = _extract_raw_key(authorization)

    try:
        entitlement = await validate_key(raw_key)
    except EntitlementDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except EntitlementCheckUnavailable as exc:
        # 502, not 403: this means "we couldn't verify," not "we
        # verified and the answer is no." A well-behaved caller (Command
        # Center's sync_client.py) treats this push as failed and
        # retries next cycle rather than treating it as a permanent
        # rejection — see entitlements.py's module docstring.
        raise HTTPException(status_code=502, detail=f"entitlement check unavailable: {exc}") from exc

    if len(payload.rows) > settings.MAX_ROWS_PER_PUSH:
        raise HTTPException(
            status_code=413,
            detail=f"too many rows in one push (max {settings.MAX_ROWS_PER_PUSH})",
        )

    tenant_key = entitlement.tenant_key
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
