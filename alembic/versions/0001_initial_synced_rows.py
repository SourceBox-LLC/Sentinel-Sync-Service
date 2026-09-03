"""initial synced_rows table

Revision ID: 0001
Revises:
Create Date: 2026-09-02

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "synced_rows",
        sa.Column("tenant_key", sa.String(length=64), primary_key=True),
        sa.Column("table_name", sa.String(length=100), primary_key=True),
        sa.Column("row_id", sa.String(length=64), primary_key=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(), nullable=False),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index(
        "ix_synced_rows_tenant_table",
        "synced_rows",
        ["tenant_key", "table_name"],
    )


def downgrade() -> None:
    op.drop_index("ix_synced_rows_tenant_table", table_name="synced_rows")
    op.drop_table("synced_rows")
