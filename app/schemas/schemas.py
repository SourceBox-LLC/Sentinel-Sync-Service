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
