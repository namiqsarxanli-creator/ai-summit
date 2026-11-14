"""`POST /api/staff/points/award` request/response — ARCHITECTURE.md
§12.2 (the canonical request contract), §13.4 (the canonical response
snapshot).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AwardTaskPointsRequest(BaseModel):
    """THE canonical award request contract (§12.2), reproduced exactly
    - this is the single authoritative definition; no other module
    restates it. `extra="forbid"` (invariant #16) is what makes
    `booth_id`, `staff_id`, `source`, `points` and `created_at`
    unrepresentable by a client: they are deliberately absent below,
    not merely unused."""

    model_config = ConfigDict(extra="forbid")

    card_token: str
    # The activity being awarded. The POINT VALUE is not in the request at
    # all: it is the activity's own server-side rule, so a client cannot
    # choose, inflate or "round up" an award.
    task_id: int = Field(gt=0)
    # The rule the staff member CONFIRMED, as the server described it in
    # `GET /api/staff/tasks` (or in a TASK_RULE_CHANGED answer). It decides
    # nothing: it is only compared with the rule in force at award time, and
    # a mismatch awards nothing. Opaque to the client.
    rule_revision: str = Field(min_length=1, max_length=64)
    idempotency_key: uuid.UUID
    scan_ref: str
    # deliberately absent: points, booth_id, staff_id, source, timestamp


class AwardRequestBody(BaseModel):
    """What the endpoint actually parses: the current contract OR an older
    staff page's. It exists so an out-of-date cached page gets a clear
    "client outdated" answer (ClientOutdatedError) instead of a bare 422. `points` is accepted only
    to be refused, never read. Anything else unknown is still rejected
    (invariant #16)."""

    model_config = ConfigDict(extra="forbid")

    card_token: str
    idempotency_key: uuid.UUID
    scan_ref: str
    task_id: int | None = Field(default=None, gt=0)
    rule_revision: str | None = Field(default=None, max_length=64)
    points: object | None = None

    def current_contract(self) -> AwardTaskPointsRequest | None:
        """The strict request, or `None` when this body is an older page's
        (no activity, no confirmed revision, or a client-chosen point value)."""
        if self.points is not None or self.task_id is None or not self.rule_revision:
            return None
        return AwardTaskPointsRequest(
            card_token=self.card_token,
            task_id=self.task_id,
            rule_revision=self.rule_revision,
            idempotency_key=self.idempotency_key,
            scan_ref=self.scan_ref,
        )


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
    # Added with activities. Optional so a snapshot stored before they
    # existed still replays.
    task_id: int | None = None
    task_title: str | None = None


MAX_REVERSAL_REASON = 300
MIN_REVERSAL_REASON = 3


class ReversalRequest(BaseModel):
    """`POST /api/admin/points-log/{entry_id}/reverse` - an administrator cancels
    one AWARD in full.

    What is deliberately NOT here: the amount, the attendee, the booth, the
    activity, the source. They are all read from the original entry on the
    server (`extra="forbid"` makes any of them a 422), so a client can neither
    reverse "a bit" nor move the reversal to someone else. Only the reason and
    the idempotency key come from the client; the entry is the URL's."""

    model_config = ConfigDict(extra="forbid")

    # Free text typed by an administrator. Whitespace is collapsed, and the text
    # must be readable (no control characters): it is kept in the ledger, shown
    # in the audit trail and exported, and must be safe in all three. It is
    # never written to a log.
    reason: str = Field(max_length=2000)
    idempotency_key: uuid.UUID

    @field_validator("reason")
    @classmethod
    def _normalise_reason(cls, value: str) -> str:
        text = " ".join(value.split())
        if not all(ch.isprintable() for ch in text):
            raise ValueError("the reason may only contain readable characters")
        if not MIN_REVERSAL_REASON <= len(text) <= MAX_REVERSAL_REASON:
            raise ValueError(
                f"the reason must be {MIN_REVERSAL_REASON}-{MAX_REVERSAL_REASON} characters"
            )
        return text


class ReversalResponse(BaseModel):
    """The canonical answer, kept as the idempotency snapshot and replayed
    verbatim (only `replayed` differs). Facts about THIS reversal; no name, no
    reason."""

    model_config = ConfigDict(extra="forbid")

    reversal_id: int
    reversed_entry_id: int
    # Positive: how many points were taken away.
    points_reversed: int
    # The attendee's total after the reversal.
    resulting_total: int
    created_at: datetime
    replayed: bool
