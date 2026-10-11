"""Shared helpers for the OTP outbox tests.

Nothing here talks to a real email provider: `FakeProvider` is the test
adapter for the worker's `EmailProvider` seam, and `no_real_email`
(conftest) additionally makes any stray `httpx.post` fail the test.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.modules.attendees.models import OtpChallenge, OtpPurpose
from app.modules.auth import otp_outbox as ob
from app.modules.auth.otp_crypto import decrypt_code
from app.modules.auth.otp_provider import EmailMessage, SendResult

# Test-only keys: arbitrary bytes, URL-safe base64. Not used anywhere real.
TEST_KEY = "AwoRGB8mLTQ7QklQV15lbHN6gYiPlp2kq7K5wMfO1dw="
OTHER_KEY = "BRIfLDlGU2BteoeUoa67yNXi7_wJFiMwPUpXZHF-i5g="
FAKE_API_KEY = "re_test_not_a_real_key_0000"
EMAIL_DOMAIN = "outbox.facemark-tests.net"


def outbox_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "resend_api_key": FAKE_API_KEY,
        "otp_outbox_encryption_key": TEST_KEY,
        "otp_from_email": "FACEMARK <info@facemark-tests.net>",
    }
    values.update(overrides)
    return get_settings().model_copy(update=values)


def install_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Settings:
    for target in (
        "app.modules.attendees.service.get_settings",
        "app.modules.auth.otp_delivery.get_settings",
        "app.modules.auth.otp_outbox.get_settings",
    ):
        monkeypatch.setattr(target, lambda: settings)
    return settings


# --- the provider test adapter -----------------------------------------------


def accepted(message_id: str = "msg_test_0001") -> SendResult:
    return SendResult(True, provider_message_id=message_id, http_status=200)


def retryable(reason: str = "provider_unavailable", status: int = 503) -> SendResult:
    return SendResult(False, reason_code=reason, retryable=True, http_status=status)


def permanent(reason: str = "provider_rejected_message", status: int = 422) -> SendResult:
    return SendResult(False, reason_code=reason, retryable=False, http_status=status)


class FakeProvider:
    """Records every message it is asked to send and answers from a script
    (the last answer repeats). Never touches the network."""

    def __init__(self, *script: SendResult) -> None:
        self._script = list(script) or [accepted()]
        self.messages: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> SendResult:
        self.messages.append(message)
        index = min(len(self.messages) - 1, len(self._script) - 1)
        return self._script[index]

    def code_in(self, index: int = -1) -> str:
        match = re.search(r"\b(\d{6})\b", self.messages[index].text)
        assert match is not None
        return match.group(1)


class ExplodingProvider:
    def __init__(self, message: str) -> None:
        self._message = message

    def send(self, message: EmailMessage) -> SendResult:
        raise RuntimeError(self._message)


# --- direct database helpers (committed state, like any other process) -------


def new_address() -> str:
    return f"ob-{uuid.uuid4().hex[:12]}@{EMAIL_DOMAIN}"


def make_delivery(
    *,
    email: str | None = None,
    code: str = "123456",
    language: str = "en",
    purpose: OtpPurpose = OtpPurpose.LOGIN,
    ttl: timedelta = timedelta(minutes=10),
    settings: Settings | None = None,
) -> tuple[int, int, str]:
    """A committed challenge + outbox row, exactly as `issue_otp` makes
    them. Returns (challenge_id, delivery_id, email)."""
    address = email or new_address()
    settings = settings or outbox_settings()
    db = SessionLocal()
    try:
        challenge = OtpChallenge(
            email=address,
            user_id=None,
            purpose=purpose,
            code_hash="$argon2id$test-only-not-verified",
            expires_at=datetime.now(UTC) + ttl,
        )
        db.add(challenge)
        db.flush()
        row = ob.enqueue(db, challenge=challenge, code=code, language=language, settings=settings)
        ids = (challenge.id, row.id, address)
        db.commit()
        return ids
    finally:
        db.close()


def row(delivery_id: int) -> Any:
    db = SessionLocal()
    try:
        return (
            db.execute(text("SELECT * FROM otp_deliveries WHERE id = :i"), {"i": delivery_id})
            .mappings()
            .one()
        )
    finally:
        db.close()


def execute(sql: str, **params: Any) -> None:
    db = SessionLocal()
    try:
        db.execute(text(sql), params)
        db.commit()
    finally:
        db.close()


def make_due(delivery_id: int) -> None:
    execute(
        "UPDATE otp_deliveries SET next_attempt_at = now() - interval '1 second' WHERE id = :i",
        i=delivery_id,
    )


def expire_lease(delivery_id: int) -> None:
    execute(
        "UPDATE otp_deliveries SET locked_until = now() - interval '1 second' WHERE id = :i",
        i=delivery_id,
    )


def peek_code(delivery_id: int, settings: Settings | None = None) -> str:
    """What the worker itself does: decrypt the stored payload. For tests
    that need to know the code of a delivery created through the API."""
    r = row(delivery_id)
    return decrypt_code(
        bytes(r["payload_ciphertext"]),
        r["payload_key_id"],
        idempotency_key=r["idempotency_key"],
        email=r["email"],
        purpose=r["purpose"],
        settings=settings or outbox_settings(),
    )


def deliveries_for(email: str) -> list[Any]:
    db = SessionLocal()
    try:
        return list(
            db.execute(
                text("SELECT * FROM otp_deliveries WHERE email = :e ORDER BY id"), {"e": email}
            )
            .mappings()
            .all()
        )
    finally:
        db.close()


def count(table: str, where: str = "true", **params: Any) -> int:
    db = SessionLocal()
    try:
        return int(
            db.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params).scalar_one()
        )
    finally:
        db.close()


def clean_outbox() -> None:
    execute(f"DELETE FROM otp_challenges WHERE email LIKE '%@{EMAIL_DOMAIN}'")
    execute("DELETE FROM otp_deliveries")
