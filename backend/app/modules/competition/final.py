"""17:30 close, review and the 17:40 Grand Final (migration 0023).

* Before the close nobody can take a final snapshot (`competition_open`).
* A Game Master (GAME_MASTER scope) or an administrator takes a DRAFT: the
  complete competition ranking as it stands (the same ranking as the live board:
  points, then who reached the total earlier, then the stable user id), the
  Founder / Lucky Draw eligible list and a ledger watermark, frozen as data.
* They review it and PUBLISH it explicitly; publishing a later version
  supersedes the earlier one in the same transaction. A DRAFT can be discarded.
* Publication is refused while it would be known to be wrong: when an offline
  scan still waits for review (`offline_reviews_pending`), or when the ranking or
  the Founder list as computed NOW differs from the draft (`stale_snapshot`).
  Taking a snapshot and publishing one both hold SHARE locks on users,
  offline_scans and points_log (in that order, the order every writer already
  takes them in), so no award, correction or offline submission/review can
  commit between the check and the decision, and the snapshot's ranking and
  Founder list come from one consistent state. There is no override.
* Until a version is published after the close, the public status is
  `pending_review`. A ledger change after a snapshot (a verified offline scan, a
  correction) never edits it: the review screen shows that the ledger moved on,
  and only a NEW published version changes the result.
* Everything public is privacy-safe: first name and surname initial, and the
  attendee's own photo (or the robot), never contact data. The Lucky Draw list
  is an eligibility list; no draw algorithm or prize mechanic is invented.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any, Final

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, aliased

from app.core.errors import DomainError, StaffNotAuthorizedError
from app.core.images import SMALL
from app.core.media import avatar_url
from app.core.names import short_name
from app.modules.admin.models import AdminAudit
from app.modules.attendees.models import User
from app.modules.competition.final_models import FinalResult
from app.modules.competition.founder import live_founder, rule_confirmed
from app.modules.competition.ledger import closes_at, eligible
from app.modules.competition.scopes import has_scope
from app.modules.masterclasses.models import MasterclassEvent
from app.modules.points.models import PointsEntryType, PointsLog, PointsSource
from app.modules.staff.models import StaffAccount, StaffRole

ON_SCREEN: Final = 10
AUDIT_CREATE = "final.create"
AUDIT_PUBLISH = "final.publish"
AUDIT_DISCARD = "final.discard"
AUDIT_EXPORT = "final.export"


class FinalError(DomainError):
    code = "FINAL_REFUSED"
    status_code = 409

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.details = {"reason": reason}


class FinalMissing(DomainError):
    code = "FINAL_NOT_FOUND"
    status_code = 404


# The one public name format (app/core/names.py), under its older name here.
display_name = short_name


def require_reviewer(db: Session, staff: StaffAccount) -> None:
    if staff.role != StaffRole.ADMIN and not has_scope(db, staff, "GAME_MASTER", None):
        raise StaffNotAuthorizedError("Only a Game Master or an administrator can do this.")


def _event(db: Session) -> MasterclassEvent:
    event = db.execute(select(MasterclassEvent).order_by(MasterclassEvent.id)).scalars().first()
    if event is None:
        raise FinalError("not_configured")
    return event


def _watermark(db: Session) -> int:
    return int(db.execute(select(func.coalesce(func.max(PointsLog.id), 0))).scalar_one())


def build_payload(db: Session) -> dict[str, Any]:
    from app.modules.leaderboard.service import _ranked

    ranked = _ranked(competition=True)
    rows = db.execute(select(ranked).order_by(ranked.c.rn)).all()
    ranking = [
        {
            "place": int(r.rn),
            "user_id": int(r.user_id),
            "name": display_name(r.first_name, r.last_name),
            "photo_key": r.photo_key,
            "points": int(r.total_points),
            "reached_at": r.reached_at.isoformat() if r.reached_at else None,
        }
        for r in rows
    ]
    # The ranking already holds only ACTIVE, competing people (competition mode);
    # one query finds which of them hold a live Founder bonus.
    reversal = aliased(PointsLog)
    founder_at = dict(
        db.execute(
            select(PointsLog.user_id, func.coalesce(PointsLog.occurred_at, PointsLog.created_at))
            .outerjoin(reversal, reversal.reverses_points_log_id == PointsLog.id)
            .where(
                PointsLog.source == PointsSource.FOUNDER,
                PointsLog.entry_type == PointsEntryType.AWARD,
                reversal.id.is_(None),
            )
        ).all()
    )
    confirmed = rule_confirmed()
    founders = [
        {
            "user_id": e["user_id"],
            "name": e["name"],
            "photo_key": e["photo_key"],
            "founder_at": founder_at[e["user_id"]].isoformat(),
        }
        for e in ranking
        if confirmed and e["user_id"] in founder_at
    ]
    return {
        "ranking": ranking,
        "founders": founders,
        # False: the Founder rule was unconfirmed when this snapshot was taken, so
        # it names no Founder and no Lucky Draw entrant (founders is empty).
        "founder_rule_confirmed": confirmed,
        "ledger_watermark": _watermark(db),
    }


def _freeze_writers(db: Session) -> None:
    """Writers wait; readers do not. SHARE conflicts with every INSERT/UPDATE/DELETE
    and with nothing that only reads. The order matches the writers' own (a profile
    completion updates users before it awards; an offline review updates its row
    before it awards), so this cannot deadlock with them."""
    db.execute(text("LOCK TABLE users, offline_scans, points_log IN SHARE MODE"))


def _comparable(payload: dict[str, Any]) -> tuple[list[tuple[Any, ...]], list[Any]]:
    """What a result decides: places, people, points and the moment each reached
    them, and who is a Founder. Names and photos may change without making a
    ranking wrong, so they are left out."""
    ranking = [(e["place"], e["user_id"], e["points"], e["reached_at"]) for e in payload["ranking"]]
    return ranking, sorted(f["user_id"] for f in payload["founders"])


def _stale(db: Session, row: FinalResult) -> bool:
    return _comparable(build_payload(db)) != _comparable(row.payload)


def _summary(row: FinalResult) -> dict[str, Any]:
    return {
        "id": row.id,
        "version": row.version,
        "status": row.status.lower(),
        "created_at": row.created_at.isoformat(),
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "entries": len(row.payload.get("ranking", [])),
        "founders": len(row.payload.get("founders", [])),
        "founder_rule_confirmed": bool(row.payload.get("founder_rule_confirmed", False)),
    }


def _audit(db: Session, staff: StaffAccount, action: str, row: FinalResult, before: Any) -> None:
    db.add(
        AdminAudit(
            staff_account_id=staff.id,
            action=action,
            target_type="final_result",
            target_id=str(row.id),
            before_value=before,
            after_value=_summary(row),
        )
    )
    db.flush()


def create(db: Session, staff: StaffAccount, now: datetime) -> dict[str, Any]:
    """Caller holds the reset fence. Concurrent creators are serialised on the
    event row, so versions never collide."""
    require_reviewer(db, staff)
    event = db.execute(
        select(MasterclassEvent).order_by(MasterclassEvent.id).limit(1).with_for_update()
    ).scalar_one_or_none()
    if event is None:
        raise FinalError("not_configured")
    close = closes_at(db)
    if close is None or now < close:
        raise FinalError("competition_open")
    _freeze_writers(db)  # one consistent state for ranking, Founders and the marker
    latest = db.execute(
        select(func.coalesce(func.max(FinalResult.version), 0)).where(
            FinalResult.event_id == event.id
        )
    ).scalar_one()
    row = FinalResult(
        event_id=event.id,
        version=int(latest) + 1,
        payload=build_payload(db),
        created_by_staff_id=staff.id,
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    _audit(db, staff, AUDIT_CREATE, row, None)
    return _summary(row)


def _locked(db: Session, result_id: int) -> FinalResult:
    row = db.execute(
        select(FinalResult).where(FinalResult.id == result_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise FinalMissing("No such result.")
    return row


def publish(db: Session, staff: StaffAccount, result_id: int, now: datetime) -> dict[str, Any]:
    require_reviewer(db, staff)
    _event(db)
    row = _locked(db, result_id)
    if row.status == "PUBLISHED":
        return _summary(row)  # a repeated publish
    if row.status != "DRAFT":
        raise FinalError("not_a_draft")
    _freeze_writers(db)  # nothing may commit between these checks and the publication
    from app.modules.competition.offline import pending_count

    if pending_count(db):
        raise FinalError("offline_reviews_pending")
    if _stale(db, row):
        raise FinalError("stale_snapshot")
    before = _summary(row)
    for old in db.execute(
        select(FinalResult)
        .where(FinalResult.event_id == row.event_id, FinalResult.status == "PUBLISHED")
        .with_for_update()
    ).scalars():
        if old.version > row.version:
            raise FinalError("newer_version_published")
        old.status = "SUPERSEDED"
        old.closed_at = now
    db.flush()
    row.status = "PUBLISHED"
    row.published_at = now
    row.published_by_staff_id = staff.id
    db.flush()
    _audit(db, staff, AUDIT_PUBLISH, row, before)
    return _summary(row)


def discard(db: Session, staff: StaffAccount, result_id: int, now: datetime) -> dict[str, Any]:
    require_reviewer(db, staff)
    row = _locked(db, result_id)
    if row.status == "DISCARDED":
        return _summary(row)
    if row.status != "DRAFT":
        raise FinalError("not_a_draft")
    before = _summary(row)
    row.status = "DISCARDED"
    row.closed_at = now
    db.flush()
    _audit(db, staff, AUDIT_DISCARD, row, before)
    return _summary(row)


def versions(db: Session, staff: StaffAccount) -> dict[str, Any]:
    from app.modules.competition.offline import pending_count

    require_reviewer(db, staff)
    close = closes_at(db)
    event = db.execute(select(MasterclassEvent).order_by(MasterclassEvent.id)).scalars().first()
    rows = db.execute(select(FinalResult).order_by(FinalResult.version.desc())).scalars()
    return {
        "closes_at": close.isoformat() if close else None,
        "timezone": event.timezone if event else "UTC",
        # Late offline scans still waiting: review them before the final snapshot.
        "pending_offline_scans": pending_count(db),
        "ledger_watermark": _watermark(db),
        "versions": [_summary(r) for r in rows],
    }


def detail(db: Session, staff: StaffAccount, result_id: int) -> dict[str, Any]:
    require_reviewer(db, staff)
    row = db.get(FinalResult, result_id)
    if row is None:
        raise FinalMissing("No such result.")
    from app.modules.competition.offline import pending_count

    return {
        **_summary(row),
        "ranking": row.payload["ranking"],
        "founder_list": row.payload["founders"],
        # The result as computed now differs (a correction, a verified offline
        # scan): this snapshot is unchanged; take a new version to include it.
        # Publishing it is refused (`stale_snapshot`).
        "ledger_changed_since": row.status in ("DRAFT", "PUBLISHED") and _stale(db, row),
        "pending_offline_scans": pending_count(db),
    }


def founders_csv(db: Session, staff: StaffAccount, result_id: int) -> str:
    """The Lucky Draw eligibility list of ONE snapshot (not the live ledger)."""
    require_reviewer(db, staff)
    row = db.get(FinalResult, result_id)
    if row is None:
        raise FinalMissing("No such result.")
    if not row.payload.get("founder_rule_confirmed", False):
        raise FinalError("founder_rule_unconfirmed")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["user_id", "name", "founder_at"])
    for f in row.payload["founders"]:
        # A leading formula character is neutralised for spreadsheet safety.
        name = str(f["name"])
        writer.writerow([f["user_id"], f"'{name}" if name[:1] in "=+-@" else name, f["founder_at"]])
    _audit(db, staff, AUDIT_EXPORT, row, None)
    return out.getvalue()


def _public_person(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": entry["name"],
        "photo_url": avatar_url(entry.get("photo_key"), SMALL),
        **({"place": entry["place"], "points": entry["points"]} if "place" in entry else {}),
    }


def missing_configuration(db: Session) -> list[str]:
    """What the competition still lacks, in a fixed order: the event, its 17:30
    close, the competition ranking switched on. The same list everywhere a
    status is shown (`public`, the venue screen, the organiser overview)."""
    from app.modules.leaderboard.service import competition_mode

    has_event = db.execute(select(MasterclassEvent.id).limit(1)).first() is not None
    return [
        name
        for name, absent in (
            ("event", not has_event),
            ("close", closes_at(db) is None),
            ("competition_mode", not competition_mode(db)),
        )
        if absent
    ]


def public(db: Session, now: datetime) -> dict[str, Any]:
    """What the big screen and every attendee see. Never contact data.

    `status`: `published` (a Game Master published a result), `not_configured`
    (no event or no close: nothing can be earned yet - never shown as closed),
    `disabled` (configured, but the competition ranking is switched off), `open`
    (before the close) or `pending_review` (after it). `missing` names what is
    absent (`missing_configuration`)."""
    close = closes_at(db)
    missing = missing_configuration(db)
    row = db.execute(
        select(FinalResult).where(FinalResult.status == "PUBLISHED").limit(1)
    ).scalar_one_or_none()
    if row is None:
        if "event" in missing or "close" in missing:
            status = "not_configured"
        elif missing:
            status = "disabled"
        else:
            status = "open" if close is not None and now < close else "pending_review"
        return {
            "status": status,
            "missing": missing,
            "closes_at": close.isoformat() if close else None,
        }
    return {
        "status": "published",
        "missing": missing,
        "closes_at": close.isoformat() if close else None,
        "version": row.version,
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "top": [_public_person(e) for e in row.payload["ranking"][:ON_SCREEN]],
        "founders": [_public_person(f) for f in row.payload["founders"]],
        "founder_rule_confirmed": bool(row.payload.get("founder_rule_confirmed", False)),
    }


def my_final(db: Session, user_id: int) -> dict[str, Any] | None:
    row = db.execute(
        select(FinalResult).where(FinalResult.status == "PUBLISHED").limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    mine = next((e for e in row.payload["ranking"] if e["user_id"] == user_id), None)
    founder = any(f["user_id"] == user_id for f in row.payload["founders"])
    return {
        "version": row.version,
        "place": mine["place"] if mine else None,
        "points": mine["points"] if mine else None,
        "founder": founder,
    }


def founder_status(db: Session, user_id: int) -> dict[str, Any]:
    from app.modules.competition.founder import at_grand_final, zones

    reached = zones(db, user_id)
    confirmed = rule_confirmed()
    bonus = live_founder(db, user_id) if confirmed else None
    user = db.get(User, user_id)
    return {
        "zones": {zone: at is not None for zone, at in reached.items()}
        | {"grand_final": at_grand_final(db, user_id)},
        "founder_rule_confirmed": confirmed,
        "founder": bonus is not None,
        "lucky_draw": bonus is not None and eligible(user),
    }
