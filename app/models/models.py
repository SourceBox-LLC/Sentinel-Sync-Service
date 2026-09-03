"""Data model for Sentinel-Sync-Service.

One generic table, not a per-model schema kept in lockstep with Command
Center's own ~19 models: a push is always just an upsert keyed by
(tenant_key, table_name, row_id). This avoids coordinating a schema
migration across two repos every time a Command Center model gains or
drops a column — the payload is just JSONB, opaque to this service.

tenant_key is License-Service's `key_hash` (see app/core/entitlements.py)
— a stable, server-derived identifier for the license that pushed the
row, NOT anything the caller supplies in the request body. That's the
entire tenant-isolation boundary: one install can never read or
overwrite another's rows because it can never present another
license's key.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, Column, DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB

from app.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)


class SyncedRow(Base):
    __tablename__ = "synced_rows"

    tenant_key = Column(String(64), primary_key=True)
    table_name = Column(String(100), primary_key=True)
    row_id = Column(String(64), primary_key=True)

    payload = Column(JSONB, nullable=False)
    # The synced row's own updated_at/created_at/timestamp from Command
    # Center — NOT this row's own bookkeeping (see synced_at below).
    # Lets a future "give me everything changed since X" read endpoint
    # exist without re-deriving that field from inside payload.
    source_updated_at = Column(DateTime, nullable=False)
    synced_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)
    deleted = Column(Boolean, nullable=False, default=False, server_default="false")

    __table_args__ = (
        # The hot query this whole table exists to serve, once a read
        # side (cloud portal / restore tooling) is built: "everything
        # for this tenant+table, not tombstoned."
        Index("ix_synced_rows_tenant_table", "tenant_key", "table_name"),
    )
