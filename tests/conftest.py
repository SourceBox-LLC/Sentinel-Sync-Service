"""Shared test fixtures. Unlike License-Service's in-memory-SQLite
conftest, this needs a real Postgres (JSONB isn't SQLite-portable) —
point TEST_DATABASE_URL at a throwaway instance (see README.md's
"Running tests" section for the one-line docker command) before
running the suite.
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Must set env vars BEFORE importing app modules so config.py picks them up.
_TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://sync:sync@localhost:55432/sentinel_sync_test"
)
os.environ["DATABASE_URL"] = _TEST_DB_URL

from app.core.database import Base, get_db  # noqa: E402
from app.core.entitlements import _reset_cache_for_tests  # noqa: E402
from app.core.limiter import limiter  # noqa: E402
from app.main import app  # noqa: E402
from app.models import models  # noqa: E402, F401

engine = create_engine(_TEST_DB_URL)
TestSession = sessionmaker(bind=engine)

Base.metadata.create_all(bind=engine)


def _override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _override_get_db


@pytest.fixture(autouse=True)
def _clean_state():
    limiter.reset()
    _reset_cache_for_tests()
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db_session():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()
