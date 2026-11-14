"""Database engine, session factory, and the reset-fence unit of work.

Driver: `psycopg` (v3), matching ARCHITECTURE.md §4's technology
table ("Mature wheels for psycopg ... Host has 3.14; the container is
authoritative"). Engine is synchronous — FastAPI runs sync path
operations in its threadpool automatically, which keeps the ORM
session model simple and avoids mixing async ORM sessions with the
sequential SAVEPOINT / advisory-lock / SELECT-FOR-UPDATE patterns later
milestones rely on. (Recorded as Implementation Clarification C8 —
ARCHITECTURE.md does not mandate sync vs. async SQLAlchemy.)

``mutating_transaction`` (M02) is the shared unit-of-work primitive
required by ARCHITECTURE.md §19.4 / invariant #29: "No reset-sensitive
mutation may perform authoritative database resolution or business
validation before acquiring the shared reset fence." It is the ONLY
sanctioned entry point for a reset-sensitive mutation, from M04 onward
— every mutating service wraps its work in it rather than acquiring
the fence by hand, so a single omission cannot silently reopen the F05
race IMPLEMENTATION_PLAN.md's M02 scope calls out explicitly.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import Depends
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.errors import MaintenanceError
from app.core.locks import FACEMARK_RESET_FENCE


def _build_engine() -> Engine:
    settings = get_settings()
    # connect_timeout: an unreachable database host must fail /api/health/ready in
    # seconds instead of hanging until a proxy gives up.
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        future=True,
        # Comfortably above the admission limit (app/core/admission.py, 12), so
        # admitted requests, live-stream session checks and photo uploads never
        # wait on each other for a connection. PostgreSQL's default
        # max_connections (100) leaves room for the worker and one-off jobs.
        pool_size=10,
        max_overflow=10,
        connect_args={"connect_timeout": 10},
    )


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


DbSession = Annotated[Session, Depends(get_db)]


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


# PostgreSQL's SET LOCAL does not accept bind parameters — it requires
# a literal in the statement text (this is a PostgreSQL/libpq protocol
# limitation, not a SQLAlchemy one). `int(...)` here is the safety
# boundary: it raises on anything that is not actually an integer
# before the value ever reaches string formatting, so this is not the
# "string-formatted SQL containing request data" ARCHITECTURE.md
# §21.10 prohibits — no request data reaches this call in the first
# place; `lock_timeout_ms` is always an internal, caller-supplied int.
DEFAULT_FENCE_LOCK_TIMEOUT_MS = 2_000

# Set on a session by `maintenance_fast_path_check` (every HTTP mutation calls it
# first): its fenced transactions then re-read the maintenance flag after the
# fence. CLI and background jobs never set it and keep working in maintenance.
_MAINTENANCE_GATED = "facemark.maintenance_gated"


def gate_on_maintenance(db: Session) -> None:
    """Make every later `mutating_transaction` on `db` refuse with MAINTENANCE
    while `event_config.maintenance_mode` is on - read fresh, inside the fence.

    The flag is persistent state: a failed reset leaves it ON after releasing
    the fence (ARCHITECTURE.md §19.7), so while no fence is held the flag is the
    only thing that keeps mutations out. It must not depend on an in-process
    cache, a notification or a TTL; the reset may run in another process and fail
    before it can notify anyone."""
    db.info[_MAINTENANCE_GATED] = True


@contextmanager
def mutating_transaction(
    db: Session, *, lock_timeout_ms: int = DEFAULT_FENCE_LOCK_TIMEOUT_MS
) -> Iterator[Session]:
    """The sanctioned entry point for every reset-sensitive mutation.

    Acquires the shared reset fence — ``pg_advisory_xact_lock_shared``
    — as literally the first statement executed against ``db``, before
    any reset-sensitive read or write. ARCHITECTURE.md §19.4:

        "Every transaction that mutates reset-sensitive application
        data must acquire the shared reset fence before its first
        reset-sensitive database read, and hold it continuously
        through commit or rollback."

    Usage::

        with mutating_transaction(db) as tx:
            # every reset-sensitive read and write goes through `tx`
            ...
        # committed on success, rolled back on any exception

    ``db`` is an existing session (e.g. FastAPI's request-scoped
    ``get_db`` dependency) — this function does not open or close a
    session of its own, only wraps the fence acquisition and the
    commit/rollback boundary around one already obtained. Only work
    that is provably reset-independent (request parsing, schema
    validation, cookie extraction, cryptographic verification against
    a configured secret, hashing — ARCHITECTURE.md §19.4) may happen
    before this is called; nothing that reads or depends on
    application data may.

    The transaction-scoped variant is deliberate: it releases
    automatically on commit OR rollback, so a crashed request, a
    timeout, or an unhandled exception can never leak the fence and
    stall a future reset (contrast with the reset side's own
    session-scoped exclusive lock on a dedicated connection, which is
    M15 scope).

    ``lock_timeout_ms`` bounds how long this call will wait if a demo
    reset currently holds the exclusive side of the fence. Exceeding
    it raises ``OperationalError`` (PostgreSQL error 55P03,
    ``lock_not_available``) — the caller is expected to map that to
    the documented ``MAINTENANCE`` outcome (ARCHITECTURE.md §20.3);
    that mapping is feature-layer work, not M02 scope.
    """
    timeout_ms = int(lock_timeout_ms)
    db.execute(text(f"SET LOCAL lock_timeout = '{timeout_ms}ms'"))
    db.execute(
        text("SELECT pg_advisory_xact_lock_shared(:key)"),
        {"key": FACEMARK_RESET_FENCE},
    )
    try:
        # After the fence (no reset is between steps 3 and 7 now) and before any
        # authentication or business read: a reset that failed, or maintenance
        # switched on by hand, keeps this mutation out - whatever a cache says.
        if (
            db.info.get(_MAINTENANCE_GATED)
            and db.execute(
                text("SELECT maintenance_mode FROM event_config WHERE id = 1")
            ).scalar_one_or_none()
        ):
            raise MaintenanceError(
                "The service is briefly unavailable. Please try again in a moment."
            )
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
