"""API shapes of the masterclass programme and reservations.

The programme is public and anonymous: titles, rooms, times, speakers and seat
COUNTS only - never who holds a seat. The attendee's own choices come from a
separate, session-bound endpoint. Every mutating schema forbids extra fields.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.masterclasses.models import MAX_CAPACITY, MAX_WAITLIST

Availability = Literal["available", "waitlist", "full"]
ChoiceStatus = Literal["confirmed", "waitlisted"]
Outcome = Literal["confirmed", "waitlisted", "unchanged", "cancelled"]


# --- the public programme ----------------------------------------------------------


class MasterclassOut(BaseModel):
    id: int
    title: str
    description: str | None
    speaker: str | None
    room: str | None
    room_display_number: int | None = None
    capacity: int
    waitlist_capacity: int
    confirmed_count: int
    waitlist_count: int
    seats_left: int
    # available: a seat can be taken now; waitlist: no seat, the queue has room;
    # full: no seat and no queue place.
    availability: Availability


class BlockOut(BaseModel):
    display_number: int | None = None
    id: int
    code: str
    title: str
    starts_at: datetime
    ends_at: datetime
    change_deadline: datetime
    # Decided by the server clock, so a wrong phone clock changes nothing.
    changes_open: bool
    masterclasses: list[MasterclassOut]


class UnscheduledOut(BaseModel):
    """Published, but its block/room is not decided: shown, not bookable."""

    id: int
    title: str
    description: str | None
    speaker: str | None


class EventOut(BaseModel):
    code: str
    title: str
    venue: str | None
    timezone: str
    event_date: date
    blocks: list[BlockOut]
    unscheduled: list[UnscheduledOut]


class ProgramOut(BaseModel):
    server_time: datetime
    events: list[EventOut]


# --- the attendee's own choices ----------------------------------------------------


class ChoiceOut(BaseModel):
    block_id: int
    masterclass_id: int
    status: ChoiceStatus
    # 1 = next in line. None when confirmed.
    waitlist_position: int | None
    change_deadline: datetime
    changes_open: bool


class MyChoicesOut(BaseModel):
    server_time: datetime
    choices: list[ChoiceOut]
    revisions: dict[int, str]


class ChoiceRequest(BaseModel):
    """Set the attendee's choice for one block (PUT: sending it again changes
    nothing). `join_waitlist` must be true to accept a waitlist place when the
    seats are gone; `give_up_confirmed_seat` must ALSO be true when that would
    release a seat the attendee already holds in this block."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: str = Field(
        pattern=r"^(0|[1-9][0-9]*:(CONFIRMED|WAITLISTED|CANCELLED))$", max_length=80
    )
    masterclass_id: int = Field(ge=1, le=2**53)
    join_waitlist: bool = False
    give_up_confirmed_seat: bool = False


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str = Field(
        pattern=r"^(0|[1-9][0-9]*:(CONFIRMED|WAITLISTED|CANCELLED))$", max_length=80
    )


class ChoiceResult(BaseModel):
    outcome: Outcome
    choices: MyChoicesOut


# --- administration ----------------------------------------------------------------


def _zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as err:
        raise ValueError("unknown time zone") from err
    return value


_CODE = r"^[a-z0-9][a-z0-9-]{0,63}$"


class EventWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=_CODE)
    title: str = Field(min_length=1, max_length=200)
    venue: str | None = Field(default=None, max_length=200)
    timezone: str = Field(min_length=1, max_length=64)
    event_date: date

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        return _zone(v)


class EventEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    venue: str | None = Field(default=None, max_length=200)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    event_date: date | None = None
    # Migration 0020: the competition close (17:30 Asia/Baku); with an offset.
    competition_closes_at: datetime | None = None

    @field_validator("competition_closes_at")
    @classmethod
    def _close_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None:
            _require_offset(v)
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        return None if v is None else _zone(v)


def _require_offset(v: datetime) -> None:
    if v.tzinfo is None:
        raise ValueError("a time zone offset is required")


class BlockWrite(BaseModel):
    display_number: int | None = Field(default=None, ge=1, le=10000)
    model_config = ConfigDict(extra="forbid")

    event_id: int = Field(ge=1, le=2**53)
    code: str = Field(pattern=_CODE)
    title: str = Field(min_length=1, max_length=200)
    starts_at: datetime
    ends_at: datetime
    change_deadline: datetime
    sort_order: int = Field(default=0, ge=0, le=10_000)

    @field_validator("starts_at", "ends_at", "change_deadline")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        _require_offset(v)
        return v

    @model_validator(mode="after")
    def _order(self) -> BlockWrite:
        if self.ends_at <= self.starts_at:
            raise ValueError("the block must end after it starts")
        if self.change_deadline > self.starts_at:
            raise ValueError("the change deadline must not be after the start")
        return self


class BlockEdit(BaseModel):
    display_number: int | None = Field(default=None, ge=1, le=10000)
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    change_deadline: datetime | None = None
    sort_order: int | None = Field(default=None, ge=0, le=10_000)

    @field_validator("starts_at", "ends_at", "change_deadline")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None:
            _require_offset(v)
        return v


class RoomWrite(BaseModel):
    display_number: int | None = Field(default=None, ge=1, le=10000)
    model_config = ConfigDict(extra="forbid")

    event_id: int = Field(ge=1, le=2**53)
    name: str = Field(min_length=1, max_length=100)
    sort_order: int = Field(default=0, ge=0, le=10_000)


class RoomEdit(BaseModel):
    display_number: int | None = Field(default=None, ge=1, le=10000)
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    sort_order: int | None = Field(default=None, ge=0, le=10_000)


class MasterclassWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: int = Field(ge=1, le=2**53)
    block_id: int | None = Field(default=None, ge=1, le=2**53)
    room_id: int | None = Field(default=None, ge=1, le=2**53)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    speaker: str | None = Field(default=None, min_length=1, max_length=300)
    capacity: int = Field(default=40, ge=1, le=MAX_CAPACITY)
    waitlist_capacity: int = Field(default=10, ge=0, le=MAX_WAITLIST)
    is_published: bool = False
    sort_order: int = Field(default=0, ge=0, le=10_000)


class MasterclassEdit(BaseModel):
    """Only the fields sent are changed. `block_id` / `room_id` may be sent as
    null to unschedule / clear the room."""

    model_config = ConfigDict(extra="forbid")

    block_id: int | None = Field(default=None, ge=1, le=2**53)
    room_id: int | None = Field(default=None, ge=1, le=2**53)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    speaker: str | None = Field(default=None, min_length=1, max_length=300)
    capacity: int | None = Field(default=None, ge=1, le=MAX_CAPACITY)
    waitlist_capacity: int | None = Field(default=None, ge=0, le=MAX_WAITLIST)
    # Migration 0020: standing places a door volunteer may fill once seats are
    # gone; 0 = none. Bounded like the waitlist, never "unlimited".
    standing_capacity: int | None = Field(default=None, ge=0, le=MAX_WAITLIST)
    is_published: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=10_000)
