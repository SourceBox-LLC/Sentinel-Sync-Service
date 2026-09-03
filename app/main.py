import logging
import time
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api import sync
from app.core.config import settings
from app.core.health_probes import run_readiness_probes
from app.core.limiter import limiter
from app.core.sentry import init_sentry

# Import models so they're registered on Base.metadata for Alembic's
# autogenerate / for tests that create tables directly. Unlike Command
# Center and License-Service, schema convergence here is NOT done at
# app boot (no ensure_schema() call) — this is the one service in the
# family using real migrations (see alembic/), applied via `alembic
# upgrade head` as this service's Fly release_command, not inline in
# the request-serving process.
from app.models import models  # noqa: F401

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

init_sentry(
    dsn=settings.SENTRY_DSN or None,
    traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
)

_STARTED_AT_MONO = time.monotonic()

app = FastAPI(
    title="Sentinel Sync Service",
    description="Cloud data-sync mirror for self-hosted Sentinel Command Center installs.",
    version="0.1.0",
    docs_url="/api-docs",
    redoc_url="/api-redoc",
    openapi_url="/api/openapi.json",
)

app.state.limiter = limiter


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    limit_str = str(exc.detail) if getattr(exc, "detail", None) else "rate limit exceeded"
    body = {
        "error": "rate_limit_exceeded",
        "message": "Too many requests. Back off and retry after the Retry-After window.",
        "limit": limit_str,
        "retry_after_seconds": 60,
    }
    return JSONResponse(status_code=429, content=body, headers={"Retry-After": "60"})


app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.include_router(sync.router)


@app.get("/health")
async def health():
    """Pure liveness — must never be slow. This is what Fly's health
    check polls; a slow dependency shouldn't pull the only machine out
    of rotation."""
    return {"status": "healthy", "version": "0.1.0"}


@app.get("/health/ready")
def health_ready():
    """Readiness — 503 if a critical probe fails, 200 otherwise.

    Plain `def`, not `async def` — run_readiness_probes() does blocking
    SQLAlchemy I/O; an async def running it inline would stall the sole
    event-loop thread for the probe's duration.
    """
    report = run_readiness_probes()
    status_code = 200 if report.ready else 503
    body = {
        **report.to_dict(),
        "version": "0.1.0",
        "uptime_seconds": round(time.monotonic() - _STARTED_AT_MONO, 1),
    }
    return JSONResponse(status_code=status_code, content=body)


@app.get("/")
async def root():
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    return {"service": "sentinel-sync-service", "time": now.isoformat() + "Z"}
