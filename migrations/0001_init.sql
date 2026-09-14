-- Mirrors the Alembic 0001 revision exactly: this table already exists in
-- production with rows in it, so the Rust service must adopt the schema
-- rather than define its own. Column types, lengths, the composite
-- primary key and the index name are all carried over unchanged.
--
-- IF NOT EXISTS throughout because the live database is already at this
-- state — the first Rust deploy must be a no-op against it, not a
-- failure and not a rewrite.
CREATE TABLE IF NOT EXISTS synced_rows (
    tenant_key        VARCHAR(64)  NOT NULL,
    table_name        VARCHAR(100) NOT NULL,
    row_id            VARCHAR(64)  NOT NULL,
    payload           JSONB        NOT NULL,
    source_updated_at TIMESTAMP    NOT NULL,
    synced_at         TIMESTAMP    NOT NULL,
    deleted           BOOLEAN      NOT NULL DEFAULT false,
    PRIMARY KEY (tenant_key, table_name, row_id)
);

CREATE INDEX IF NOT EXISTS ix_synced_rows_tenant_table
    ON synced_rows (tenant_key, table_name);
