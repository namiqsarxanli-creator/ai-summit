"""`POST /api/attendees/feedback` — ARCHITECTURE.md §15.

Not rate-limited: like `POST /api/staff/points/award`, the reward is
structurally de-duplicated by `uq_points_feedback_user_booth`, and
feedback itself is deliberately unbounded (multiple submissions are
legitimate, §7.11) — nothing here is the kind of cheap, consequential
abuse §21.7 rate-limits against.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import SessionToken
from app.db.session import DbSession, mutating_transaction
from app.modules.feedback.schemas import FeedbackRequest, FeedbackResponse
from app.modules.feedback.service import submit_feedback

router = APIRouter(prefix="/attendees", tags=["feedback"])


@router.post("/feedback", response_model=FeedbackResponse)
def feedback(payload: FeedbackRequest, token: SessionToken, db: DbSession) -> FeedbackResponse:
    """`token` is the raw session cookie — a string, not yet an
    authenticated identity (§14.3's rule applies here too). Real
    authentication happens only inside `submit_feedback`, after the
    fence (§19.4, invariant #29)."""
    with mutating_transaction(db) as tx:
        return submit_feedback(tx, raw_session_token=token, payload=payload)
