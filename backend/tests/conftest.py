"""Shared pytest fixtures for the backend test suite."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.ratelimit import limiter
from app.db import all_models  # noqa: F401 — registers every model with Base.metadata
from app.db.session import SessionLocal
from tests.m04_support import OTP_LOGGER, cleanup_email, make_client, new_email


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
