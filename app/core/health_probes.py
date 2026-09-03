"""Dependency probes — trimmed from License-Service's own
health_probes.py to just the one probe that applies here: database.
No disk probe (unlike the SQLite services, this one's data lives in a
remote Postgres, not a local mounted volume — this container's own
ephemeral disk isn't where anything worth alerting on lives).

Same probe contract: a ProbeResult with status in ok/warn/critical,
never raises.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from app.core.database import SessionLocal

logger = logging.getLogger(__name__)


@dataclass
class ProbeResult:
    status: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, **self.data}

    @property
    def is_critical(self) -> bool:
        return self.status == "critical"


def probe_database() -> ProbeResult:
    """SELECT 1 round-trip. A failure here is the most pager-worthy
    signal in this service — every push reads/writes the DB."""
    try:
        db = SessionLocal()
        try:
            t0 = time.perf_counter()
            db.execute(text("SELECT 1"))
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            return ProbeResult(status="ok", data={"latency_ms": latency_ms})
        finally:
            db.close()
    except Exception as exc:
        logger.warning("[Health] DB ping failed", exc_info=True)
        return ProbeResult(status="critical", data={"error_class": type(exc).__name__})


@dataclass
class ReadinessReport:
    ready: bool
    probes: dict[str, ProbeResult]

    def to_dict(self) -> dict[str, Any]:
        return {"ready": self.ready, "checks": {name: p.to_dict() for name, p in self.probes.items()}}


def run_readiness_probes() -> ReadinessReport:
    probes = {"database": probe_database()}
    ready = not any(p.is_critical for p in probes.values())
    return ReadinessReport(ready=ready, probes=probes)
