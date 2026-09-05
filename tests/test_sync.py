"""Tests for POST /v1/sync/push — auth/entitlement gating, upsert
semantics, deletion reconciliation, and tenant isolation.
"""

from __future__ import annotations

import pytest

from app.api import sync as sync_api
from app.core.config import settings
from app.core.entitlements import Entitlement, EntitlementCheckUnavailable, EntitlementDenied
from app.models.models import SyncedRow

AUTH = {"Authorization": "Bearer slk_test_key"}


def _mock_entitled(monkeypatch, tenant_key="tenant-a"):
    async def _fake(raw_key):
        return Entitlement(tenant_key=tenant_key, sync_enabled=True)

    monkeypatch.setattr(sync_api, "validate_key", _fake)


def _mock_denied(monkeypatch, reason="revoked"):
    async def _fake(raw_key):
        raise EntitlementDenied(reason)

    monkeypatch.setattr(sync_api, "validate_key", _fake)


def _mock_unavailable(monkeypatch):
    async def _fake(raw_key):
        raise EntitlementCheckUnavailable("boom")

    monkeypatch.setattr(sync_api, "validate_key", _fake)


# ── Auth / entitlement gating ───────────────────────────────────────


def test_missing_authorization_is_401(client):
    r = client.post("/v1/sync/push", json={"table": "cameras", "rows": []})
    assert r.status_code == 401


def test_malformed_authorization_is_401(client):
    r = client.post(
        "/v1/sync/push", json={"table": "cameras", "rows": []},
        headers={"Authorization": "NotBearer x"},
    )
    assert r.status_code == 401


def test_denied_entitlement_is_403(monkeypatch, client):
    _mock_denied(monkeypatch)
    r = client.post("/v1/sync/push", json={"table": "cameras", "rows": []}, headers=AUTH)
    assert r.status_code == 403


def test_unavailable_entitlement_check_is_502(monkeypatch, client):
    _mock_unavailable(monkeypatch)
    r = client.post("/v1/sync/push", json={"table": "cameras", "rows": []}, headers=AUTH)
    assert r.status_code == 502


def test_too_many_rows_is_413(monkeypatch, client):
    _mock_entitled(monkeypatch)
    rows = [{"id": str(i), "updated_at": "2026-01-01T00:00:00Z", "data": {}} for i in range(settings.MAX_ROWS_PER_PUSH + 1)]
    r = client.post("/v1/sync/push", json={"table": "cameras", "rows": rows}, headers=AUTH)
    assert r.status_code == 413


# ── Upsert semantics ─────────────────────────────────────────────────


