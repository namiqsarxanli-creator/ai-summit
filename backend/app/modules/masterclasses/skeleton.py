"""The confirmed frame of the Beyond Now programme - and nothing more.

`create_program_skeleton` adds the event (13 November 2026, The Ritz-Carlton
Baku, Asia/Baku), its two blocks (11:30-13:00 with changes until 11:00,
16:00-17:30 with changes until 15:30) and five rooms. It adds NO masterclass:
the titles and speakers in the planning documents are proposals, so an
administrator enters the masterclasses once they are confirmed. Re-running it
adds only what is missing and never changes an existing row.

Run by an operator (`python -m app.cli masterclass-skeleton`), never by a
migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.masterclasses.models import MasterclassBlock, MasterclassEvent, MasterclassRoom

EVENT_CODE: Final = "ai-summit-2026"
TIMEZONE: Final = "Asia/Baku"
EVENT_DATE: Final = date(2026, 11, 13)
ROOM_NAMES: Final = ("Zal 1", "Zal 2", "Zal 3", "Zal 4", "Zal 5")
# (code, title, start, end, change deadline) in the event's local time.
BLOCKS: Final = (
    ("b1", "Blok 1", time(11, 30), time(13, 0), time(11, 0)),
    ("b2", "Blok 2", time(16, 0), time(17, 30), time(15, 30)),
)


@dataclass(frozen=True)
class SkeletonReport:
    event_created: bool
    blocks_created: int
    rooms_created: int


def _at(t: time) -> datetime:
    return datetime.combine(EVENT_DATE, t, tzinfo=ZoneInfo(TIMEZONE))


def create_program_skeleton(db: Session) -> SkeletonReport:
    event = db.execute(
        select(MasterclassEvent).where(MasterclassEvent.code == EVENT_CODE)
    ).scalar_one_or_none()
    created = event is None
    if event is None:
        event = MasterclassEvent(
            code=EVENT_CODE,
            title="AI Summit 2026 · Beyond Now",
            venue="The Ritz-Carlton Baku",
            timezone=TIMEZONE,
            event_date=EVENT_DATE,
        )
        db.add(event)
        db.flush()

    have_blocks = set(
        db.execute(
            select(MasterclassBlock.code).where(MasterclassBlock.event_id == event.id)
        ).scalars()
    )
    blocks = 0
    for order, (code, title, start, end, deadline) in enumerate(BLOCKS, start=1):
        if code in have_blocks:
            continue
        db.add(
            MasterclassBlock(
                event_id=event.id,
                code=code,
                title=title,
                starts_at=_at(start),
                ends_at=_at(end),
                change_deadline=_at(deadline),
                sort_order=order,
                display_number=order,
            )
        )
        blocks += 1

    have_rooms = set(
        db.execute(
            select(MasterclassRoom.name).where(MasterclassRoom.event_id == event.id)
        ).scalars()
    )
    rooms = 0
    for order, name in enumerate(ROOM_NAMES, start=1):
        if name not in have_rooms:
            db.add(
                MasterclassRoom(
                    event_id=event.id, name=name, sort_order=order, display_number=order
                )
            )
            rooms += 1
    db.flush()
    return SkeletonReport(event_created=created, blocks_created=blocks, rooms_created=rooms)
