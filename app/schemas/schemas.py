from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class PushRow(BaseModel):
    id: str = Field(max_length=64)
    updated_at: datetime
    data: dict[str, Any]


class PushRequest(BaseModel):
    table: str = Field(max_length=100)
    rows: list[PushRow] = Field(default_factory=list)
    # Present only for tables that want deletion reconciliation (see
    # Command Center's sync_client.py SyncTableSpec.reconcile_deletes) —
    # the complete current set of local row ids for this table. Absent
    # (None) means "don't touch tombstone state for this table," which
    # is the deliberate default for high-volume log/event tables whose
    # local retention deletes must never propagate.
    known_ids: Optional[list[str]] = None


class PushResponse(BaseModel):
    accepted: int
    tombstoned: int = 0


class TableSummary(BaseModel):
    table: str
    rows: int
    deleted: int


class TablesResponse(BaseModel):
    """What this tenant has mirrored, for a restore to plan against —
    and for an operator to sanity-check that syncing is actually
    working before they ever need it."""

    tables: list[TableSummary]


class StoredRow(BaseModel):
    id: str
    data: dict[str, Any]
    source_updated_at: datetime
    synced_at: datetime
    deleted: bool


class RowsResponse(BaseModel):
    table: str
    rows: list[StoredRow]
    # Opaque continuation token; None once the last page is returned.
    # Keyset pagination on row_id rather than OFFSET: a restore of a
    # high-volume table (motion_events runs to 100K+ rows) walks the
    # whole thing, and OFFSET makes each successive page more expensive
    # than the last.
    next_cursor: Optional[str] = None
