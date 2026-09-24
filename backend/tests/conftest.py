"""Shared pytest fixtures for the backend test suite."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.ratelimit import limiter
from app.db import all_models  # noqa: F401 — registers every model with Base.metadata
from app.db.session import SessionLocal
from tests.m04_support import OTP_LOGGER, cleanup_email, make_client, new_email
from tests.reset_support import (
    ALL_RESET_RELEVANT_TABLES,
    SEED_ADMIN_PASSWORD,
    SEED_STAFF_PASSWORD,
    create_isolated_database,
    drop_isolated_database,
    isolated_url,
    migrate_isolated_database,
)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Behaves like the real same-origin frontend: every request carries
    the configured `Origin`, as a browser on APP_ORIGIN does (§8.3.1).
    CSRF tests that need a foreign or missing origin override it."""
    with make_client() as c:
        yield c


@pytest.fixture(autouse=True)
def isolated_rate_limiter() -> Iterator[None]:
    """The limiter is in-process state (§21.7's demo adapter): reset it
    around every test, and freeze its clock so no window can roll over
    mid-test and make a limit assertion flaky. Tests that need time to
    move set `limiter.clock` themselves."""
    real_clock = limiter.clock
    limiter.reset()
    limiter.clock = lambda: 1_800_000_000.0
    yield
    limiter.reset()
    limiter.clock = real_clock


@pytest.fixture
def unique_email() -> Iterator[str]:
    """A fresh address per test; every row it produced is deleted
    afterwards, since M04 endpoints genuinely commit."""
    email = new_email()
    yield email
    cleanup_email(email)


@pytest.fixture
def otp_log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Captures the DEMO_MODE delivery adapter's log line — the only
    place a plaintext OTP ever exists outside the request that made it.
    DEBUG: the line carries a full email, which §21.11 keeps out of INFO."""
    caplog.set_level(logging.DEBUG, logger=OTP_LOGGER)
    return caplog


@pytest.fixture
def db() -> Iterator[Session]:
    """A real PostgreSQL session for a single test, inside ONE
    transaction that is ALWAYS rolled back at teardown — never
    committed. ARCHITECTURE.md / IMPLEMENTATION_PLAN.md M02:
    "PostgreSQL-specific behaviour must be tested on PostgreSQL" — this
    is deliberately not SQLite, and runs against the same `db` Compose
    service the running application uses.

    Setup rows inserted via this fixture (see the `seeded_*` fixtures
    below) are never committed either — they live only within this
    transaction, which is exactly enough: everything a negative
    constraint test needs (the setup row and the violating attempt) is
    read-your-own-writes within one open transaction. Nothing here is
    visible to a second connection — tests that genuinely need that
    (the advisory-lock concurrency tests) use `raw_conn` instead, which
    commits deliberately and cleans up explicitly.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def seeded_interest_id(db: Session) -> int:
    row = db.execute(
        text(
            "INSERT INTO interests (slug, name) VALUES "
            "('ai', 'Artificial Intelligence') RETURNING id"
        )
    ).one()
    return int(row[0])


@pytest.fixture
def seeded_booth_id(db: Session) -> int:
    """A throwaway booth for constraint tests. Deliberately NOT
    'aiworks': from M03 onward that slug is the seed's permanent demo
    booth (app/seed/demo_fixtures.py), always present in a migrated +
    seeded database, so a test-local row here must use a slug of its
    own to avoid colliding with real, committed data."""
    row = db.execute(
        text(
            "INSERT INTO booths (slug, name) VALUES "
            "('m02-test-booth', 'M02 Test Booth') RETURNING id"
        )
    ).one()
    return int(row[0])


@pytest.fixture
def seeded_second_booth_id(db: Session) -> int:
    row = db.execute(
        text(
            "INSERT INTO booths (slug, name) VALUES "
            "('m02-test-booth-2', 'M02 Test Booth 2') RETURNING id"
        )
    ).one()
    return int(row[0])


@pytest.fixture
def seeded_user_id(db: Session) -> int:
    row = db.execute(
        text(
            "INSERT INTO users (first_name, last_name, email, phone, profession, "
            "sponsor_consent, status, activated_at) VALUES "
            "('Ada', 'Lovelace', 'ada@example.com', '+10000000000', 'Engineer', "
            "false, 'ACTIVE', now()) RETURNING id"
        )
    ).one()
    return int(row[0])


@pytest.fixture
def seeded_staff_id(db: Session, seeded_booth_id: int) -> int:
    row = db.execute(
        text(
            "INSERT INTO staff_accounts (email, password_hash, full_name, role, booth_id) "
            "VALUES ('staff@example.com', 'x', 'Staff One', 'BOOTH_STAFF', :booth_id) RETURNING id"
        ),
        {"booth_id": seeded_booth_id},
    ).one()
    return int(row[0])


@pytest.fixture
def seeded_task_id(db: Session, seeded_booth_id: int) -> int:
    row = db.execute(
        text("INSERT INTO tasks (booth_id, title) VALUES (:booth_id, 'Try the demo') RETURNING id"),
        {"booth_id": seeded_booth_id},
    ).one()
    return int(row[0])


# --- M15: the isolated destructive-reset test database ---------------------
# See tests/reset_support.py's module docstring for why this is a
# SEPARATE database, never the one the fixtures above operate on.


@pytest.fixture(scope="session")
def reset_db_url() -> Iterator[str]:
    """A freshly created, freshly migrated, EMPTY isolated database —
    created once for the whole test session and dropped at the end.
    Individual tests use `clean_reset_db` (function-scoped) to get back
    to a known, freshly-seeded baseline rather than re-creating this."""
    create_isolated_database()
    url = isolated_url()
    migrate_isolated_database(url)
    try:
        yield url
    finally:
        drop_isolated_database()


@pytest.fixture(scope="session")
def reset_db_engine(reset_db_url: str) -> Iterator[Engine]:
    """A dedicated, NullPool engine bound to the isolated database —
    never `app.db.session.engine`, which stays bound to the real
    database for the lifetime of this test process."""
    engine = create_engine(reset_db_url, poolclass=NullPool, future=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def clean_reset_db(reset_db_url: str, reset_db_engine: Engine) -> Iterator[str]:
    """Function-scoped: truncates every reset-relevant table (and
    restores `event_config`'s singleton row to its fixture defaults
    directly — it is never truncated) and re-seeds via the real
    `seed_all()`, so every M15 test starts from an identical, freshly-
    seeded, generation-1 baseline independent of what the previous test
    did to this same long-lived isolated database. Yields the URL."""
    from app.seed.seeder import seed_all

    with reset_db_engine.connect() as conn:
        tables = ", ".join(ALL_RESET_RELEVANT_TABLES)
        conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
        conn.execute(
            text(
                "INSERT INTO event_config "
                "(id, feedback_points, allow_repeat_task_points_default, "
                "leaderboard_top_n, dataset_generation, maintenance_mode) "
                "VALUES (1, 5, false, 10, 1, false) "
                "ON CONFLICT (id) DO UPDATE SET "
                "feedback_points = 5, allow_repeat_task_points_default = false, "
                "leaderboard_top_n = 10, dataset_generation = 1, maintenance_mode = false, "
                "updated_by_staff_id = NULL"
            )
        )
        conn.commit()

    seed_session = Session(bind=reset_db_engine)
    try:
        seed_all(
            seed_session, admin_password=SEED_ADMIN_PASSWORD, staff_password=SEED_STAFF_PASSWORD
        )
    finally:
        seed_session.close()

    yield reset_db_url
