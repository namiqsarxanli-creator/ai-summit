"""Mutual QR connections and the CONNECTION points (PDF: "Görüşdə qarşılıqlı QR
skanı: match 3 bal, digər tanışlıq 1 bal; ən yüksək 4 tanışlıq sayılır").

* Each person scans the other's live card QR; a scan of oneself, of a revoked or
  inactive card, or of a blocked person is refused. When both directions exist
  the pair is CONNECTED - once. Its kind is MATCH if either appears in the other's top-five
  recommendations at that moment, OTHER otherwise; whether they share a (non-empty)
  company is recorded at the same moment. Both are frozen there.
* Points: MATCH 3, OTHER 1, same company 0. Only the four best connections count
  (at most 12). A better fifth replaces the weakest counted one: the new one is
  paid and the weakest counted entry gets a REVERSAL (append-only; never edited,
  never "the first four"). Every connection is kept even when it earns nothing.
* The contact action (LinkedIn or e-mail) opens to the connected pair only; a
  public card shows no contact at all to anyone else.

What a scan answers (`outcome`) is what happened to THIS person's points, never
the pair's nominal value: `waiting` (only one side has scanned), `awarded` (paid
now), `already_paid` (paid earlier and still counted), `replaced` (paid earlier,
then replaced by a better connection), `reversed` (paid earlier, cancelled by an
organiser), and the zero-point reasons `same_company`, `ineligible`,
`not_configured` (no competition close is set - never called "closed"),
`closed` (after the close) and `limit_reached` (the best four are better). A
later scan of an existing connection never pays: one that was never paid says
`unpaid` with the reason as far as it is known now; an organiser reviews such
connections (people/reconcile.py). `points` is what this person holds from the
connection now.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, aliased

from app.core.errors import DomainError
from app.modules.attendees.models import User
from app.modules.competition.ledger import (
    award,
    closes_at,
    dedupe_key,
    eligible,
    lock_attendee_points,
)
from app.modules.contact_cards.service import contact_action_for, resolve_active_attendee
from app.modules.people.compatibility import blocked_ids, normalise_company, suggestions
from app.modules.people.models import Connection, ConnectionScan
from app.modules.points.models import PointsEntryType, PointsLog, PointsSource

MATCH_POINTS = 3
OTHER_POINTS = 1
COUNTED = 4


class ConnectionError_(DomainError):  # noqa: N801 - avoid shadowing the builtin
    code = "CONNECTION_REFUSED"
    status_code = 409

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.details = {"reason": reason}


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def connected(db: Session, a: int, b: int) -> bool:
    low, high = _pair(a, b)
    return (
        db.execute(
            select(Connection.id).where(
                Connection.user_low_id == low, Connection.user_high_id == high
            )
        ).first()
        is not None
    )


def _recommended(db: Session, a: User, b: User) -> bool:
    """Use server recommendations in either direction when the pair connects."""
    return any(user.id == b.id for user, _ in suggestions(db, a)) or any(
        user.id == a.id for user, _ in suggestions(db, b)
    )


def value(row: Connection) -> int:
    if row.same_company:
        return 0
    return MATCH_POINTS if row.kind == "MATCH" else OTHER_POINTS


def _counted_entries(db: Session, user_id: int) -> list[PointsLog]:
    reversal = aliased(PointsLog)
    return list(
        db.execute(
            select(PointsLog)
            .outerjoin(reversal, reversal.reverses_points_log_id == PointsLog.id)
            .where(
                PointsLog.user_id == user_id,
                PointsLog.source == PointsSource.CONNECTION,
                PointsLog.entry_type == PointsEntryType.AWARD,
                reversal.id.is_(None),
            )
            .order_by(PointsLog.points, PointsLog.created_at.desc(), PointsLog.id.desc())
        ).scalars()
    )


def unit(row: Connection) -> str:
    return f"pair:{row.user_low_id}:{row.user_high_id}"


def award_entry(db: Session, user_id: int, row: Connection) -> PointsLog | None:
    """The CONNECTION award this person got for this pair, if any (one per pair:
    the ledger's unique `dedupe_key`)."""
    key = dedupe_key(PointsSource.CONNECTION, user_id, unit(row))
    return db.execute(select(PointsLog).where(PointsLog.dedupe_key == key)).scalar_one_or_none()


def held(db: Session, user_id: int, row: Connection) -> tuple[str, int]:
    """What the person holds from an existing connection: (status, points)."""
    entry = award_entry(db, user_id, row)
    if entry is None:
        return "unpaid", 0
    reversal = db.execute(
        select(PointsLog).where(PointsLog.reverses_points_log_id == entry.id)
    ).scalar_one_or_none()
    if reversal is None:
        return "already_paid", entry.points
    # The best-four replacement is the only reversal the system makes itself.
    return ("replaced" if reversal.entered_by_system else "reversed"), 0


def unpaid_reason(db: Session, user_id: int, row: Connection) -> str:
    """Why a connection holds no award for this person, as far as it is known NOW.
    `not_paid` means none of the recorded facts explains it: an organiser reviews
    it (people/reconcile.py); nothing is paid by a later scan."""
    if value(row) == 0:
        return "same_company"
    if not eligible(db.get(User, user_id)):
        return "ineligible"
    close = closes_at(db)
    if close is None:
        return "not_configured"
    if row.established_at >= close:
        return "closed"
    return "not_paid"


def _pay(db: Session, user_id: int, row: Connection, now: datetime) -> str:
    """Count the connection for `user_id` if it belongs in their best four."""
    points = value(row)
    if points == 0:
        return "same_company"
    lock_attendee_points(db, user_id)
    # Checked BEFORE anything is reversed: a refused award must not cost the
    # person a connection they already have counted.
    if not eligible(db.get(User, user_id)):
        return "ineligible"
    close = closes_at(db)
    if close is None:
        return "not_configured"
    if now >= close:
        return "closed"
    counted = _counted_entries(db, user_id)
    if len(counted) >= COUNTED:
        weakest = counted[0]  # lowest value; among equals the most recent
        if weakest.points >= points:
            return "limit_reached"  # kept, not counted: it is not better than the four
        db.add(
            PointsLog(
                user_id=user_id,
                source=PointsSource.CONNECTION,
                entry_type=PointsEntryType.REVERSAL,
                points=-weakest.points,
                reverses_points_log_id=weakest.id,
                entered_by_system=True,
                note="Replaced by a higher-value connection (best four count).",
            )
        )
        db.flush()
    outcome = award(
        db,
        user_id=user_id,
        source=PointsSource.CONNECTION,
        unit=unit(row),
        points=points,
        occurred_at=now,
    ).outcome
    return {"already": "already_paid", "category_full": "limit_reached"}.get(outcome, outcome)


def scan(db: Session, me: User, card_token: str, now: datetime) -> dict[str, Any]:
    other = resolve_active_attendee(db, card_token)  # revoked / inactive -> INVALID_QR
    if other.id == me.id:
        raise ConnectionError_("self")
    if other.id in blocked_ids(db, me.id):
        raise ConnectionError_("blocked")
    if (
        db.execute(
            select(ConnectionScan.id).where(
                ConnectionScan.scanner_id == me.id, ConnectionScan.scanned_id == other.id
            )
        ).first()
        is None
    ):
        db.add(ConnectionScan(scanner_id=me.id, scanned_id=other.id, scanned_at=now))
        db.flush()
    reverse = db.execute(
        select(ConnectionScan.id).where(
            ConnectionScan.scanner_id == other.id, ConnectionScan.scanned_id == me.id
        )
    ).first()
    name = f"{other.first_name} {other.last_name[:1]}."
    if reverse is None:
        return {
            "connected": False,
            "mutual": False,
            "name": name,
            "outcome": "waiting",
            "points": 0,
        }
    low, high = _pair(me.id, other.id)
    row = db.execute(
        select(Connection)
        .where(Connection.user_low_id == low, Connection.user_high_id == high)
        .with_for_update()
    ).scalar_one_or_none()
    outcomes: dict[int, str] = {}
    created = row is None
    if row is None:
        company = normalise_company(me.company)
        row = Connection(
            user_low_id=low,
            user_high_id=high,
            kind="MATCH" if _recommended(db, me, other) else "OTHER",
            same_company=bool(company) and company == normalise_company(other.company),
            established_at=now,
        )
        db.add(row)
        db.flush()
        for uid in sorted((low, high)):  # a stable order: no lock cycle between the two
            outcomes[uid] = _pay(db, uid, row, now)
    status, points = held(db, me.id, row)
    result: dict[str, Any] = {
        "connected": True,
        "mutual": True,
        "name": name,
        "kind": row.kind.lower(),
        "same_company": row.same_company,
        "points": points,  # what this person holds from it now, not its nominal value
        "outcome": outcomes[me.id] if created else status,
    }
    if result["outcome"] == "unpaid":
        result["reason"] = unpaid_reason(db, me.id, row)
    return result


def my_connections(db: Session, me: User) -> list[dict[str, Any]]:
    """Connected people with their contact action - only ever to the pair."""
    rows = db.execute(
        select(Connection, User)
        .join(
            User,
            or_(
                and_(Connection.user_low_id == me.id, User.id == Connection.user_high_id),
                and_(Connection.user_high_id == me.id, User.id == Connection.user_low_id),
            ),
        )
        .order_by(Connection.established_at.desc(), Connection.id.desc())
    ).all()
    out: list[dict[str, Any]] = []
    for row, other in rows:
        status, points = held(db, me.id, row)
        out.append(
            {
                "user_id": other.id,
                "name": f"{other.first_name} {other.last_name}",
                "profession": other.profession,
                "kind": row.kind.lower(),
                # What this person holds from it, not its nominal value.
                "points": points,
                "paid": status == "already_paid",
                "status": status,
                "contact": contact_action_for(
                    linkedin_url=other.linkedin_url, email=str(other.email)
                ).model_dump(),
            }
        )
    return out
