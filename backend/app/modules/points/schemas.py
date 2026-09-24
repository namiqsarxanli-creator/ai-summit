"""`POST /api/staff/points/award` request/response — ARCHITECTURE.md
§12.2 (the canonical request contract), §13.4 (the canonical response
snapshot).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AwardTaskPointsRequest(BaseModel):
    """THE canonical award request contract (§12.2), reproduced exactly
    — this is the single authoritative definition; no other module
    restates it. `extra="forbid"` (invariant #16) is what makes
    `booth_id`, `staff_id`, `source`, `task_id` and `created_at`
    unrepresentable by a client: they are deliberately absent below,
    not merely unused."""

    model_config = ConfigDict(extra="forbid")

    card_token: str
    points: int
    idempotency_key: uuid.UUID
    scan_ref: str
    # deliberately absent: booth_id, staff_id, source, task_id, timestamp


class AwardResponse(BaseModel):
    """The canonical response snapshot (§13.4) — every field a fact
    about THIS award, fixed at commit time, byte-identical on replay.
    `rank` is deliberately excluded (§13.4): it is not an immutable
    property of an award."""

    model_config = ConfigDict(extra="forbid")

    points_log_id: int
    points_awarded: int
    resulting_total: int
    attendee_name: str
    booth_name: str
    awarded_at: datetime
    replayed: bool
