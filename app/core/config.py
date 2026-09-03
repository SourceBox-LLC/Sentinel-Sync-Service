import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    # Postgres, not SQLite — unlike every other service in this family
    # (Command Center, License-Service), this one is Postgres-native
    # from day one. It's the actual cloud mirror target for self-hosted
    # installs' data, and JSONB is what makes the generic synced_rows
    # schema (see app/models/models.py) workable without a per-model
    # migration kept in lockstep with Command Center's own schema.
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "postgresql+psycopg://sync:sync@localhost:5432/sentinel_sync"
    )

    # Sentinel-License-Service is the source of truth for whether a
    # given license key is allowed to push data here — this service
    # holds no license records of its own. See app/core/entitlements.py.
    LICENSE_SERVICE_URL: str = os.getenv(
        "LICENSE_SERVICE_URL", "https://sentinel-license.fly.dev"
    )
    # How long a validated key's entitlement is trusted before
    # re-checking with License-Service. Short enough that a revoked
    # license stops being able to push within minutes, long enough that
    # a self-hosted install's 30-minute sync loop (Command Center's
    # SENTINEL_SYNC_INTERVAL_SECONDS) doesn't call out to License-Service
    # on literally every batch of a large multi-batch push.
    ENTITLEMENT_CACHE_SECONDS: int = int(os.getenv("ENTITLEMENT_CACHE_SECONDS", "300"))

    SENTRY_DSN: str = os.getenv("SENTRY_DSN", "")
    SENTRY_TRACES_SAMPLE_RATE: float = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1"))

    REDIS_URL: str = os.getenv("REDIS_URL", "")

    # A single push batch is capped at this many rows — matches Command
    # Center's own sync_client.py _BATCH_SIZE so a well-behaved client
    # never trips this, while still bounding worst-case request size
    # from a misbehaving or malicious caller.
    MAX_ROWS_PER_PUSH: int = int(os.getenv("MAX_ROWS_PER_PUSH", "1000"))


settings = Config()
