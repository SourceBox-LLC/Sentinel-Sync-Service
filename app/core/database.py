"""Postgres/SQLAlchemy setup. Unlike every other service in this family
(Command Center, License-Service — both SQLite), this one talks to a
real Postgres instance, so the SQLite-specific pool/pragma dance those
two need doesn't apply here: plain QueuePool (SQLAlchemy's default) is
correct, and pool_pre_ping guards against a cloud Postgres provider
silently closing idle connections between this service's requests.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import settings

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
