"""Feedback submission with automatic, structurally-once reward —
ARCHITECTURE.md §15.

Called inside the caller's `mutating_transaction(db)` block — the fence
is already held by the time `submit_feedback` runs. Every step below is
numbered to match §15's own pseudocode exactly.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    BoothNotFoundError,
    DuplicateFeedbackRewardError,
    translate_integrity_error,
)
from app.core.sessions import resolve_attendee_session
from app.modules.admin.models import EventConfig
from app.modules.booths.service import get_active_booth_by_slug
from app.modules.feedback.models import Feedback
from app.modules.feedback.schemas import FeedbackRequest, FeedbackResponse
from app.modules.points.models import PointsLog, PointsSource


def submit_feedback(
    db: Session, *, raw_session_token: str | None, payload: FeedbackRequest
) -> FeedbackResponse:
    # 0b — AUTHORITATIVE auth: session -> attendee, require ACTIVE
    # (built into resolve_attendee_session: PROVISIONAL raises
    # RegistrationNotVerifiedError). Booth resolution is also inside
    # the fence — both are reads the write below depends on (§15, §19.4).
    attendee = resolve_attendee_session(db, raw_session_token)
    booth = get_active_booth_by_slug(db, payload.booth_slug)
    if booth is None:
        raise BoothNotFoundError()

    # 1 — INSERT feedback. Always succeeds in normal operation: feedback
    # is deliberately NOT unique on (user_id, booth_id) (§7.11), and
    # every other field is already validated (rating by pydantic,
    # user_id/booth_id by the resolutions above) — but any IntegrityError
    # here is still translated by constraint name (§7.14, §20.4: "a raw
    # IntegrityError reaching the handler is a bug, and is logged as
    # one"), never left to propagate raw.
    feedback = Feedback(
        user_id=attendee.id,
        booth_id=booth.id,
        rating=payload.rating,
        comment=payload.comment,
    )
    db.add(feedback)
    try:
        db.flush()
    except IntegrityError as err:
        raise translate_integrity_error(err) from err

    event_config = db.execute(select(EventConfig).where(EventConfig.id == 1)).scalar_one()
    feedback_points = event_config.feedback_points

    # 2 — SAVEPOINT reward. `uq_points_feedback_user_booth` (§7.12,
    # §7.14) is the sole authority on eligibility — never a
    # check-then-insert (§15: "that pattern has a window between check
    # and insert and would double-award under exactly the conditions a
    # live demo produces"). Only a violation of THIS exact constraint
    # is recovered; anything else fails the outer transaction.
    savepoint = db.begin_nested()
    points_log = PointsLog(
        user_id=attendee.id,
        booth_id=booth.id,
        source=PointsSource.FEEDBACK,
        points=feedback_points,
        entered_by_staff_id=None,
        entered_by_system=True,
        dedupe_key=None,
    )
    db.add(points_log)
    try:
        db.flush()
    except IntegrityError as err:
        savepoint.rollback()
        translated = translate_integrity_error(err)
        if not isinstance(translated, DuplicateFeedbackRewardError):
            raise translated from err
        awarded_points = 0
        already_rewarded = True
    else:
        savepoint.commit()
        awarded_points = feedback_points
        already_rewarded = False

    # 3 — recompute the authoritative total, over the whole ledger —
    # task awards, feedback awards, reversals, anything else. Never a
    # cached value plus the new reward (§14.1, invariant #1).
    resulting_total = db.execute(
        select(func.coalesce(func.sum(PointsLog.points), 0)).where(PointsLog.user_id == attendee.id)
    ).scalar_one()

    return FeedbackResponse(
        feedback_id=feedback.id,
        awarded_points=awarded_points,
        already_rewarded=already_rewarded,
        total_points=int(resulting_total),
    )
