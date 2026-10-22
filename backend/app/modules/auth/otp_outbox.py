# ruff: noqa: E501  (the operator-facing message table below is prose)
"""The durable OTP delivery outbox.

Flow
----
1. `issue_otp` creates the OTP challenge and calls `enqueue` IN THE SAME
   TRANSACTION. Roll the registration/login back and the outbox row goes
   with it - no email for something that never happened. Commit it and the
   email can no longer be lost, whatever happens to the web process.
2. A separate worker (`python -m app.worker`) claims due rows, sends them
   through an `EmailProvider`, and records the outcome.

States (`otp_deliveries.status`)
--------------------------------
pending     waiting for its (next) attempt
processing  claimed by a worker; the claim is a lease that expires
accepted    the PROVIDER accepted the message (HTTP 2xx). Not "delivered":
            inbox placement, bounces and spam filtering are not observable
failed      permanently undeliverable (provider rejected it, or attempts ran out)
expired     never sent, and must not be: the code expired, or it was used or
            replaced by a newer code before its turn came

Guarantees
----------
* Claiming is one atomic `UPDATE ... FOR UPDATE SKIP LOCKED`: two workers
  never take the same row, and a crashed worker's rows return to the pool
  when their lease runs out.
* The provider idempotency key is generated once, stored, and sent on every
  attempt of the row.
* The code is stored as ciphertext only while the row is live; every
  terminal transition wipes it (and a database CHECK forbids otherwise).
* Nothing here logs the address, the code, the API key or provider text.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import mutating_transaction
from app.modules.attendees.models import (
    OtpChallenge,
    OtpDelivery,
    OtpDeliveryStatus,
    OtpPurpose,
)
from app.modules.auth.otp_crypto import OtpCryptoError, decrypt_code, encrypt_code
from app.modules.auth.otp_email import render_otp_email
from app.modules.auth.otp_provider import PROVIDER_RATE_LIMITED, EmailMessage, EmailProvider
from app.modules.auth.otp_throttle import SendThrottle

logger = logging.getLogger(__name__)

LEASE_SECONDS = 60  # > the provider timeout (8 s) with a wide margin
MAX_ATTEMPTS = 5
# Delay after attempt n is BACKOFF_SECONDS[n-1]. All of it fits inside the
# 10-minute OTP lifetime, which is the real deadline.
BACKOFF_SECONDS = (5, 15, 45, 120)
# A provider's `Retry-After` is honoured in full for the row (a retry sooner
# than it asked for only burns an attempt on another 429); the expiry check
# below ends a row whose wait would outlive its code. The worker-wide pause
# is capped separately (otp_throttle.MAX_PAUSE_SECONDS).
DEFAULT_RETENTION_DAYS = 30

# Reason codes the worker itself writes (provider ones are in otp_provider).
REASON_OTP_EXPIRED = "otp_expired"
REASON_CHALLENGE_CONSUMED = "challenge_consumed"
REASON_PAYLOAD_UNREADABLE = "payload_unreadable"
REASON_WORKER_LOST = "worker_lost"

# What an operator is told for each code. Fixed text only - never provider
# output, never anything that came from a request.
ERROR_MESSAGES: dict[str, str] = {
    REASON_OTP_EXPIRED: "The code expired before it could be sent. The attendee can request a new one.",
    REASON_CHALLENGE_CONSUMED: "Not sent: the code had already been used or replaced by a newer one.",
    REASON_PAYLOAD_UNREADABLE: "The stored code could not be decrypted (encryption key changed or missing). Check OTP_OUTBOX_ENCRYPTION_KEY.",
    REASON_WORKER_LOST: "The worker stopped repeatedly while sending this. Check the worker logs.",
    "provider_auth_rejected": "The email provider rejected the API key or the sending domain. Check RESEND_API_KEY and the verified domain.",
    "provider_rejected_message": "The email provider refused this message (for example an invalid recipient address).",
    "provider_rate_limited": "The email provider is rate limiting requests; retries were exhausted.",
    "provider_conflict": "The email provider reported a conflicting request; retries were exhausted.",
    "provider_unavailable": "The email provider had a server error; retries were exhausted.",
    "network_error": "The email provider could not be reached; retries were exhausted.",
    "unexpected_error": "An unexpected error occurred while sending.",
}


def describe_reason(code: str | None) -> str | None:
    if code is None:
        return None
    return ERROR_MESSAGES.get(code, "Unknown reason.")


# --- producer (runs inside the request's transaction) -----------------------


def enqueue(
    db: Session,
    *,
    challenge: OtpChallenge,
    code: str,
    language: str,
    settings: Settings | None = None,
) -> OtpDelivery:
    """Adds the outbox row for `challenge`. The caller's transaction owns it:
    this flushes but never commits."""
    idempotency_key = uuid.uuid4().hex
    ciphertext, key_id = encrypt_code(
        code,
        idempotency_key=idempotency_key,
        email=challenge.email,
        purpose=challenge.purpose.value,
        settings=settings,
    )
    row = OtpDelivery(
        challenge_id=challenge.id,
        email=challenge.email,
        purpose=challenge.purpose,
        language=language,
        status=OtpDeliveryStatus.PENDING.value,
        idempotency_key=idempotency_key,
        payload_ciphertext=ciphertext,
        payload_key_id=key_id,
        expires_at=challenge.expires_at,
    )
    db.add(row)
    db.flush()
    return row


# --- consumer (the worker) ---------------------------------------------------


@dataclass(frozen=True)
class ClaimedDelivery:
    id: int
    challenge_id: int
    email: str
    purpose: str
    language: str
    attempt_count: int  # already includes this attempt
    idempotency_key: str
    payload_ciphertext: bytes
    payload_key_id: str | None
    expires_at: datetime

    def __repr__(self) -> str:  # never let the address or ciphertext reach a log by accident
        return f"ClaimedDelivery(id={self.id}, attempt={self.attempt_count})"


_CLAIM_SQL = text(
    """
    WITH due AS (
        SELECT id FROM otp_deliveries
        WHERE expires_at > now()
          AND (
                (status = 'pending' AND next_attempt_at <= now())
             OR (status = 'processing' AND locked_until < now() AND attempt_count < :max_attempts)
          )
        -- Sign-in and staff-recovery codes first: anonymous registrations of
        -- fresh addresses can queue ACTIVATION rows faster than the provider
        -- drains them, and must not make anyone else's 10-minute code expire.
        ORDER BY (purpose = 'ACTIVATION'), next_attempt_at, id
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE otp_deliveries d
    SET status = 'processing',
        locked_by = :worker_id,
        locked_until = now() + make_interval(secs => CAST(:lease AS double precision)),
        attempt_count = d.attempt_count + 1,
        updated_at = now()
    FROM due
    WHERE d.id = due.id
    RETURNING d.id, d.challenge_id, d.email::text, d.purpose::text, d.language,
              d.attempt_count, d.idempotency_key, d.payload_ciphertext,
              d.payload_key_id, d.expires_at
    """
)


def claim_batch(db: Session, *, worker_id: str, limit: int) -> list[ClaimedDelivery]:
    """Atomically takes up to `limit` due rows (new, retry-due, or whose
    previous worker's lease ran out). Commits via the caller's block."""
    rows = db.execute(
        _CLAIM_SQL,
        {
            "worker_id": worker_id,
            "limit": limit,
            "lease": LEASE_SECONDS,
            "max_attempts": MAX_ATTEMPTS,
        },
    ).all()
    return [
        ClaimedDelivery(
            id=r[0],
            challenge_id=r[1],
            email=r[2],
            purpose=r[3],
            language=r[4],
            attempt_count=r[5],
            idempotency_key=r[6],
            payload_ciphertext=bytes(r[7]),
            payload_key_id=r[8],
            expires_at=r[9],
        )
        for r in rows
    ]


def claim_next(
    session_factory: Callable[[], Session], *, worker_id: str, limit: int
) -> list[ClaimedDelivery]:
    db = session_factory()
    try:
        with mutating_transaction(db) as tx:
            return claim_batch(tx, worker_id=worker_id, limit=limit)
    finally:
        db.close()


def run_maintenance(db: Session) -> tuple[int, int]:
    """Housekeeping the claim query deliberately skips. Returns
    (expired, lost). Safe to run from several workers at once."""
    expired = db.execute(
        text(
            """
            UPDATE otp_deliveries
            SET status = 'expired', reason_code = :reason, payload_ciphertext = NULL,
                payload_key_id = NULL, locked_until = NULL, locked_by = NULL,
                finished_at = now(), updated_at = now()
            WHERE expires_at <= now()
              AND (status = 'pending' OR (status = 'processing' AND locked_until < now()))
            RETURNING id
            """
        ),
        {"reason": REASON_OTP_EXPIRED},
    ).all()
    lost = db.execute(
        text(
            """
            UPDATE otp_deliveries
            SET status = 'failed', reason_code = :reason, payload_ciphertext = NULL,
                payload_key_id = NULL, locked_until = NULL, locked_by = NULL,
                finished_at = now(), updated_at = now()
            WHERE status = 'processing' AND locked_until < now() AND attempt_count >= :max_attempts
            RETURNING id
            """
        ),
        {"reason": REASON_WORKER_LOST, "max_attempts": MAX_ATTEMPTS},
    ).all()
    return len(expired), len(lost)


def maintain(session_factory: Callable[[], Session]) -> tuple[int, int]:
    db = session_factory()
    try:
        with mutating_transaction(db) as tx:
            return run_maintenance(tx)
    finally:
        db.close()


def purge_finished(db: Session, *, older_than_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Deletes finished (accepted/failed/expired) rows older than the
    retention period - they hold no code, but they do hold an address."""
    if older_than_days < 1:
        raise ValueError("older_than_days must be at least 1")
    result = db.execute(
        text(
            "DELETE FROM otp_deliveries WHERE status IN ('accepted','failed','expired') "
            "AND finished_at < now() - make_interval(days => :days) RETURNING id"
        ),
        {"days": older_than_days},
    )
    return len(result.all())


_FINISH_SQL = text(
    """
    UPDATE otp_deliveries
    SET status = :status, reason_code = :reason, last_http_status = :http_status,
        provider_message_id = :message_id,
        payload_ciphertext = NULL, payload_key_id = NULL,
        locked_until = NULL, locked_by = NULL,
        accepted_at = CASE WHEN :status = 'accepted' THEN now() ELSE NULL END,
        finished_at = now(), updated_at = now()
    WHERE id = :id AND status = 'processing' AND locked_by = :worker_id
    RETURNING id
    """
)

_REQUEUE_SQL = text(
    """
    UPDATE otp_deliveries
    SET status = 'pending', reason_code = :reason, last_http_status = :http_status,
        next_attempt_at = now() + make_interval(secs => CAST(:delay AS double precision)),
        locked_until = NULL, locked_by = NULL, updated_at = now()
    WHERE id = :id AND status = 'processing' AND locked_by = :worker_id
    RETURNING id
    """
)


def _record(
    session_factory: Callable[[], Session],
    sql: Any,
    claimed: ClaimedDelivery,
    worker_id: str,
    **params: Any,
) -> bool:
    """Writes an outcome only if this worker STILL holds the lease - a worker
    that was presumed dead and then woke up must not overwrite whoever took
    the row over."""
    db = session_factory()
    try:
        with mutating_transaction(db) as tx:
            row = tx.execute(sql, {"id": claimed.id, "worker_id": worker_id, **params}).first()
        return row is not None
    finally:
        db.close()


def _finish(
    session_factory: Callable[[], Session],
    claimed: ClaimedDelivery,
    worker_id: str,
    status: OtpDeliveryStatus,
    *,
    reason: str | None = None,
    http_status: int | None = None,
    message_id: str | None = None,
) -> str:
    stored = _record(
        session_factory,
        _FINISH_SQL,
        claimed,
        worker_id,
        status=status.value,
        reason=reason,
        http_status=http_status,
        message_id=message_id,
    )
    if not stored:
        logger.warning("OTP delivery %s: lease lost, outcome discarded", claimed.id)
        return "lease_lost"
    logger.info(
        "OTP delivery %s: %s (attempt %d, reason=%s)",
        claimed.id,
        status.value,
        claimed.attempt_count,
        reason or "-",
    )
    return status.value


def _requeue(
    session_factory: Callable[[], Session],
    claimed: ClaimedDelivery,
    worker_id: str,
    *,
    delay: float,
    reason: str,
    http_status: int | None,
) -> str:
    stored = _record(
        session_factory,
        _REQUEUE_SQL,
        claimed,
        worker_id,
        delay=delay,
        reason=reason,
        http_status=http_status,
    )
    if not stored:
        logger.warning("OTP delivery %s: lease lost, outcome discarded", claimed.id)
        return "lease_lost"
    logger.info(
        "OTP delivery %s: retry in %.0fs (attempt %d, reason=%s)",
        claimed.id,
        delay,
        claimed.attempt_count,
        reason,
    )
    return "retry"


def _challenge_is_still_usable(
    session_factory: Callable[[], Session], claimed: ClaimedDelivery
) -> str | None:
    """None if the code may still be sent, else the reason it must not be.
    Checked immediately before every send, so a code that was used or
    superseded while the row waited (or was retrying) is never sent late."""
    db = session_factory()
    try:
        row = db.execute(
            select(OtpChallenge.consumed_at, OtpChallenge.expires_at).where(
                OtpChallenge.id == claimed.challenge_id
            )
        ).first()
        now = datetime.now(UTC)
    finally:
        db.rollback()
        db.close()
    if row is None or row[0] is not None:
        return REASON_CHALLENGE_CONSUMED
    if row[1] <= now or claimed.expires_at <= now:
        return REASON_OTP_EXPIRED
    return None


def process_delivery(
    session_factory: Callable[[], Session],
    claimed: ClaimedDelivery,
    *,
    provider: EmailProvider,
    worker_id: str,
    settings: Settings | None = None,
    throttle: SendThrottle | None = None,
) -> str:
    """One attempt for one claimed row. Returns the outcome label (for
    tests and logs); never raises. On an unexpected internal error the row
    is left `processing` on purpose: its lease runs out and it is retried,
    which is the same recovery as a crashed worker."""
    settings = settings or get_settings()
    try:
        skip = _challenge_is_still_usable(session_factory, claimed)
        if skip is not None:
            if throttle is not None:
                throttle.refund(1)  # no request is made: the provider's allowance is untouched
            return _finish(
                session_factory, claimed, worker_id, OtpDeliveryStatus.EXPIRED, reason=skip
            )

        try:
            code = decrypt_code(
                claimed.payload_ciphertext,
                claimed.payload_key_id,
                idempotency_key=claimed.idempotency_key,
                email=claimed.email,
                purpose=claimed.purpose,
                settings=settings,
            )
        except OtpCryptoError:
            if throttle is not None:
                throttle.refund(1)
            return _finish(
                session_factory,
                claimed,
                worker_id,
                OtpDeliveryStatus.FAILED,
                reason=REASON_PAYLOAD_UNREADABLE,
            )

        content = render_otp_email(claimed.language, OtpPurpose(claimed.purpose), code)
        result = provider.send(
            EmailMessage(
                from_email=settings.otp_from_email,
                to=claimed.email,
                subject=content.subject,
                text=content.text,
                html=content.html,
                idempotency_key=claimed.idempotency_key,
            )
        )
        del code, content

        if result.accepted:
            return _finish(
                session_factory,
                claimed,
                worker_id,
                OtpDeliveryStatus.ACCEPTED,
                http_status=result.http_status,
                message_id=result.provider_message_id,
            )

        reason = result.reason_code or "unexpected_error"
        if reason == PROVIDER_RATE_LIMITED and throttle is not None:
            # Tell the whole worker, not just this row: the rest of the queue
            # would otherwise run into the same limit and lose attempts too.
            paused = throttle.pause(result.retry_after_seconds)
            logger.warning("OTP provider rate limit: sending paused for %.0fs", paused)
        if not result.retryable:
            return _finish(
                session_factory,
                claimed,
                worker_id,
                OtpDeliveryStatus.FAILED,
                reason=reason,
                http_status=result.http_status,
            )
        if claimed.attempt_count >= MAX_ATTEMPTS:
            return _finish(
                session_factory,
                claimed,
                worker_id,
                OtpDeliveryStatus.FAILED,
                reason=reason,
                http_status=result.http_status,
            )
        delay = float(BACKOFF_SECONDS[min(claimed.attempt_count, len(BACKOFF_SECONDS)) - 1])
        if result.retry_after_seconds is not None:
            delay = max(delay, result.retry_after_seconds)
        if datetime.now(UTC) + timedelta(seconds=delay) >= claimed.expires_at:
            # No later attempt could still deliver a live code.
            return _finish(
                session_factory,
                claimed,
                worker_id,
                OtpDeliveryStatus.EXPIRED,
                reason=REASON_OTP_EXPIRED,
                http_status=result.http_status,
            )
        return _requeue(
            session_factory,
            claimed,
            worker_id,
            delay=delay,
            reason=reason,
            http_status=result.http_status,
        )
    except Exception as exc:  # noqa: BLE001 - the lease makes this recoverable
        logger.error("OTP delivery %s: internal error (%s)", claimed.id, type(exc).__name__)
        return "error"


# --- admin read side ----------------------------------------------------------


def mask_email(email: str) -> str:
    """`namiq@gmail.com` -> `n***@gmail.com`: enough to recognise an
    attendee when helping them, not enough to harvest addresses."""
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


@dataclass(frozen=True)
class DeliveryView:
    id: int
    created_at: datetime
    updated_at: datetime
    email_masked: str
    purpose: str
    language: str
    status: str
    attempts: int
    next_attempt_at: datetime | None
    accepted_at: datetime | None
    finished_at: datetime | None
    provider_message_id: str | None
    http_status: int | None
    reason_code: str | None
    reason: str | None


def list_deliveries(
    db: Session,
    *,
    status: str | None = None,
    email: str | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> tuple[list[DeliveryView], int | None]:
    """Newest first, keyset-paginated on `id`. Selects explicit columns -
    the ciphertext and idempotency key are never even read."""
    columns = (
        OtpDelivery.id,
        OtpDelivery.created_at,
        OtpDelivery.updated_at,
        OtpDelivery.email,
        OtpDelivery.purpose,
        OtpDelivery.language,
        OtpDelivery.status,
        OtpDelivery.attempt_count,
        OtpDelivery.next_attempt_at,
        OtpDelivery.accepted_at,
        OtpDelivery.finished_at,
        OtpDelivery.provider_message_id,
        OtpDelivery.last_http_status,
        OtpDelivery.reason_code,
    )
    query = select(*columns).order_by(OtpDelivery.id.desc()).limit(limit + 1)
    if status is not None:
        query = query.where(OtpDelivery.status == status)
    if email is not None:
        query = query.where(OtpDelivery.email == email.strip().lower())
    if before_id is not None:
        query = query.where(OtpDelivery.id < before_id)
    rows = db.execute(query).all()
    page = rows[:limit]
    items = [
        DeliveryView(
            id=r.id,
            created_at=r.created_at,
            updated_at=r.updated_at,
            email_masked=mask_email(r.email),
            purpose=r.purpose.value,
            language=r.language,
            status=r.status,
            attempts=r.attempt_count,
            next_attempt_at=r.next_attempt_at if r.status == "pending" else None,
            accepted_at=r.accepted_at,
            finished_at=r.finished_at,
            provider_message_id=r.provider_message_id,
            http_status=r.last_http_status,
            reason_code=r.reason_code,
            reason=describe_reason(r.reason_code),
        )
        for r in page
    ]
    next_cursor = page[-1].id if len(rows) > limit and page else None
    return items, next_cursor


@dataclass(frozen=True)
class DeliverySummary:
    counts: dict[str, int]
    due_now: int
    oldest_due_seconds: int | None


def summarise(db: Session) -> DeliverySummary:
    counts = {s.value: 0 for s in OtpDeliveryStatus}
    by_status: Sequence[Any] = db.execute(
        text("SELECT status, count(*) FROM otp_deliveries GROUP BY status")
    ).all()
    for status, n in by_status:
        counts[status] = int(n)
    due_row: Any = db.execute(
        text(
            "SELECT count(*), EXTRACT(EPOCH FROM (now() - min(next_attempt_at))) "
            "FROM otp_deliveries WHERE status = 'pending' AND next_attempt_at <= now()"
        )
    ).one()
    due, oldest = due_row
    return DeliverySummary(
        counts=counts,
        due_now=int(due),
        oldest_due_seconds=int(oldest) if oldest is not None else None,
    )
