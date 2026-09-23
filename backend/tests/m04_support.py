"""Shared helpers for the M04 registration/activation test suites.

These tests exercise real, committing endpoints (every M04 mutation goes
through `mutating_transaction`, which commits), so each test owns a
unique email and removes every row it created afterwards — the same
explicit-cleanup approach M02's `committed_interest_id` fixture uses,
rather than the rollback-only `db` fixture, which cannot observe a
commit made by the code under test.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import text

from app.db.session import SessionLocal

OTP_LOGGER = "app.modules.auth.otp_delivery"


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
