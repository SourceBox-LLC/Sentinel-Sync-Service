"""Rate limiter — per source IP, copied from License-Service's own
limiter.py (identical rationale: a push caller is identified only by
the license key it presents, and bucketing by IP is what actually
blunts brute-forcing / abuse, not a tenant/org concept this service
doesn't have).
"""

import logging

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.core.config import settings

logger = logging.getLogger(__name__)


def get_client_ip(request: Request) -> str:
    """The real client IP, resistant to X-Forwarded-For spoofing.

    This service runs uvicorn with `--forwarded-allow-ips=*` (needed
    since Fly's edge is the only thing that can ever reach the
    machine's port), but that also makes uvicorn trust the *leftmost*
    entry of a client-supplied X-Forwarded-For header verbatim — a
    caller can set that header to anything and land in a different
    rate-limit bucket on every request, fully defeating the 20/minute
    cap this limiter exists to enforce. Fly's edge sets its own
    Fly-Client-IP header from the real TCP connection, which a client
    reaching the edge cannot override, so prefer that when present.
    """
    fly_client_ip = request.headers.get("Fly-Client-IP")
    if fly_client_ip:
        return fly_client_ip
    return get_remote_address(request)


def _build_limiter() -> Limiter:
    kwargs: dict = {"key_func": get_client_ip}
    if settings.REDIS_URL:
        kwargs["storage_uri"] = settings.REDIS_URL
        logger.info("[Limiter] Using Redis storage for rate limits")
    else:
        logger.warning(
            "[Limiter] REDIS_URL not set — in-memory rate limiting only. "
            "Fine for this service's single-machine deploy."
        )
    return Limiter(**kwargs)


limiter = _build_limiter()
