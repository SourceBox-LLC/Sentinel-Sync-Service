"""Validates a caller's Bearer license key against the separate
Sentinel-License-Service before accepting a sync push — this service
holds no license records of its own (see Sentinel-Sync-Service/README.md
and the Sentinel Command plan doc for the full design).

Unlike Command Center's own license_client.py, there's no fail-open
grace window here: if License-Service is unreachable, a push is simply
rejected for this cycle. That's the right tradeoff in this direction —
Command Center's sync loop already treats a rejected/failed push as
"retry next cycle, cursor doesn't advance," so failing closed here loses
nothing but a few minutes of sync latency, whereas failing open would
mean accepting (and paying to store) data from a caller we couldn't
actually verify was entitled to push it.

A short in-memory TTL cache avoids calling out to License-Service on
every batch of a large multi-batch push, while still noticing a
revoked/disabled license within ENTITLEMENT_CACHE_SECONDS.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_VALIDATE_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class Entitlement:
    tenant_key: str  # License-Service's key_hash — the stable tenant id
    sync_enabled: bool


class EntitlementDenied(Exception):
    """Raised for any outcome other than 'valid and sync_enabled' —
    the API layer maps this to a 403, distinct from a 502 (which means
    "we genuinely couldn't check," not "we checked and the answer is no").
    """


class EntitlementCheckUnavailable(Exception):
    """License-Service was unreachable or answered unexpectedly — the
    API layer maps this to a 502, letting a well-behaved client (like
    Command Center's own sync_client.py) distinguish it from a hard
    403 and simply retry next cycle rather than treating it as a
    permanent rejection.
    """


# raw_key -> (Entitlement, cached_at_monotonic). A process-local cache is
# fine even with multiple workers/machines eventually — the cost of a
# cache miss duplicated across processes is just an extra, harmless
# License-Service call, not a correctness issue.
_cache: dict[str, tuple[Entitlement, float]] = {}


def _cache_key(raw_key: str) -> str:
    # Never hold the raw key itself as a long-lived dict key any longer
    # than necessary — hash it for the cache index. This is purely a
    # local cache key, not compared against anything License-Service
    # stores, so it doesn't need to match that service's own hash_key().
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def validate_key(raw_key: str) -> Entitlement:
    """Returns the caller's Entitlement, or raises EntitlementDenied /
    EntitlementCheckUnavailable."""
    key = _cache_key(raw_key)
    cached = _cache.get(key)
    if cached is not None:
        entitlement, cached_at = cached
        if time.monotonic() - cached_at < settings.ENTITLEMENT_CACHE_SECONDS:
            if not entitlement.sync_enabled:
                raise EntitlementDenied("sync not enabled (cached)")
            return entitlement

    try:
        async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{settings.LICENSE_SERVICE_URL.rstrip('/')}/v1/licenses/entitlements",
                headers={"Authorization": f"Bearer {raw_key}"},
            )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    except Exception as exc:
        logger.warning("[Entitlements] License-Service check failed", exc_info=True)
        raise EntitlementCheckUnavailable(str(exc)) from exc

    if not data.get("valid"):
        raise EntitlementDenied(str(data.get("reason") or "invalid"))

    tenant_key = data.get("license_key_hash")
    if not tenant_key:
        # A valid response must always carry the tenant id — treat a
        # missing one as unavailable (not denied), since this points at
        # a License-Service bug/contract drift, not an actually-invalid
        # caller.
        raise EntitlementCheckUnavailable("entitlements response missing license_key_hash")

    entitlement = Entitlement(tenant_key=tenant_key, sync_enabled=bool(data.get("sync_enabled")))
    _cache[key] = (entitlement, time.monotonic())

    if not entitlement.sync_enabled:
        raise EntitlementDenied("sync not enabled")
    return entitlement


def _reset_cache_for_tests() -> None:
    _cache.clear()
