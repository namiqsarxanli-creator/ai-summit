"""Planning and sending programme reminders (see models.py for the guarantees).

* `plan` (the worker, every few minutes): for every ACTIVE attendee who opted
  in, the wanted reminders are derived from the real programme; new ones are
  inserted, moved ones re-timed, vanished ones (a cancelled booking) cancelled.
  Only reminders still in the future are planned.
* `run_once` (the worker loop): claims due rows with a lease, re-checks
  everything at send time (switch on, consent given, person active, item still
  there, not stale), sends through the email provider with a stable idempotency
  key and records the outcome. A retryable failure is retried with exponential
  backoff up to `MAX_ATTEMPTS`; then the row is FAILED. No database transaction
  is held open across the provider call.

Nothing here logs an address or message text.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import mutating_transaction
from app.modules.admin.models import EventConfig
from app.modules.attendees.models import User, UserStatus
from app.modules.auth.otp_provider import EmailMessage, EmailProvider
from app.modules.masterclasses.models import MasterclassEvent
from app.modules.reminders.models import ReminderDelivery

logger = logging.getLogger("facemark.reminders")

BEFORE: Final = timedelta(minutes=10)
EVE_AT: Final = time(18, 0)  # 12 November, event time
STALE_AFTER: Final = timedelta(minutes=15)  # a reminder this late is no longer useful
LEASE: Final = timedelta(minutes=2)
MAX_ATTEMPTS: Final = 5
BATCH: Final = 20
_REMINDED = {"confirmed", "scheduled", "checked_in"}  # not a waiting place


def enabled(db: Session) -> bool:
    return bool(
        db.execute(
            select(EventConfig.reminders_enabled).where(EventConfig.id == 1)
        ).scalar_one_or_none()
    )


def _event(db: Session) -> MasterclassEvent | None:
    return db.execute(select(MasterclassEvent).order_by(MasterclassEvent.id)).scalars().first()


def _eve(event: MasterclassEvent) -> datetime:
    zone = ZoneInfo(event.timezone)
    day: date = event.event_date - timedelta(days=1)
    return datetime.combine(day, EVE_AT, zone).astimezone(UTC)


def wanted(db: Session, user: User, event: MasterclassEvent) -> dict[str, datetime]:
    """item_key -> when the reminder is due, from the person's real programme."""
    from app.modules.competition.programme import programme

    out = {"eve": _eve(event)}
    for item in programme(db, user.id)["items"]:
        if item["status"] in _REMINDED:
            start = datetime.fromisoformat(item["starts_at"])
            out[f"{item['kind']}:{item['id']}"] = start - BEFORE
    return out


def plan(db: Session, now: datetime) -> dict[str, int]:
    """Caller holds the reset fence (a mutating transaction)."""
    counts = {"created": 0, "retimed": 0, "cancelled": 0}
    event = _event(db)
    if event is None or not enabled(db):
        return counts
    existing: dict[tuple[int, str], ReminderDelivery] = {
        (r.user_id, r.item_key): r
        for r in db.execute(
            select(ReminderDelivery).where(ReminderDelivery.status == "PENDING").with_for_update()
        ).scalars()
    }
    users = db.execute(
        select(User).where(User.status == UserStatus.ACTIVE, User.reminder_consent_at.is_not(None))
    ).scalars()
    keep: set[tuple[int, str]] = set()
    known = {
        (uid, key)
        for uid, key in db.execute(select(ReminderDelivery.user_id, ReminderDelivery.item_key))
    }
    for user in users:
        for key, due in wanted(db, user, event).items():
            if due <= now:
                continue
            keep.add((user.id, key))
            row = existing.get((user.id, key))
            if row is not None:
                if row.due_at != due:
                    row.due_at = due
                    row.next_attempt_at = due
                    counts["retimed"] += 1
            elif (user.id, key) not in known:  # sent or cancelled before: never again
                db.add(
                    ReminderDelivery(user_id=user.id, item_key=key, due_at=due, next_attempt_at=due)
                )
                counts["created"] += 1
    for pair, row in existing.items():
        if pair not in keep and row.due_at > now:
            row.status = "CANCELLED"
            row.reason_code = "item_gone"
            counts["cancelled"] += 1
    db.flush()
    return counts


