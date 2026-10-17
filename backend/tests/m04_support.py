"""Shared helpers for the registration/authentication test suites
(M04, M05).

These tests exercise real, committing endpoints (every mutation goes
through `mutating_transaction`, which commits), so each test owns a
unique email and removes every row it created afterwards — the same
explicit-cleanup approach M02's `committed_interest_id` fixture uses,
rather than the rollback-only `db` fixture, which cannot observe a
commit made by the code under test.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import psycopg
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text

from app.core.config import get_settings
from app.core.locks import FACEMARK_RESET_FENCE
from app.db.session import SessionLocal

OTP_LOGGER = "app.modules.auth.otp_delivery"
PSYCOPG_URL = get_settings().database_url.replace("postgresql+psycopg://", "postgresql://")


def make_client(**kwargs: Any) -> TestClient:
    """A TestClient that behaves like the same-origin browser: it sends
    the configured APP_ORIGIN as `Origin` (§8.3.1). Pass `headers=` to
    override for CSRF tests."""
    from app.main import app

    headers = {"Origin": get_settings().app_origin, **kwargs.pop("headers", {})}
    return TestClient(app, headers=headers, **kwargs)


# `pg_locks` is CLUSTER-wide: it also lists the locks of every other database on
# the server, including a concurrent test run's own. Advisory locks belong to a
# database, so a test inspecting "the fence" must look at this database only.
ADVISORY_LOCKS_HERE = (
    "locktype = 'advisory' "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
)


_APP_TABLE_LOCKS_WHILE_WAITING_ON_FENCE = """
SELECT count(*) FROM pg_locks rel
JOIN pg_locks adv ON adv.pid = rel.pid AND adv.locktype = 'advisory' AND NOT adv.granted
  AND adv.database = rel.database
WHERE rel.locktype = 'relation' AND rel.granted
  AND rel.database = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND rel.relation IN (
      'users'::regclass, 'otp_challenges'::regclass, 'interests'::regclass,
      'user_interests'::regclass, 'contact_cards'::regclass, 'consent_audit'::regclass,
      'sessions'::regclass, 'staff_accounts'::regclass, 'points_log'::regclass,
      'idempotency_records'::regclass, 'booths'::regclass, 'task_preset_points'::regclass,
      'event_config'::regclass, 'feedback'::regclass
  )
