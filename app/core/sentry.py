"""Sentry error tracking — copied verbatim from Sentinel Command Center's
backend/app/core/sentry.py. Already parameterized by dsn/environment/
release/traces_sample_rate with env-var fallbacks; nothing here is
Command-Center-specific. This service's own header-scrub list is
trimmed since it has no auth-header/agent-key vocabulary of its own —
just Authorization, which is already covered by send_default_pii=False
plus the explicit scrub below.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_initialized = False


def init_sentry(
    dsn: Optional[str] = None,
    environment: Optional[str] = None,
    release: Optional[str] = None,
    traces_sample_rate: float = 0.1,
) -> bool:
    global _initialized
    if _initialized:
        return False

    resolved_dsn = dsn or os.getenv("SENTRY_DSN", "").strip()
    if not resolved_dsn:
        logger.info("[Sentry] SENTRY_DSN not set — error tracking disabled")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except ImportError:
        logger.exception("[Sentry] sentry-sdk import failed — skipping init")
        return False

    resolved_env = environment or os.getenv("SENTRY_ENVIRONMENT") or (
        "production" if os.getenv("FLY_APP_NAME") else "development"
    )
    resolved_release = release or os.getenv("SENTRY_RELEASE") or os.getenv("FLY_MACHINE_VERSION")

    try:
        sentry_sdk.init(
            dsn=resolved_dsn,
            environment=resolved_env,
            release=resolved_release,
            send_default_pii=False,
            traces_sample_rate=traces_sample_rate,
            profiles_sample_rate=0.0,
            integrations=[
                StarletteIntegration(transaction_style="endpoint"),
                FastApiIntegration(transaction_style="endpoint"),
                SqlalchemyIntegration(),
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ],
            before_send=_scrub_event,
        )
    except Exception:
        logger.exception("[Sentry] init failed — error tracking disabled")
        return False

    _initialized = True
    logger.info(
        "[Sentry] initialized — env=%s release=%s trace_rate=%.2f",
        resolved_env, resolved_release or "(unset)", traces_sample_rate,
    )
    return True


def _scrub_event(event: dict, hint: dict) -> Optional[dict]:
    """Strip query strings and redact the Authorization header — a raw
    license key could otherwise ride into Sentry via a captured request
    URL or header dump."""
    request = event.get("request")
    if isinstance(request, dict):
        if "query_string" in request:
            request["query_string"] = ""
        url = request.get("url")
        if isinstance(url, str) and "?" in url:
            request["url"] = url.split("?", 1)[0]

        headers = request.get("headers")
        if isinstance(headers, dict):
            for name in list(headers.keys()):
                if name.lower() in {"authorization", "cookie"}:
                    headers[name] = "[redacted]"

    return event


def capture_exception(exc: BaseException, **tags: Any) -> None:
    if not _initialized:
        return
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            for key, value in tags.items():
                scope.set_tag(key, value)
            sentry_sdk.capture_exception(exc)
    except Exception:
        logger.exception("[Sentry] capture_exception failed")


def is_initialized() -> bool:
    return _initialized


def _reset_for_tests() -> None:
    global _initialized
    _initialized = False
