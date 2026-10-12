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
import time

from sqlalchemy import select

from app.core.config import get_settings
from app.core.sessions import resolve_attendee_session
from app.db.session import SessionLocal
from app.modules.admin.models import EventConfig
from app.modules.attendees.models import User
from app.modules.leaderboard.service import leaderboard_push_payload
from app.modules.realtime.broker import event_bus

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 15
MAX_STREAM_AGE_SECONDS = 30 * 60
# A phone with a few tabs open is well under the per-attendee cap; the global
# one bounds what the single worker holds open at once.
MAX_STREAMS_PER_ATTENDEE = 5
MAX_STREAMS_TOTAL = 3000


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


_GENERATION_TTL_SECONDS = 1.0
_generation_cache: tuple[int, int, float] | None = None  # (bus version, generation, read at)


def cached_dataset_generation() -> int:
    """`current_dataset_generation` for the per-response header: re-read at most
    once a second, and at once after this process saw a dataset reset (the
    reset broadcast bumps `event_bus.leaderboard_version`). Every request used to
    pay a database round trip and a pooled connection for it; under load that
    alone was a noticeable share of the single worker's time."""
    global _generation_cache
    if get_settings().leaderboard_cache_seconds <= 0:  # read caching off (the test suite)
        return current_dataset_generation()
    version = event_bus.leaderboard_version
    cached = _generation_cache
    now = time.monotonic()
    if cached is not None and cached[0] == version and now - cached[2] < _GENERATION_TTL_SECONDS:
        return cached[1]
    generation = current_dataset_generation()
    _generation_cache = (version, generation, now)
    return generation


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


def publish_leaderboard_changed() -> None:
    """Invalidate the shared ranking, rank once, push the new top N to every
    stream (see `leaderboard_push_payload`). If ranking fails the event still
    goes out without a payload and clients fall back to refetching."""
    event_bus.invalidate_leaderboard()
    payload: dict[str, object] = {}
    try:
        db = SessionLocal()
        try:
            payload = {"leaderboard": leaderboard_push_payload(db)}
        finally:
            db.close()
    except Exception:
        logger.exception("failed to build the leaderboard push")
    event_bus.publish_broadcast("leaderboard_changed", payload)


def publish_award_signals(user_id: int) -> None:
    """Best-effort (§17.7.2): "a failure to publish is logged and
    never fails the award. The 20 s reconciliation covers it." Called
    by M11/M12 AFTER their own transaction has already committed, for
    a genuinely NEW award only — never a replay, never an
    already-rewarded feedback submission, both of which changed
    nothing for anyone to be notified about."""
    try:
        event_bus.publish_personal(user_id, "state_changed", {"scope": "personal"})
        publish_leaderboard_changed()
    except Exception:
        logger.exception("failed to publish realtime signal for user_id=%s", user_id)


def publish_profile_signals(user_id: int, *, leaderboard: bool) -> None:
    """After an attendee's own profile, interests, consent or photo changed
    (committed). Best-effort, exactly like `publish_award_signals`: the 20 s
    reconciliation covers a missed one.

    `state_changed` (personal) makes the attendee's OTHER open devices refetch
    their profile and recommendations; `leaderboard_changed` is sent only when
    something the leaderboard shows (name, profession, photo) changed."""
    try:
        event_bus.publish_personal(user_id, "state_changed", {"scope": "personal"})
        if leaderboard:
            publish_leaderboard_changed()
    except Exception:
        logger.exception("failed to publish profile signal for user_id=%s", user_id)
