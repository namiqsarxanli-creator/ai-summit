"""Explicit M16 admin request/response contracts. No ledger write contract exists."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from app.modules.staff.models import StaffRole


class Mutation(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StaffCreate(Mutation):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    full_name: str = Field(min_length=1, max_length=200)
    role: StaffRole
    booth_id: int | None = None


class BoothWrite(Mutation):
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    location_label: str | None = None
    is_active: bool = True
    allow_repeat_task_points: bool | None = None


class BoothEdit(Mutation):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    location_label: str | None = None
    is_active: bool | None = None
    allow_repeat_task_points: bool | None = None


class InterestWrite(Mutation):
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=200)
    sort_order: int = 0
    is_active: bool = True


class InterestEdit(Mutation):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    sort_order: int | None = None
    is_active: bool | None = None


class WeightWrite(Mutation):
    weight: Decimal = Field(gt=0, le=1, max_digits=3, decimal_places=2)


class TaskWrite(Mutation):
    booth_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    sort_order: int = 0
    is_active: bool = True
    # The award rule: points one award of this activity is worth (1-100,
    # the same range as the legacy presets), set by the admin, never by staff.
    points: int = Field(ge=1, le=100)
    # None = inherit the booth's, then the event's, repeat setting.
    allow_repeat_points: bool | None = None


class TaskEdit(Mutation):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None
    points: int | None = Field(default=None, ge=1, le=100)
    # Send an explicit null to go back to inheriting.
    allow_repeat_points: bool | None = None

    @model_validator(mode="after")
    def _points_cannot_be_cleared(self) -> TaskEdit:
        # An activity without a value cannot be awarded; clearing it by
        # sending null is almost certainly a mistake. Deactivate instead.
        if "points" in self.model_fields_set and self.points is None:
            raise ValueError("points cannot be cleared; deactivate the activity instead")
        return self


class PresetWrite(Mutation):
    booth_id: int | None = Field(default=None, gt=0)
    points: int = Field(ge=1, le=100)
    label: str | None = Field(default=None, max_length=100)
    sort_order: int = 0
    is_active: bool = True


class PresetEdit(Mutation):
    points: int | None = Field(default=None, ge=1, le=100)
    label: str | None = Field(default=None, max_length=100)
    sort_order: int | None = None
    is_active: bool | None = None


class ConfigEdit(Mutation):
    feedback_points: int | None = Field(default=None, ge=1, le=100)
    leaderboard_top_n: int | None = Field(default=None, ge=1, le=100)
    allow_repeat_task_points_default: bool | None = None
    # Migration 0020: count only the Beyond Now competition in the ranking.
    competition_mode: bool | None = None
    # Migration 0024: programme reminder emails (attendees still opt in).
    reminders_enabled: bool | None = None


# --- OTP delivery status (read-only) -------------------------------------------
# Explicit output contract: there is deliberately NO field for the code, the
# ciphertext, the idempotency key or the full address, so no future column can
# leak through this endpoint by accident.

OtpDeliveryStatusName = Literal["pending", "processing", "accepted", "failed", "expired"]


class OtpDeliveryItem(BaseModel):
    id: int
    created_at: datetime
    updated_at: datetime
    email_masked: str
    purpose: Literal["ACTIVATION", "LOGIN", "STAFF_PASSWORD_RESET"]
    language: Literal["az", "en", "ru"]
    status: OtpDeliveryStatusName
    attempts: int
    next_attempt_at: datetime | None
    accepted_at: datetime | None
    finished_at: datetime | None
    provider_message_id: str | None
    http_status: int | None
    reason_code: str | None
    reason: str | None


class OtpDeliverySummary(BaseModel):
    counts: dict[str, int]
    due_now: int
    oldest_due_seconds: int | None


class OtpDeliveryPage(BaseModel):
    items: list[OtpDeliveryItem]
    next_before_id: int | None
    summary: OtpDeliverySummary
    # "accepted" = taken by the email provider, not proof of inbox delivery.
    note: str = "accepted means the email provider took the message, not that it reached an inbox."


# --- paginated, filterable admin lists ---------------------------------------
#
# Query models forbid unknown parameters: a misspelt filter must be a 422, not
# an unfiltered (and so misleading) page. Datetimes must carry a UTC offset
# (`Z` or `+04:00`; a literal `+` has to be sent as `%2B`) and ranges are
# half-open - `from` inclusive, `to` exclusive - see admin/pagination.py.

LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 100


class _ListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=LIST_DEFAULT_LIMIT, ge=1, le=LIST_MAX_LIMIT)

    @field_validator("cursor")
    @classmethod
    def _blank_cursor_is_none(cls, value: str | None) -> str | None:
        return value or None


def _check_range(start: AwareDatetime | None, end: AwareDatetime | None) -> None:
    if start is not None and end is not None and start >= end:
        raise ValueError("the start of the date range must be before its end")


class AttendeeFilters(BaseModel):
    """The attendee search and filters. ONE definition, used by the list
    (`AttendeeListQuery`) and by the CSV export, so a filter means the same
    thing on screen and in the file."""

    model_config = ConfigDict(extra="forbid")

    q: str | None = Field(default=None, max_length=100)
    status: Literal["PROVISIONAL", "ACTIVE"] | None = None
    sponsor_consent: bool | None = None
    registered_from: AwareDatetime | None = None  # inclusive
    registered_to: AwareDatetime | None = None  # exclusive

    @field_validator("q")
    @classmethod
    def _blank_search_is_none(cls, value: str | None) -> str | None:
        value = " ".join(value.split()) if value else None
        return value or None

    @model_validator(mode="after")
    def _range(self) -> AttendeeFilters:
        _check_range(self.registered_from, self.registered_to)
        return self


class AttendeeListQuery(_ListQuery, AttendeeFilters):
    pass


class PointsFilters(BaseModel):
    """The ledger filters; shared by the list and the CSV export."""

    model_config = ConfigDict(extra="forbid")

    user_id: int | None = Field(default=None, ge=1)
    booth_id: int | None = Field(default=None, ge=1)
    task_id: int | None = Field(default=None, ge=1)
    staff_id: int | None = Field(default=None, ge=1)
    source: Literal["TASK", "FEEDBACK"] | None = None
    entry_type: Literal["AWARD", "REVERSAL"] | None = None
    created_from: AwareDatetime | None = None  # inclusive
    created_to: AwareDatetime | None = None  # exclusive

    @model_validator(mode="after")
    def _range(self) -> PointsFilters:
        _check_range(self.created_from, self.created_to)
        return self


class PointsLogQuery(_ListQuery, PointsFilters):
    pass


class AuditListQuery(_ListQuery):
    """Filters of the audit trail (`GET /admin/audit`). Every value is validated:
    a malformed one is a 422, never a silently different query. `target_id`
    only means something together with `target_type` ("5" is a booth, a task, a
    staff account...), so it requires it. `created_from` is inclusive and
    `created_to` exclusive, both timezone-aware."""

    actor_id: int | None = Field(default=None, ge=1, le=2**53)
    action: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$"
    )
    target_type: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"
    )
    target_id: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9:_.-]+$"
    )
    created_from: AwareDatetime | None = None  # inclusive
    created_to: AwareDatetime | None = None  # exclusive

    @model_validator(mode="after")
    def _consistent(self) -> AuditListQuery:
        _check_range(self.created_from, self.created_to)
        if self.target_id is not None and self.target_type is None:
            raise ValueError("target_id needs target_type")
        return self


class SponsorExportRequest(BaseModel):
    """The sponsor contact export takes NO parameters. Which attendees it
    contains (ACTIVE and currently consenting) is decided by the server; a
    body that tries to say otherwise is a 422, not silently different."""

    model_config = ConfigDict(extra="forbid")


class ExportPrepared(BaseModel):
    """Answer to "prepare an export": the one-time download link and where to
    ask what became of it. `rows` is what matched when it was prepared
    (advisory - the download counts again inside its own snapshot and states
    the exact figure in its `X-Export-Rows` header)."""

    download_url: str  # carries the one-time ticket; never log it
    ref: str
    status_url: str
    kind: Literal["attendees", "points-log", "sponsor-contacts"]
    rows: int
    max_rows: int
    expires_in_seconds: int


class ExportStatus(BaseModel):
    """What the server knows about one export's download (see
    admin/export_status.py). Outcomes and counts only - never a row, a filter
    value or the ticket."""

    ref: str
    kind: Literal["attendees", "points-log", "sponsor-contacts"]
    state: Literal[
        "ready", "starting", "streaming", "finished", "interrupted", "failed", "rejected", "expired"
    ]
    code: str | None
    retry: Literal["none", "same_link", "new_export"]
    rows_expected: int | None
    rows_written: int | None


class AttendeeRow(BaseModel):
    """What an administrator sees of an attendee. Deliberately NOT included:
    phone, LinkedIn, profession, interests, card token."""

    id: int
    first_name: str
    last_name: str
    email: str
    status: Literal["PROVISIONAL", "ACTIVE"]
    sponsor_consent: bool
    created_at: AwareDatetime


class AttendeePage(BaseModel):
    items: list[AttendeeRow]
    next_cursor: str | None
    limit: int


class PointsLogRow(BaseModel):
    id: int
    user_id: int
    attendee_name: str
    booth_id: int | None
    booth_name: str | None
    source: Literal["TASK", "FEEDBACK"]
    entry_type: Literal["AWARD", "REVERSAL"]
    points: int
    task_id: int | None
    # The activity's current title; for rows without one, what they are
    # ("Feedback reward", "Booth-level award (before activities)").
    activity: str
    entered_by_staff_id: int | None
    staff_name: str | None
    entered_by_system: bool
    created_at: AwareDatetime
    # The two ends of a reversal. On a REVERSAL: the entry it cancels. On an
    # AWARD: the reversal that cancelled it, or null while it still counts.
    reverses_entry_id: int | None
    reversed_by_entry_id: int | None
    # Why a REVERSAL was made (administrator's text); null for an AWARD.
    reason: str | None


# A value the audit view may show: a plain scalar. Never an object or a list.
AuditValue = str | int | bool | None


class AuditExportResult(BaseModel):
    """What an export event says about the export: outcome and counts. Never a
    row, a filter VALUE or the download ticket."""

    kind: Literal["attendees", "points-log", "sponsor-contacts"] | None
    outcome: Literal["started", "rejected", "stream_finished", "interrupted", "failed"] | None
    reason: str | None
    error_class: str | None
    rows_expected: int | None
    rows_written: int | None
    rows_matched: int | None
    max_rows: int | None
    bytes_written: int | None
    elapsed_ms: int | None
    snapshot_at: AwareDatetime | None
    filters_used: list[str]  # the NAMES of the filters that were set


class AuditEvent(BaseModel):
    """One audit row as an administrator may see it. `before` / `after` hold only
    the allowlisted, plainly typed fields of that kind of event; everything else
    is counted in `hidden_field_count` and never sent (see admin/audit.py).
    `details` is "limited" for an event kind the server has no allowlist for:
    then only the metadata above is shown."""

    id: int
    created_at: AwareDatetime
    actor_id: int
    actor_name: str | None
    action: str
    target_type: str
    target_id: str | None
    # Ties the started / finished / rejected events of ONE export together.
    operation_id: str | None
    before: dict[str, AuditValue] | None
    after: dict[str, AuditValue] | None
    changed_fields: list[str]
    result: AuditExportResult | None
    hidden_field_count: int
    details: Literal["known", "limited"]


class AuditPage(BaseModel):
    items: list[AuditEvent]
    next_cursor: str | None
    limit: int


class PointsLogPage(BaseModel):
    items: list[PointsLogRow]
    next_cursor: str | None
    limit: int
