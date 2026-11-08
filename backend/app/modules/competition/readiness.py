"""Is the event configured for the Beyond Now day? A READ-ONLY checklist.

Printed by `python -m app.cli competition-report` after the activation report
(competition/activation.py); it reads, counts and compares, and never creates or
changes anything. It prints no secret, no setting value other than on/off, and
no personal data: counts, times in the event's time zone, and the ids of the
rows an operator has to fix - never a name, an email address or a phone number.

What "ready" means comes from the organiser's documents (PDF) and the decisions
recorded in BEYOND_NOW.md: one event on its date in Asia/Baku, two masterclass
blocks in five rooms with published masterclasses, two Main Stage panels, the
17:30 close, the competition ranking on, AI Playground booths with 36 slots
each, mentor sessions of at most six, Match Corner tables, and the staff scopes
that let every door, mentor group and booth be scanned.

The final 13 Nov agenda (8 Nov) has Panel 1 at 10:30-11:30 and neither a
Meet the Mentor slot nor room for a Match Corner: with no mentor session or no
active table that activity is reported as "info" (not used), not as missing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.modules.admin.models import EventConfig
from app.modules.attendees.models import User, UserStatus
from app.modules.competition.ledger import closes_at
from app.modules.competition.models import Panel, StaffScope
from app.modules.competition.rules import GRAND_FINAL_CODE
from app.modules.masterclasses.models import (
    Masterclass,
    MasterclassBlock,
    MasterclassEvent,
    MasterclassRoom,
)
from app.modules.mentors.models import MentorSession
from app.modules.people.models import MatchTable
from app.modules.playground.models import PlaygroundSlot
from app.modules.staff.models import StaffAccount, StaffRole

EVENT_DATE = date(2026, 11, 13)
TIMEZONE = "Asia/Baku"
CLOSE_LOCAL = time(17, 30)
BLOCKS = 2
ROOMS = 5
PANELS = 2
PLAYGROUND_SLOTS = 36
MENTOR_GROUP_MAX = 6

Status = Literal["ok", "missing", "info"]


@dataclass(frozen=True)
class Check:
    key: str
    status: Status  # "info": shown for the operator, not a condition of readiness
    detail: str


def _local(at: datetime, zone: str) -> str:
    return at.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M")


def _ids(ids: list[int]) -> str:
    return ", ".join(str(i) for i in sorted(ids)[:20]) + (" ..." if len(ids) > 20 else "")


def _live_targets(db: Session, scope: str) -> set[int]:
    """Targets with at least one live grant held by an ACTIVE staff account."""
    return {
        int(t)
        for t in db.execute(
            select(StaffScope.target_id)
            .join(StaffAccount, StaffAccount.id == StaffScope.staff_account_id)
            .where(
                StaffScope.scope == scope,
                StaffScope.revoked_at.is_(None),
                StaffScope.target_id.is_not(None),
                StaffAccount.is_active.is_(True),
            )
        ).scalars()
        if t is not None
    }


def _mentor_checks(
    add: Callable[[str, bool, str], None], db: Session, sessions: list[MentorSession], zone: str
) -> None:
    oversized = [s.id for s in sessions if s.capacity > MENTOR_GROUP_MAX]
    add(
        "mentor_sessions",
        bool(sessions) and not oversized,
        f"{len(sessions)} session(s)"
        + "".join(f"; {_local(s.starts_at, zone)[-5:]}" for s in sessions)
        + (f"; over {MENTOR_GROUP_MAX} seats: session {_ids(oversized)}" if oversized else ""),
    )
    open_sessions = [s for s in sessions if s.is_open]
    mentor_times = {s.starts_at.astimezone(ZoneInfo(zone)).time() for s in open_sessions}
    expected_mentors = {time(13, 10), time(13, 35), time(15, 35)}
    add(
        "mentor_schedule",
        expected_mentors <= mentor_times
        and all(
            s.starts_at.astimezone(ZoneInfo(zone)).date() == EVENT_DATE
            and s.ends_at - s.starts_at == timedelta(minutes=20)
            and s.capacity == MENTOR_GROUP_MAX
            for s in open_sessions
        ),
        "expected open 20-minute groups of 6 at 13:10, 13:35 and 15:35 on the event day",
    )
    hosted = _live_targets(db, "MENTOR_HOST")
    unhosted = [s.id for s in sessions if s.id not in hosted]
    add(
        "mentor_hosts",
        bool(sessions) and not unhosted,
        "every session has a host"
        if sessions and not unhosted
        else f"no host: session {_ids(unhosted)}"
        if sessions
        else "no sessions",
    )


def check(db: Session) -> list[Check]:
    out: list[Check] = []

    def add(key: str, ok: bool, detail: str) -> None:
        out.append(Check(key, "ok" if ok else "missing", detail))

    event = db.execute(select(MasterclassEvent).order_by(MasterclassEvent.id).limit(1)).scalar()
    if event is None:
        add("event", False, "no event: run `python -m app.cli masterclass-skeleton`")
        zone = TIMEZONE
    else:
        zone = event.timezone
        add(
            "event",
            event.timezone == TIMEZONE and event.event_date == EVENT_DATE,
            f"date {event.event_date.isoformat()}, time zone {event.timezone}"
            + ("" if event.timezone == TIMEZONE else f" (expected {TIMEZONE})")
            + ("" if event.event_date == EVENT_DATE else f" (expected date {EVENT_DATE})"),
        )

    blocks = list(
        db.execute(select(MasterclassBlock).order_by(MasterclassBlock.starts_at)).scalars()
    )
    add(
        "masterclass_blocks",
        len(blocks) == BLOCKS,
        f"{len(blocks)} of {BLOCKS}: "
        + "; ".join(f"{_local(b.starts_at, zone)}-{_local(b.ends_at, zone)[-5:]}" for b in blocks),
    )
    rooms = int(db.execute(select(func.count()).select_from(MasterclassRoom)).scalar_one())
    # The 13 Nov agenda (SIZ2026) has 5 halls in block 1 and 6 in block 2.
    add("masterclass_rooms", rooms >= ROOMS, f"{rooms} (at least {ROOMS})")
    published = {
        block_id: int(n)
        for block_id, n in db.execute(
            select(Masterclass.block_id, func.count())
            .where(Masterclass.is_published.is_(True), Masterclass.block_id.is_not(None))
            .group_by(Masterclass.block_id)
        ).all()
        if block_id is not None
    }
    unpublished = int(
        db.execute(
            select(func.count()).select_from(Masterclass).where(Masterclass.is_published.is_(False))
        ).scalar_one()
    )
    add(
        "masterclasses_published",
        bool(blocks) and all(published.get(b.id, 0) >= ROOMS for b in blocks),
        "; ".join(
            f"block {b.id}: {published.get(b.id, 0)} published (at least {ROOMS})" for b in blocks
        )
        + f"; {unpublished} not published"
        if blocks
        else "no blocks",
    )

    every = list(db.execute(select(Panel).order_by(Panel.starts_at)).scalars())
    panels = [p for p in every if p.code != GRAND_FINAL_CODE]
    final_rows = [p for p in every if p.code == GRAND_FINAL_CODE]
    add(
        "grand_final",
        len(final_rows) == 1
        and final_rows[0].starts_at.astimezone(ZoneInfo(zone)).date() == EVENT_DATE,
        "; ".join(f"{_local(p.starts_at, zone)}-{_local(p.ends_at, zone)[-5:]}" for p in final_rows)
        or "no Grand Final check-in: Beyond Now Founder cannot be earned",
    )
    add(
        "panels",
        len(panels) == PANELS,
        f"{len(panels)} of {PANELS}"
        + "".join(f"; {_local(p.starts_at, zone)}-{_local(p.ends_at, zone)[-5:]}" for p in panels),
    )

    close = closes_at(db)
    if close is None:
        add("competition_close", False, "NOT SET - nothing can be earned")
    else:
        local = close.astimezone(ZoneInfo(zone))
        on_time = local.time() == CLOSE_LOCAL and (
            event is None or local.date() == event.event_date
        )
        add(
            "competition_close",
            on_time,
            _local(close, zone) + ("" if on_time else " (expected 17:30 on the event date)"),
        )

    expected_blocks = {(time(11, 30), time(13), time(11)), (time(16), time(17, 30), time(15, 30))}
    actual_blocks = {
        (
            b.starts_at.astimezone(ZoneInfo(zone)).time(),
            b.ends_at.astimezone(ZoneInfo(zone)).time(),
            b.change_deadline.astimezone(ZoneInfo(zone)).time(),
        )
        for b in blocks
    }
    add(
        "masterclass_schedule",
        actual_blocks == expected_blocks
        and all(
            b.starts_at.astimezone(ZoneInfo(zone)).date() == EVENT_DATE
            and b.ends_at.astimezone(ZoneInfo(zone)).date() == EVENT_DATE
            for b in blocks
        ),
        "expected event-day blocks 11:30-13:00 / 16:00-17:30, deadlines 11:00 / 15:30",
    )
    expected_panels = {(time(10, 30), time(11, 30)), (time(14), time(15, 30))}
    actual_panels = {
        (p.starts_at.astimezone(ZoneInfo(zone)).time(), p.ends_at.astimezone(ZoneInfo(zone)).time())
        for p in panels
    }
    add(
        "panel_schedule",
        actual_panels == expected_panels
        and all(
            p.starts_at.astimezone(ZoneInfo(zone)).date() == EVENT_DATE
            and p.ends_at.astimezone(ZoneInfo(zone)).date() == EVENT_DATE
            for p in panels
        ),
        "expected event-day panels 10:30-11:30 / 14:00-15:30",
    )

    mode = bool(
        db.execute(select(EventConfig.competition_mode).where(EventConfig.id == 1)).scalar()
    )
    add("competition_mode", mode, "on" if mode else "off - the competition ranking is not shown")

    slots = {
        int(booth_id): int(n)
        for booth_id, n in db.execute(
            select(PlaygroundSlot.booth_id, func.count()).group_by(PlaygroundSlot.booth_id)
        ).all()
    }
    short = [b for b, n in slots.items() if n != PLAYGROUND_SLOTS]
    add(
        "playground_slots",
        8 <= len(slots) <= 15 and not short,
        f"{len(slots)} booth(s) with slots (expected 8-15)"
        + (f"; not {PLAYGROUND_SLOTS} slots: booth {_ids(short)}" if short else "")
        if slots
        else "no Playground slots prepared",
    )
    slot_rows = list(db.execute(select(PlaygroundSlot)).scalars())
    invalid_slots = [
        s.id
        for s in slot_rows
        if not (
            1 <= s.number <= PLAYGROUND_SLOTS
            and s.starts_at.astimezone(ZoneInfo(zone)).date() == EVENT_DATE
            and s.starts_at.astimezone(ZoneInfo(zone)).time()
            == (
                datetime.combine(EVENT_DATE, time(9)) + timedelta(minutes=15 * (s.number - 1))
            ).time()
            and s.ends_at - s.starts_at == timedelta(minutes=15)
        )
    ]
    add(
        "playground_schedule",
        bool(slot_rows) and not invalid_slots,
        f"invalid slot ids: {_ids(invalid_slots)}"
        if invalid_slots
        else "expected event-day slots P01-P36, 09:00-18:00, 15 minutes each",
    )
    staffed = {
        int(b)
        for b in db.execute(
            select(StaffAccount.booth_id).where(
                StaffAccount.role == StaffRole.BOOTH_STAFF,
                StaffAccount.is_active.is_(True),
                StaffAccount.booth_id.is_not(None),
            )
        ).scalars()
        if b is not None
    }
    # FACEMARK's scanners (a live door or Game Master scope) work every booth.
    scanners = db.execute(
        select(StaffScope.id)
        .join(StaffAccount, StaffAccount.id == StaffScope.staff_account_id)
        .where(
            StaffScope.scope.in_(("MASTERCLASS_DOOR", "GAME_MASTER")),
            StaffScope.revoked_at.is_(None),
            StaffAccount.is_active.is_(True),
        )
        .limit(1)
    ).first()
    unstaffed = [] if scanners else [b for b in slots if b not in staffed]
    add(
        "playground_booth_staff",
        bool(slots) and not unstaffed,
        "every Playground booth has active booth staff"
        if slots and not unstaffed
        else (
            f"no active booth staff: booth {_ids(unstaffed)}" if slots else "no Playground booths"
        ),
    )

    sessions = list(db.execute(select(MentorSession).order_by(MentorSession.starts_at)).scalars())
    if not sessions:
        out.append(Check("mentor_sessions", "info", "no mentor groups: not in the programme"))
    else:
        _mentor_checks(add, db, sessions, zone)

    tables = int(
        db.execute(
            select(func.count()).select_from(MatchTable).where(MatchTable.is_active.is_(True))
        ).scalar_one()
    )
    if tables == 0:
        out.append(
            Check(
                "match_corner_tables",
                "info",
                "no active table: Match Corner off; accepted requests meet at the Connection Hub",
            )
        )
    else:
        add("match_corner_tables", 4 <= tables <= 6, f"{tables} active table(s) (expected 4-6)")

    published_ids = list(
        db.execute(select(Masterclass.id).where(Masterclass.is_published.is_(True))).scalars()
    )
    doors = _live_targets(db, "MASTERCLASS_DOOR")
    no_door = [m for m in published_ids if m not in doors]
    add(
        "masterclass_door_scopes",
        bool(published_ids) and not no_door,
        "every published masterclass has a door volunteer"
        if published_ids and not no_door
        else (
            f"no door volunteer: masterclass {_ids(no_door)}"
            if published_ids
            else "no published masterclasses"
        ),
    )
    game_masters = int(
        db.execute(
            select(func.count(func.distinct(StaffScope.staff_account_id)))
            .join(StaffAccount, StaffAccount.id == StaffScope.staff_account_id)
            .where(
                StaffScope.scope == "GAME_MASTER",
                StaffScope.revoked_at.is_(None),
                StaffAccount.is_active.is_(True),
            )
        ).scalar_one()
    )
    add("game_masters", game_masters > 0, f"{game_masters} active Game Master(s)")

    excluded = int(
        db.execute(
            select(func.count())
            .select_from(User)
            .where(User.status == UserStatus.ACTIVE, User.competition_excluded_reason.is_not(None))
        ).scalar_one()
    )
    out.append(
        Check(
            "competition_exclusions",
            "info",
            f"{excluded} active person(s) excluded (team, volunteers, sponsors, speakers) - "
            "check the list in the admin panel",
        )
    )
    settings = get_settings()
    out.append(
        Check(
            "founder_rule",
            "info",
            "confirmed" if settings.founder_rule_confirmed else "NOT confirmed - no Founder bonus",
        )
    )
    reminders = bool(
        db.execute(select(EventConfig.reminders_enabled).where(EventConfig.id == 1)).scalar()
    )
    out.append(
        Check(
            "reminders",
            "info",
            "reminder emails on" if reminders else "reminder emails off",
        )
    )
    return out


def ready(checks: list[Check]) -> bool:
    return all(c.status != "missing" for c in checks)
