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


_APP_TABLE_LOCKS_WHILE_WAITING_ON_FENCE = """
SELECT count(*) FROM pg_locks rel
JOIN pg_locks adv ON adv.pid = rel.pid AND adv.locktype = 'advisory' AND NOT adv.granted
WHERE rel.locktype = 'relation' AND rel.granted
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
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
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