"""


def assert_fenced_before_any_read(call: Callable[[], Response]) -> Response:
    """Holds the EXCLUSIVE side of the reset fence (as demo reset will,
    M15), fires the request, and — while it is blocked — inspects
    pg_locks: the blocked backend must hold no lock on ANY application
    table. Merely blocking would prove the fence exists; holding zero
    relation locks while blocked proves it comes BEFORE the first
    reset-sensitive read, which is exactly invariant #29. The lock is
    then released and the request must complete normally."""
    holder = psycopg.connect(PSYCOPG_URL)
    holder.autocommit = True
    with holder.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (FACEMARK_RESET_FENCE,))

    result: dict[str, Response] = {}
    worker = threading.Thread(target=lambda: result.setdefault("r", call()))
    try:
        worker.start()
        with psycopg.connect(PSYCOPG_URL, autocommit=True) as probe, probe.cursor() as cur:
            deadline = time.monotonic() + 1.5
            waiting = 0
            while time.monotonic() < deadline and not waiting:
                cur.execute(
                    f"SELECT count(*) FROM pg_locks WHERE {ADVISORY_LOCKS_HERE} AND NOT granted"
                )
                row = cur.fetchone()
                waiting = row[0] if row else 0
                time.sleep(0.02)
            assert waiting >= 1, "request never reached the fence"
            cur.execute(_APP_TABLE_LOCKS_WHILE_WAITING_ON_FENCE)
            row = cur.fetchone()
            assert row is not None and row[0] == 0, (
                "a reset-sensitive table was read before the fence was acquired"
            )
    finally:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (FACEMARK_RESET_FENCE,))
        holder.close()
        worker.join(timeout=15)
    return result["r"]


def new_email(tag: str = "m04") -> str:
    # .demo, not a special-use TLD like .test/.example, which
    # email-validator rejects outright.
    return f"{tag}-{uuid.uuid4().hex[:12]}@test.facemark.demo"


def register_payload(email: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "first_name": "Grace",
        "last_name": "Hopper",
        "email": email,
        "phone": "+1-555-0142",
        "profession": "Systems Engineer",
        "linkedin_url": "https://www.linkedin.com/in/grace-hopper-demo",
        "interest_slugs": ["generative-ai", "data-analytics"],
        "sponsor_consent": False,
    }
    payload.update(overrides)
    return payload


def captured_otp(records: list[logging.LogRecord], email: str, purpose: str = "ACTIVATION") -> str:
    """The plaintext code exactly as an operator would obtain it in
    DEMO_MODE: from the delivery adapter's log record — never from the
    database, which only ever holds its Argon2id hash."""
    for record in reversed(records):
        args = record.args
        if (
            record.name == OTP_LOGGER
            and isinstance(args, tuple)
            and len(args) == 3
            and args[0] == email.lower()
            and args[1] == purpose
        ):
            return str(args[2])
    raise AssertionError(f"no {purpose} OTP was delivered for {email}")


def cleanup_email(email: str) -> None:
    db = SessionLocal()
    try:
        params = {"e": email.lower()}
        user_ids = [
            row[0]
            for row in db.execute(text("SELECT id FROM users WHERE email = :e"), params).all()
        ]
        db.execute(text("DELETE FROM otp_challenges WHERE email = :e"), params)
        if user_ids:
            ids = {"ids": user_ids}
            db.execute(text("DELETE FROM sessions WHERE user_id = ANY(:ids)"), ids)
            db.execute(text("DELETE FROM consent_audit WHERE user_id = ANY(:ids)"), ids)
            db.execute(text("DELETE FROM contact_cards WHERE user_id = ANY(:ids)"), ids)
            db.execute(text("DELETE FROM user_interests WHERE user_id = ANY(:ids)"), ids)
            # People (migration 0021): a test's connections and one-way scans.
            db.execute(
                text(
                    "DELETE FROM connections WHERE user_low_id = ANY(:ids) "
                    "OR user_high_id = ANY(:ids)"
                ),
                ids,
            )
            db.execute(
                text(
                    "DELETE FROM connection_scans WHERE scanner_id = ANY(:ids) "
                    "OR scanned_id = ANY(:ids)"
                ),
                ids,
            )
            db.execute(text("DELETE FROM users WHERE id = ANY(:ids)"), ids)
        db.commit()
    finally:
        db.close()


def fetch_one(sql: str, **params: Any) -> Any:
    """Reads committed state through a fresh session — what any other
    request would see, not the endpoint's own uncommitted view."""
    db = SessionLocal()
    try:
        return db.execute(text(sql), params).first()
    finally:
        db.close()


def fetch_all(sql: str, **params: Any) -> list[Any]:
    db = SessionLocal()
    try:
        return list(db.execute(text(sql), params).all())
    finally:
        db.close()


def execute(sql: str, **params: Any) -> None:
    db = SessionLocal()
    try:
        db.execute(text(sql), params)
        db.commit()
    finally:
        db.close()


def rule_revision_in_force(task_id: int) -> str:
    """The `rule_revision` the server would hand a staff screen for this
    activity right now - what a scanner that had just listed the activities
    would later send back as "I confirmed this rule". Reads committed state
    and resolves the repeat policy the same documented way (activity ->
    booth -> event default)."""
    from app.modules.points.service import compute_rule_revision, resolve_repeat_policy

    row = fetch_one(
        "SELECT t.points, t.allow_repeat_points, b.allow_repeat_task_points, "
        "e.allow_repeat_task_points_default "
        "FROM tasks t JOIN booths b ON b.id = t.booth_id, event_config e "
        "WHERE t.id = :t AND e.id = 1",
        t=task_id,
    )
    if row is None or row[0] is None:
        # unknown or unpriced activity: the server refuses before it compares
        return "no-rule-in-force"
    repeatable = resolve_repeat_policy(task=row[1], booth=row[2], event_default=row[3])
    return compute_rule_revision(task_id=task_id, points=int(row[0]), repeatable=repeatable)