def cancel_for(db: Session, user_id: int, reason: str) -> int:
    """Consent withdrawn (or the person left): nothing pending is sent."""
    result = db.execute(
        update(ReminderDelivery)
        .where(ReminderDelivery.user_id == user_id, ReminderDelivery.status == "PENDING")
        .values(status="CANCELLED", reason_code=reason)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Claimed:
    id: int
    user_id: int
    item_key: str
    due_at: datetime
    attempt_count: int


def _claim(db: Session, worker_id: str, now: datetime, limit: int) -> list[Claimed]:
    # A worker that died mid-send gives its rows back when the lease lapses.
    db.execute(
        update(ReminderDelivery)
        .where(ReminderDelivery.status == "SENDING", ReminderDelivery.lease_until < now)
        .values(status="PENDING", lease_owner=None, lease_until=None)
    )
    rows = list(
        db.execute(
            select(ReminderDelivery)
            .where(ReminderDelivery.status == "PENDING", ReminderDelivery.next_attempt_at <= now)
            .order_by(ReminderDelivery.next_attempt_at, ReminderDelivery.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    for r in rows:
        r.status = "SENDING"
        r.lease_owner = worker_id
        r.lease_until = now + LEASE
    db.flush()
    return [Claimed(r.id, r.user_id, r.item_key, r.due_at, r.attempt_count) for r in rows]


def _render(item: dict[str, Any] | None, event: MasterclassEvent) -> tuple[str, str]:
    zone = ZoneInfo(event.timezone)
    if item is None:
        subject = f"{event.title}: sabah görüşürük / see you tomorrow"
        body = (
            f"Sabah {event.title} başlayır. Proqramınızı AI Summit-də yoxlayın.\n"
            f"Завтра начинается {event.title}. Проверьте свою программу в AI Summit.\n"
            f"{event.title} starts tomorrow. Check your programme in AI Summit."
        )
        return subject, body
    start = datetime.fromisoformat(item["starts_at"]).astimezone(zone).strftime("%H:%M")
    title = str(item["title"])
    where = f" · {item['place']}" if item.get("place") else ""
    subject = f"{start} · {title}"
    body = (
        f"10 dəqiqədən sonra: {title}{where} ({start}).\n"
        f"Через 10 минут: {title}{where} ({start}).\n"
        f"In 10 minutes: {title}{where} ({start})."
    )
    return subject, body


def _prepare(db: Session, c: Claimed, now: datetime) -> tuple[EmailMessage | None, str | None]:
    """The message to send, or (None, why it is not sent)."""
    from app.modules.competition.programme import programme

    if not enabled(db):
        return None, "switched_off"
    user = db.get(User, c.user_id)
    if user is None or user.status != UserStatus.ACTIVE or user.reminder_consent_at is None:
        return None, "no_consent"
    if now > c.due_at + STALE_AFTER:
        return None, "stale"
    event = _event(db)
    if event is None:
        return None, "item_gone"
    item = None
    if c.item_key != "eve":
        kind, _, ref = c.item_key.partition(":")
        item = next(
            (
                i
                for i in programme(db, user.id)["items"]
                if i["kind"] == kind and str(i["id"]) == ref and i["status"] in _REMINDED
            ),
            None,
        )
        if item is None:
            return None, "item_gone"
    subject, text = _render(item, event)
    return (
        EmailMessage(
            from_email=get_settings().otp_from_email,
            to=user.email,
            subject=subject,
            text=text,
            html="<p>" + "<br>".join(html.escape(line) for line in text.splitlines()) + "</p>",
            idempotency_key=f"reminder-{c.id}",
        ),
        None,
    )


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(60 * 2 ** max(attempts - 1, 0), 900))


def run_once(
    session_factory: Callable[[], Session],
    *,
    provider: EmailProvider,
    worker_id: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    limit: int = BATCH,
) -> int:
    """Claim, check and send the due reminders. Returns how many were attempted."""
    db = session_factory()
    try:
        with mutating_transaction(db) as tx:
            if limit < 1 or not enabled(tx):
                return 0
            claimed = _claim(tx, worker_id, now(), limit)
    finally:
        db.close()
    attempted = 0
    for c in claimed:
        db = session_factory()
        try:
            with mutating_transaction(db) as tx:
                message, skip = _prepare(tx, c, now())
                if message is None:
                    tx.execute(
                        update(ReminderDelivery)
                        .where(
                            ReminderDelivery.id == c.id, ReminderDelivery.lease_owner == worker_id
                        )
                        .values(
                            status="CANCELLED", reason_code=skip, lease_owner=None, lease_until=None
                        )
                    )
                    continue
        finally:
            db.close()
        result = provider.send(message)  # no transaction is open here
        attempted += 1
        db = session_factory()
        try:
            with mutating_transaction(db) as tx:
                row = tx.execute(
                    select(ReminderDelivery)
                    .where(ReminderDelivery.id == c.id, ReminderDelivery.lease_owner == worker_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if row is None:  # the lease was lost meanwhile; the idempotency key protects
                    continue
                row.attempt_count += 1
                row.lease_owner = None
                row.lease_until = None
                stamp = now()
                if result.accepted:
                    row.status = "SENT"
                    row.sent_at = stamp
                    row.provider_message_id = result.provider_message_id
                    row.reason_code = None
                elif result.retryable and row.attempt_count < MAX_ATTEMPTS:
                    row.status = "PENDING"
                    row.reason_code = result.reason_code
                    wait = _backoff(row.attempt_count)
                    if result.retry_after_seconds:
                        wait = max(wait, timedelta(seconds=result.retry_after_seconds))
                    row.next_attempt_at = stamp + wait
                else:
                    row.status = "FAILED"
                    row.reason_code = result.reason_code
                logger.info("reminder %s -> %s (%s)", row.id, row.status, row.reason_code)
        finally:
            db.close()
    return attempted
