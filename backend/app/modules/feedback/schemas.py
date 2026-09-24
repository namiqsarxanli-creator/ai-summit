"""`POST /api/attendees/feedback` request/response — ARCHITECTURE.md
§15, §21.4.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FeedbackRequest(BaseModel):
    """`extra="forbid"` (invariant #16) — deliberately absent:
    `user_id`, `booth_id`, `points`, `source`, `staff_id`,
    `entered_by_system`, `dataset_generation`. Attendee identity comes
    only from the session; the booth is resolved server-side from
    `booth_slug`; the reward amount comes only from
    `event_config.feedback_points` — none of them are expressible here."""

    model_config = ConfigDict(extra="forbid")

    booth_slug: str = Field(min_length=1, max_length=200)
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackResponse(BaseModel):
    """§15's outcome contract, verbatim — ONE shape for both the
    rewarded and the already-rewarded case, both `200`."""

    model_config = ConfigDict(extra="forbid")

    feedback_id: int
    awarded_points: int
    already_rewarded: bool
    total_points: int
