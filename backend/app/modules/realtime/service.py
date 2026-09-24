"""Realtime stream support — ARCHITECTURE.md §17, §19.3.

Locked timing constants (§17.9): 15 s heartbeat, 30-minute max stream
age. (The 45 s dead-connection rule is client-side only — nothing here
enforces it; the server's job is only to keep emitting a frame of some
kind at least every 15 s under normal operation, which is what makes
the client's 45 s rule a reliable signal of a genuinely stalled or
buffered connection.)
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.core.sessions import resolve_attendee_session
from app.db.session import SessionLocal
from app.modules.admin.models import EventConfig
from app.modules.attendees.models import User
from app.modules.realtime.broker import event_bus

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 15
MAX_STREAM_AGE_SECONDS = 30 * 60


def resolve_attendee_for_stream(raw_token: str | None) -> User:
    """A short-lived session, opened and closed immediately — never
    held for the stream's own lifetime (up to 30 minutes, §17.9). The
    exact same check (`resolve_attendee_session`) as every other
    attendee-authenticated read in this codebase, called once at
    connection start and again on every heartbeat tick — a revoked or
    expired session fails identically everywhere (§8.3: "Session death
    must propagate to open streams")."""
    db = SessionLocal()
    try:
        return resolve_attendee_session(db, raw_token)
    finally:
        db.close()


def current_dataset_generation() -> int:
    """Read fresh on every call — never cached in this process. M14
    does not implement reset (M15's job), so this value never actually
    changes during a single run; a future in-process cache would go
    silently stale the moment M15's CLI (a separate process) advances
    it, so this reads live instead of trading a negligible, indexed
    single-row `SELECT` (§16.4's identical reasoning) for that risk."""
    db = SessionLocal()
    try:
        return int(
            db.execute(
                select(EventConfig.dataset_generation).where(EventConfig.id == 1)
            ).scalar_one()
        )
    finally:
        db.close()


def publish_award_signals(user_id: int) -> None:
    """Best-effort (§17.7.2): "a failure to publish is logged and
    never fails the award. The 20 s reconciliation covers it." Called
    by M11/M12 AFTER their own transaction has already committed, for
    a genuinely NEW award only — never a replay, never an
    already-rewarded feedback submission, both of which changed
    nothing for anyone to be notified about."""
    try:
        event_bus.publish_personal(user_id, "state_changed", {"scope": "personal"})
        event_bus.publish_broadcast("leaderboard_changed", {})
    except Exception:
        logger.exception("failed to publish realtime signal for user_id=%s", user_id)
