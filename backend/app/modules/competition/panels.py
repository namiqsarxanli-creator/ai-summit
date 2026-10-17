"""Main Stage panels: signed screen QR, attendee scans, PANEL and QMETER awards.

PANEL (8, at most 2 panels): the attendee scans the ENTRY code shown on the main
screen in the first 15 minutes and the EXIT code in the last 10 minutes. Both are
needed - entering late or only at the end earns nothing.

* The screen code is an HMAC over a domain-separated payload naming the panel,
  the PHASE and the panel's `qr_version`. An entry code cannot be used as an exit
  code, nor one panel's code for another, and rotating `qr_version` revokes every
  code shown before. The phase window is checked against the SERVER clock.
* Repeating a scan changes nothing (one row per panel, attendee and phase).
* A person verified elsewhere during the panel (a Playground session they were
  scanned at, or a mentor group whose host scanned them) does not also earn the
  panel (PDF slot table: "Panel balı itir").

QMETER (2 per panel, at most 4): a Game Master checks the question in Qmeter and
records it for the exactly identified attendee (name AND phone must match one
ACTIVE person; never a name alone). The reason is audited.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import DomainError
from app.modules.admin.models import AdminAudit
from app.modules.attendees.models import User, UserStatus
from app.modules.competition.ledger import AwardResult, award
from app.modules.competition.models import Panel, PanelScan
from app.modules.competition.rules import (
    PANEL_ENTRY_MINUTES,
    PANEL_EXIT_MINUTES,
    PANEL_POINTS,
    QMETER_POINTS,
)
from app.modules.competition.scopes import require_scope
from app.modules.mentors.models import MentorBooking, MentorSession
from app.modules.playground.models import PlaygroundBooking, PlaygroundSlot
from app.modules.points.models import PointsSource
from app.modules.staff.models import StaffAccount

AUDIT_QMETER = "competition.qmeter"


class PanelScanError(DomainError):
    code = "PANEL_SCAN_REFUSED"
    status_code = 409

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.details = {"reason": reason}


class AttendeeMatchError(DomainError):
    code = "ATTENDEE_NOT_IDENTIFIED"
    status_code = 404


def _signature(panel: Panel, phase: str) -> str:
    payload = f"panel-qr-v1:{panel.id}:{phase}:{panel.qr_version}"
    return hmac.new(
        get_settings().scan_ref_secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()


def screen_code(panel: Panel, phase: str) -> str:
    return f"P{panel.id}.{phase}.{panel.qr_version}.{_signature(panel, phase)}"


_CODE = re.compile(r"P([1-9][0-9]{0,15})\.(ENTRY|EXIT)\.([1-9][0-9]{0,8})\.([0-9a-f]{64})")


def read_code(db: Session, code: str) -> tuple[Panel, str]:
    match = _CODE.fullmatch(code.strip())
    if match is None:
        raise PanelScanError("invalid_code")
    panel = db.get(Panel, int(match.group(1)))
    phase = match.group(2)
    if panel is None or int(match.group(3)) != panel.qr_version:
        raise PanelScanError("invalid_code")
    if not hmac.compare_digest(_signature(panel, phase), match.group(4)):
        raise PanelScanError("invalid_code")
    return panel, phase


def window(panel: Panel, phase: str) -> tuple[datetime, datetime]:
    if phase == "ENTRY":
        return panel.starts_at, panel.starts_at + timedelta(minutes=PANEL_ENTRY_MINUTES)
    return panel.ends_at - timedelta(minutes=PANEL_EXIT_MINUTES), panel.ends_at


def _verified_elsewhere(db: Session, user_id: int, panel: Panel) -> bool:
    """A Playground session or mentor group the person was verified at, overlapping
    the panel."""
    mentor = db.execute(
        select(MentorBooking.id)
        .join(MentorSession, MentorSession.id == MentorBooking.session_id)
        .where(
            MentorBooking.user_id == user_id,
            MentorBooking.status == "ATTENDED",
            MentorSession.starts_at < panel.ends_at,
            MentorSession.ends_at > panel.starts_at,
        )
        .limit(1)
    ).first()
    return mentor is not None or (
        db.execute(
            select(PlaygroundBooking.id)
            .join(PlaygroundSlot, PlaygroundSlot.id == PlaygroundBooking.slot_id)
            .where(
                PlaygroundBooking.user_id == user_id,
                PlaygroundBooking.status.in_(("CHECKED_IN", "COMPLETED")),
                PlaygroundSlot.starts_at < panel.ends_at,
                PlaygroundSlot.ends_at > panel.starts_at,
            )
            .limit(1)
        ).first()
        is not None
    )


def scan(db: Session, user: User, code: str, now: datetime) -> dict[str, Any]:
    panel, phase = read_code(db, code)
    opens, closes = window(panel, phase)
    already = db.execute(
        select(PanelScan).where(
            PanelScan.panel_id == panel.id,
            PanelScan.user_id == user.id,
            PanelScan.phase == phase,
        )
    ).scalar_one_or_none()
    if already is None:
        if not opens <= now < closes:
            raise PanelScanError("outside_window")
        db.add(PanelScan(panel_id=panel.id, user_id=user.id, phase=phase, scanned_at=now))
        db.flush()
    phases = set(
        db.execute(
            select(PanelScan.phase).where(
                PanelScan.panel_id == panel.id, PanelScan.user_id == user.id
            )
        ).scalars()
    )
    result: AwardResult | None = None
    if phases == {"ENTRY", "EXIT"}:
        if _verified_elsewhere(db, user.id, panel):
            result = AwardResult("not_scored", 0)
        else:
            result = award(
                db,
                user_id=user.id,
                source=PointsSource.PANEL,
                unit=f"panel:{panel.id}",
                points=PANEL_POINTS,
                occurred_at=now,
            )
    return {
        "panel_id": panel.id,
        "phase": phase.lower(),
        "recorded": sorted(p.lower() for p in phases),
        "award": None if result is None else {"outcome": result.outcome, "points": result.points},
    }


class Timed(Protocol):
    starts_at: datetime
    ends_at: datetime


def reverse_overlapping_panel_awards(
    db: Session,
    user_id: int,
    slot: Timed,
    note: str = "Verified at a Playground session during this panel.",
) -> int:
    """Participation elsewhere (a Playground session, a mentor group) verified
    AFTER a panel at the same time was already paid: the panel award is corrected
    by a REVERSAL entry (never edited). Returns how many."""
    from app.modules.points.models import PointsEntryType, PointsLog

    panels = db.execute(
        select(Panel.id).where(Panel.starts_at < slot.ends_at, Panel.ends_at > slot.starts_at)
    ).scalars()
    reversed_count = 0
    for panel_id in panels:
        entry = db.execute(
            select(PointsLog).where(
                PointsLog.dedupe_key == f"COMP:PANEL:{user_id}:panel:{panel_id}"
            )
        ).scalar_one_or_none()
        if entry is None:
            continue
        exists = db.execute(
            select(PointsLog.id).where(PointsLog.reverses_points_log_id == entry.id)
        ).first()
        if exists is not None:
            continue
        db.add(
            PointsLog(
                user_id=user_id,
                booth_id=entry.booth_id,
                source=entry.source,
                entry_type=PointsEntryType.REVERSAL,
                points=-entry.points,
                reverses_points_log_id=entry.id,
                entered_by_system=True,
                note=note,
            )
        )
        reversed_count += 1
    db.flush()
    if reversed_count:
        from app.modules.competition.founder import recompute

        recompute(db, user_id)  # the panel may have been the only Main Stage evidence
    return reversed_count


# --- Qmeter (Game Master) ------------------------------------------------------------


def normalise_phone(value: str) -> str:
    return re.sub(r"\D", "", value)


def identify(db: Session, staff: StaffAccount, full_name: str, phone: str) -> User:
    """Exactly one ACTIVE attendee whose phone (digits only) AND name match."""
    require_scope(db, staff, "GAME_MASTER", None)
    digits = normalise_phone(phone)
    if len(digits) < 7:
        raise AttendeeMatchError("Enter the full phone number.")
    words = {w.casefold() for w in full_name.split() if w}
    candidates = [
        u
        for u in db.execute(
            select(User).where(
                User.status == UserStatus.ACTIVE,
                func.regexp_replace(User.phone, r"\D", "", "g") == digits,
            )
        ).scalars()
        if words and words <= {w.casefold() for w in f"{u.first_name} {u.last_name}".split()}
    ]
    if len(candidates) != 1:
        raise AttendeeMatchError("No single attendee matches that name and phone.")
    return candidates[0]


def masked(phone: str) -> str:
    digits = normalise_phone(phone)
    return f"•••{digits[-4:]}" if len(digits) >= 4 else "•••"


def record_qmeter(
    db: Session, staff: StaffAccount, *, user_id: int, panel_id: int, reason: str, now: datetime
) -> dict[str, Any]:
    require_scope(db, staff, "GAME_MASTER", None)
    panel = db.get(Panel, panel_id)
    if panel is None:
        raise PanelScanError("unknown_panel")
    if now < panel.starts_at:
        raise PanelScanError("panel_not_started")
    user = db.get(User, user_id)
    if user is None or user.status != UserStatus.ACTIVE:
        raise AttendeeMatchError("No such attendee.")
    result = award(
        db,
        user_id=user_id,
        source=PointsSource.QMETER,
        unit=f"panel:{panel_id}",
        points=QMETER_POINTS,
        occurred_at=now,
        staff_id=staff.id,
        note=reason,
    )
    if result.paid:
        db.add(
            AdminAudit(
                staff_account_id=staff.id,
                action=AUDIT_QMETER,
                target_type="points_log",
                target_id=str(result.entry_id),
                before_value=None,
                after_value={
                    "id": result.entry_id,
                    "user_id": user_id,
                    "panel_id": panel_id,
                    "points": result.points,
                    "reason": reason,
                },
            )
        )
        db.flush()
    return {"outcome": result.outcome, "points": result.points}
