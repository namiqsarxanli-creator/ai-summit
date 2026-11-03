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

SHORT CODE: under the QR the screen also shows six digits (an HMAC of the same
payload, reduced) for a phone whose camera fails; the attendee types them. One
per panel and phase, revoked by the same rotation; guessing is bounded by the
attendee's scan rate limit (30 per 10 minutes against 1 000 000 values).

SCREEN LINK: the hall computer shows a panel's codes WITHOUT an administrator
session (security review, 3 Nov 2026: a 12-hour admin session left on a venue
PC opened the whole admin console). The link carries `<panel>.<expiry>.<HMAC>`
over the panel id, its `qr_version` and the expiry (the end of the event day):
it reveals only what the big screen shows anyway, stops at midnight after the
event and whenever the panel's codes are rotated.

GRAND FINAL (rules.GRAND_FINAL_CODE): one ENTRY code valid for the row's whole
time, no points of its own; the scan completes Beyond Now Founder (founder.py).

QMETER (2 per panel, at most 4): a Game Master checks the question in Qmeter and
records it for the exactly identified attendee (name AND phone must match one
ACTIVE person; never a name alone), once the panel has ENDED and before the
close. The reason is audited.
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
    GRAND_FINAL_CODE,
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
_SHORT = re.compile(r"[0-9]{6}")


def is_grand_final(panel: Panel) -> bool:
    return panel.code == GRAND_FINAL_CODE


def phases_of(panel: Panel) -> tuple[str, ...]:
    return ("ENTRY",) if is_grand_final(panel) else ("ENTRY", "EXIT")


def short_code(panel: Panel, phase: str) -> str:
    """Six digits for typing the code by hand (see the module docstring)."""
    payload = f"panel-short-v1:{panel.id}:{phase}:{panel.qr_version}"
    digest = hmac.new(
        get_settings().scan_ref_secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{int(digest, 16) % 1_000_000:06d}"


def _read_short(db: Session, digits: str) -> tuple[Panel, str]:
    found = [
        (panel, phase)
        for panel in db.execute(select(Panel)).scalars()
        for phase in phases_of(panel)
        if hmac.compare_digest(short_code(panel, phase), digits)
    ]
    if len(found) != 1:  # none, or (never seen) two codes alike: refuse, never guess
        raise PanelScanError("invalid_code")
    return found[0]


class ScreenLinkError(DomainError):
    code = "SCREEN_LINK_INVALID"
    status_code = 404


_SCREEN_KEY = re.compile(r"([1-9][0-9]{0,15})\.([0-9]{1,12})\.([0-9a-f]{64})")


def _screen_signature(panel: Panel, expires: int) -> str:
    payload = f"panel-screen-v1:{panel.id}:{panel.qr_version}:{expires}"
    return hmac.new(
        get_settings().scan_ref_secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()


def screen_key(panel: Panel, expires: datetime) -> str:
    stamp = int(expires.timestamp())
    return f"{panel.id}.{stamp}.{_screen_signature(panel, stamp)}"


def read_screen_key(db: Session, key: str, now: datetime) -> Panel:
    match = _SCREEN_KEY.fullmatch(key.strip())
    if match is None:
        raise ScreenLinkError("This screen link is not valid.")
    panel = db.get(Panel, int(match.group(1)))
    stamp = int(match.group(2))
    if (
        panel is None
        or not hmac.compare_digest(_screen_signature(panel, stamp), match.group(3))
        or now.timestamp() >= stamp
    ):  # unknown, rotated or expired: one answer, never which
        raise ScreenLinkError("This screen link is not valid.")
    return panel


def read_code(db: Session, code: str) -> tuple[Panel, str]:
    digits = re.sub(r"[\s-]", "", code)
    if _SHORT.fullmatch(digits):
        return _read_short(db, digits)
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
    if is_grand_final(panel):  # one check-in code for the whole time; no exit
        return (
            (panel.starts_at, panel.ends_at) if phase == "ENTRY" else (panel.ends_at, panel.ends_at)
        )
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
    if phase not in phases_of(panel):
        raise PanelScanError("invalid_code")
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
    if is_grand_final(panel):
        from app.modules.competition.founder import recompute

        recompute(db, user.id)  # the check-in may complete Beyond Now Founder
        return {
            "panel_id": panel.id,
            "phase": "entry",
            "recorded": ["entry"],
            "grand_final": True,
            "award": None,
        }
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
    if panel is None or is_grand_final(panel):  # Qmeter questions are about panels
        raise PanelScanError("unknown_panel")
    # PDF "Kim, nəyi, nə vaxt skan edir": Qmeter is checked AFTER the panel, until
    # 17:30 (the ledger refuses after the close). Owner's decision, 25 Oct 2026.
    if now < panel.ends_at:
        raise PanelScanError("panel_not_ended")
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
