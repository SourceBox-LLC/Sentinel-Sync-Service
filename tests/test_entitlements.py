"""Tests for app/core/entitlements.py — the License-Service validation
call and its short TTL cache."""

from __future__ import annotations

import httpx
import pytest

from app.core import entitlements
from app.core.config import settings

_UNSET = object()


class _FakeResponse:
    def __init__(self, status_code: int = 200, json_data=_UNSET):
        self.status_code = status_code
        self._json_data = {} if json_data is _UNSET else json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._json_data


class _FakeAsyncClient:
    def __init__(self, response=None, exc=None, calls=None):
        self._response = response
        self._exc = exc
        self._calls = calls if calls is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, headers=None):
        self._calls.append(url)
        if self._exc is not None:
            raise self._exc
        return self._response


def _mock_http(monkeypatch, *, response=None, exc=None, calls=None):
    def factory(*args, **kwargs):
        return _FakeAsyncClient(response=response, exc=exc, calls=calls)

    monkeypatch.setattr(entitlements.httpx, "AsyncClient", factory)


async def test_valid_and_sync_enabled_returns_entitlement(monkeypatch):
    _mock_http(monkeypatch, response=_FakeResponse(200, {
        "valid": True, "license_key_hash": "hash123", "tier": "self_host_standard",
        "sync_enabled": True, "server_time": "x",
    }))
    result = await entitlements.validate_key("slk_test")
    assert result.tenant_key == "hash123"
    assert result.sync_enabled is True


async def test_valid_but_sync_not_enabled_raises_denied(monkeypatch):
    _mock_http(monkeypatch, response=_FakeResponse(200, {
        "valid": True, "license_key_hash": "hash123", "sync_enabled": False, "server_time": "x",
    }))
    with pytest.raises(entitlements.EntitlementDenied):
        await entitlements.validate_key("slk_test")


@pytest.mark.parametrize("reason", ["revoked", "expired", "suspended", "not_found"])
async def test_invalid_license_raises_denied(monkeypatch, reason):
    _mock_http(monkeypatch, response=_FakeResponse(200, {
        "valid": False, "reason": reason, "server_time": "x",
    }))
    with pytest.raises(entitlements.EntitlementDenied):
        await entitlements.validate_key("slk_test")


async def test_unreachable_license_service_raises_unavailable(monkeypatch):
    _mock_http(monkeypatch, exc=httpx.ConnectError("connection refused"))
    with pytest.raises(entitlements.EntitlementCheckUnavailable):
        await entitlements.validate_key("slk_test")


async def test_non_dict_response_raises_unavailable(monkeypatch):
    _mock_http(monkeypatch, response=_FakeResponse(200, [1, 2, 3]))
    with pytest.raises(entitlements.EntitlementCheckUnavailable):
        await entitlements.validate_key("slk_test")


async def test_valid_response_missing_tenant_key_raises_unavailable(monkeypatch):
    # A License-Service contract violation, not an actually-invalid
    # caller — must not be treated as a hard denial.
    _mock_http(monkeypatch, response=_FakeResponse(200, {
        "valid": True, "sync_enabled": True, "server_time": "x",
    }))
    with pytest.raises(entitlements.EntitlementCheckUnavailable):
        await entitlements.validate_key("slk_test")


async def test_result_is_cached_within_ttl(monkeypatch):
    monkeypatch.setattr(settings, "ENTITLEMENT_CACHE_SECONDS", 300)
    calls: list = []
    _mock_http(monkeypatch, response=_FakeResponse(200, {
        "valid": True, "license_key_hash": "hash123", "sync_enabled": True, "server_time": "x",
    }), calls=calls)

    await entitlements.validate_key("slk_test")
    await entitlements.validate_key("slk_test")

    assert len(calls) == 1  # second call served from cache, no second HTTP request


async def test_expired_cache_triggers_a_fresh_check(monkeypatch):
    monkeypatch.setattr(settings, "ENTITLEMENT_CACHE_SECONDS", 0)
    calls: list = []
    _mock_http(monkeypatch, response=_FakeResponse(200, {
        "valid": True, "license_key_hash": "hash123", "sync_enabled": True, "server_time": "x",
    }), calls=calls)

    await entitlements.validate_key("slk_test")
    await entitlements.validate_key("slk_test")

    assert len(calls) == 2