def test_push_creates_rows(monkeypatch, client, db_session):
    _mock_entitled(monkeypatch)
    r = client.post(
        "/v1/sync/push",
        json={
            "table": "cameras",
            "rows": [{"id": "cam-1", "updated_at": "2026-01-01T00:00:00Z", "data": {"name": "Front"}}],
        },
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["accepted"] == 1

    row = db_session.query(SyncedRow).filter_by(tenant_key="tenant-a", table_name="cameras", row_id="cam-1").one()
    assert row.payload["name"] == "Front"
    assert row.deleted is False


def test_repushing_same_row_id_updates_payload(monkeypatch, client, db_session):
    _mock_entitled(monkeypatch)
    body = {"table": "cameras", "rows": [{"id": "cam-1", "updated_at": "2026-01-01T00:00:00Z", "data": {"name": "Front"}}]}
    client.post("/v1/sync/push", json=body, headers=AUTH)

    body2 = {"table": "cameras", "rows": [{"id": "cam-1", "updated_at": "2026-01-02T00:00:00Z", "data": {"name": "Renamed"}}]}
    client.post("/v1/sync/push", json=body2, headers=AUTH)

    rows = db_session.query(SyncedRow).filter_by(tenant_key="tenant-a", table_name="cameras", row_id="cam-1").all()
    assert len(rows) == 1
    assert rows[0].payload["name"] == "Renamed"


# ── Deletion reconciliation ──────────────────────────────────────────


def test_known_ids_tombstones_rows_no_longer_present(monkeypatch, client, db_session):
    _mock_entitled(monkeypatch)
    client.post(
        "/v1/sync/push",
        json={"table": "cameras", "rows": [
            {"id": "cam-1", "updated_at": "2026-01-01T00:00:00Z", "data": {}},
            {"id": "cam-2", "updated_at": "2026-01-01T00:00:00Z", "data": {}},
        ], "known_ids": ["cam-1", "cam-2"]},
        headers=AUTH,
    )

    # cam-2 no longer exists locally — next push's known_ids omits it.
    r = client.post(
        "/v1/sync/push",
        json={"table": "cameras", "rows": [], "known_ids": ["cam-1"]},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["tombstoned"] == 1

    cam1 = db_session.query(SyncedRow).filter_by(tenant_key="tenant-a", table_name="cameras", row_id="cam-1").one()
    cam2 = db_session.query(SyncedRow).filter_by(tenant_key="tenant-a", table_name="cameras", row_id="cam-2").one()
    assert cam1.deleted is False
    assert cam2.deleted is True


def test_row_reappearing_in_known_ids_is_revived(monkeypatch, client, db_session):
    _mock_entitled(monkeypatch)
    client.post(
        "/v1/sync/push",
        json={"table": "cameras", "rows": [{"id": "cam-1", "updated_at": "2026-01-01T00:00:00Z", "data": {}}],
              "known_ids": ["cam-1"]},
        headers=AUTH,
    )
    client.post(
        "/v1/sync/push",
        json={"table": "cameras", "rows": [], "known_ids": []},
        headers=AUTH,
    )
    row = db_session.query(SyncedRow).filter_by(tenant_key="tenant-a", table_name="cameras", row_id="cam-1").one()
    assert row.deleted is True

    # cam-1 exists again locally (e.g. re-registered) — known_ids says so.
    client.post(
        "/v1/sync/push",
        json={"table": "cameras", "rows": [], "known_ids": ["cam-1"]},
        headers=AUTH,
    )
    db_session.expire_all()
    row = db_session.query(SyncedRow).filter_by(tenant_key="tenant-a", table_name="cameras", row_id="cam-1").one()
    assert row.deleted is False


def test_no_known_ids_never_tombstones(monkeypatch, client, db_session):
    """Regression: log/event tables (motion_events, etc.) never send
    known_ids — a push with rows but no known_ids must never delete
    anything, even implicitly."""
    _mock_entitled(monkeypatch)
    client.post(
        "/v1/sync/push",
        json={"table": "motion_events", "rows": [{"id": "1", "updated_at": "2026-01-01T00:00:00Z", "data": {}}]},
        headers=AUTH,
    )
    r = client.post(
        "/v1/sync/push",
        json={"table": "motion_events", "rows": [{"id": "2", "updated_at": "2026-01-02T00:00:00Z", "data": {}}]},
        headers=AUTH,
    )
    assert r.json()["tombstoned"] == 0
    row1 = db_session.query(SyncedRow).filter_by(tenant_key="tenant-a", table_name="motion_events", row_id="1").one()
    assert row1.deleted is False


# ── Tenant isolation ─────────────────────────────────────────────────


def test_same_row_id_different_tenants_do_not_collide(monkeypatch, client, db_session):
    _mock_entitled(monkeypatch, tenant_key="tenant-a")
    client.post(
        "/v1/sync/push",
        json={"table": "cameras", "rows": [{"id": "cam-1", "updated_at": "2026-01-01T00:00:00Z", "data": {"name": "A"}}]},
        headers=AUTH,
    )

    _mock_entitled(monkeypatch, tenant_key="tenant-b")
    client.post(
        "/v1/sync/push",
        json={"table": "cameras", "rows": [{"id": "cam-1", "updated_at": "2026-01-01T00:00:00Z", "data": {"name": "B"}}]},
        headers=AUTH,
    )

    a = db_session.query(SyncedRow).filter_by(tenant_key="tenant-a", table_name="cameras", row_id="cam-1").one()
    b = db_session.query(SyncedRow).filter_by(tenant_key="tenant-b", table_name="cameras", row_id="cam-1").one()
    assert a.payload["name"] == "A"
    assert b.payload["name"] == "B"


def test_tenant_b_known_ids_cannot_tombstone_tenant_a_rows(monkeypatch, client, db_session):
    _mock_entitled(monkeypatch, tenant_key="tenant-a")
    client.post(
        "/v1/sync/push",
        json={"table": "cameras", "rows": [{"id": "cam-1", "updated_at": "2026-01-01T00:00:00Z", "data": {}}],
              "known_ids": ["cam-1"]},
        headers=AUTH,
    )

    _mock_entitled(monkeypatch, tenant_key="tenant-b")
    client.post(
        "/v1/sync/push",
        json={"table": "cameras", "rows": [], "known_ids": []},
        headers=AUTH,
    )

    a = db_session.query(SyncedRow).filter_by(tenant_key="tenant-a", table_name="cameras", row_id="cam-1").one()
    assert a.deleted is False


# ── Read API: GET /tables and GET /rows ──────────────────────────────


def _push(client, table, rows, known_ids=None):
    body = {"table": table, "rows": rows}
    if known_ids is not None:
        body["known_ids"] = known_ids
    return client.post("/v1/sync/push", json=body, headers=AUTH)


def _row(i, **data):
    return {"id": f"r{i:03d}", "updated_at": "2026-01-01T00:00:00Z", "data": data or {"n": i}}


def test_read_requires_auth(client):
    assert client.get("/v1/sync/tables").status_code == 401
    assert client.get("/v1/sync/rows", params={"table": "cameras"}).status_code == 401


def test_tables_summarises_what_is_mirrored(monkeypatch, client):
    _mock_entitled(monkeypatch)
    _push(client, "cameras", [_row(1), _row(2)], known_ids=["r001", "r002"])
    _push(client, "motion_events", [_row(3)])
    # Tombstone one camera so the deleted count is exercised.
    _push(client, "cameras", [], known_ids=["r001"])

    body = client.get("/v1/sync/tables", headers=AUTH).json()
    by_table = {t["table"]: t for t in body["tables"]}

    assert by_table["cameras"]["rows"] == 2
    assert by_table["cameras"]["deleted"] == 1
    assert by_table["motion_events"]["rows"] == 1
    assert by_table["motion_events"]["deleted"] == 0


def test_rows_returns_the_stored_payload(monkeypatch, client):
    _mock_entitled(monkeypatch)
    _push(client, "cameras", [_row(1, name="Front", continuous_24_7=True)])

    body = client.get("/v1/sync/rows", params={"table": "cameras"}, headers=AUTH).json()

    assert len(body["rows"]) == 1
    assert body["rows"][0]["id"] == "r001"
    # The raw-column payload comes back intact — this is the property a
    # restore depends on.
    assert body["rows"][0]["data"] == {"name": "Front", "continuous_24_7": True}
    assert body["next_cursor"] is None


def test_rows_paginates_with_a_stable_cursor(monkeypatch, client):
    _mock_entitled(monkeypatch)
    _push(client, "motion_events", [_row(i) for i in range(1, 8)])

    seen, cursor, pages = [], None, 0
    while True:
        params = {"table": "motion_events", "limit": 3}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/v1/sync/rows", params=params, headers=AUTH).json()
        seen.extend(r["id"] for r in body["rows"])
        pages += 1
        cursor = body["next_cursor"]
        if not cursor:
            break
        assert pages < 10, "pagination failed to terminate"

    # Every row exactly once, in order, with no duplicates across pages.
    assert seen == [f"r{i:03d}" for i in range(1, 8)]
    assert len(seen) == len(set(seen))


def test_rows_excludes_tombstones_by_default_but_can_include_them(monkeypatch, client):
    _mock_entitled(monkeypatch)
    _push(client, "cameras", [_row(1), _row(2)], known_ids=["r001", "r002"])
    _push(client, "cameras", [], known_ids=["r001"])  # tombstones r002

    default = client.get("/v1/sync/rows", params={"table": "cameras"}, headers=AUTH).json()
    assert [r["id"] for r in default["rows"]] == ["r001"], (
        "a restore must not resurrect rows the operator deleted"
    )

    forensic = client.get(
        "/v1/sync/rows",
        params={"table": "cameras", "include_deleted": "true"},
        headers=AUTH,
    ).json()
    assert [r["id"] for r in forensic["rows"]] == ["r001", "r002"]
    assert [r["deleted"] for r in forensic["rows"]] == [False, True]


def test_reads_are_scoped_to_the_calling_tenant(monkeypatch, client):
    """The one that would be a data breach if it regressed: tenant B
    must never see tenant A's rows, even asking for the same table."""
    _mock_entitled(monkeypatch, tenant_key="tenant-a")
    _push(client, "cameras", [_row(1, name="A-secret")])

    _mock_entitled(monkeypatch, tenant_key="tenant-b")
    body = client.get("/v1/sync/rows", params={"table": "cameras"}, headers=AUTH).json()
    assert body["rows"] == []

    tables = client.get("/v1/sync/tables", headers=AUTH).json()
    assert tables["tables"] == []


def test_unknown_table_returns_an_empty_page_not_an_error(monkeypatch, client):
    _mock_entitled(monkeypatch)
    body = client.get("/v1/sync/rows", params={"table": "nope"}, headers=AUTH).json()
    assert body["rows"] == []
    assert body["next_cursor"] is None
