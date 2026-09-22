"""Database engine and session factory.

M01 scope only: a plain synchronous SQLAlchemy engine and session
factory, used here solely to prove connectivity for /api/health.

The reset-fence-acquiring unit-of-work helper required by every later
mutating transaction (ARCHITECTURE.md §19.4, invariant #29) is M02
scope — it has nothing to guard yet, since no mutable table exists
until M02's migrations land. Introducing it now would be exactly the
"business-domain code merely to anticipate later milestones" this
phase is instructed not to add.

Driver: `psycopg` (v3), matching ARCHITECTURE.md §4's technology
table ("Mature wheels for psycopg ... Host has 3.14; the container is
authoritative"). Engine is synchronous — FastAPI runs sync path
operations in its threadpool automatically, which keeps the ORM
session model simple and avoids mixing async ORM sessions with the
sequential SAVEPOINT / advisory-lock / SELECT-FOR-UPDATE patterns later
milestones rely on. (Recorded as Implementation Clarification C8 —
ARCHITECTURE.md does not mandate sync vs. async SQLAlchemy.)
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def _build_engine() -> Engine:
    settings = get_settings()
    return create_engine(settings.database_url, pool_pre_ping=True, future=True)


# Created once per process at import time. Settings are already
# validated by this point (get_settings() would have raised on a
# missing required variable), so a failure here is a genuine
# connectivity problem, not a configuration problem.
engine: Engine = _build_engine()

SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, future=True
)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def database_is_reachable() -> bool:
    """Used by /api/health. A plain ``SELECT 1`` — cheap, and enough
    to prove the configured credentials and network path both work.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — health check must never raise
        return False
