"""Offline scans: submission, review and application (see offline_models.py).

Submitting (the scanning account, inside the fence):
1. a replay of the same client scan id returns the stored answer, nothing more;
2. the session, the scope (or booth) and the attendee's QR are checked NOW -
   a revoked scope, a deactivated account or a rotated card stops it here;
3. a scan that reaches the server within `LIVE_GRACE` of the device's time,
   before the close, is applied at once like any live scan (the server clock
   decides its window); anything later waits for review. A device time in the
   future is refused.

Reviewing (a Game Master or an administrator - never the account that made
the scan, whatever else it may hold): the reviewer states the time they VERIFIED
(`verified_at`, e.g. from the host's paper list) and why; the phone's claimed
time is kept beside it and is never the evidence by itself. Approving re-checks,
in the same transaction, that the card the scan named is still live and its
owner active, and that the scanning account is still active with its scope;
then it applies the scan as of the verified time (before the close, not after
the server received it) through the same scan functions, so every window, seat,
booking and once-only rule still holds. A refusal at any of these steps is
recorded as FAILED with its reason and audited; it leaves no attendance and no
points behind (a savepoint). Rows submitted before migration 0027 carry no card
evidence: they are approved only when the reviewer re-scans the attendee's
current card (`card_token`), which must name the same person.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainError, StaffNotAuthorizedError
from app.modules.admin.models import AdminAudit
from app.modules.attendees.models import User, UserStatus
from app.modules.competition import attendance
from app.modules.competition.final import require_reviewer
from app.modules.competition.ledger import closes_at
from app.modules.competition.offline_models import OfflineScan
from app.modules.competition.scopes import may_work_booth, require_scope
from app.modules.contact_cards.models import ContactCard
from app.modules.contact_cards.service import resolve_active_attendee
from app.modules.playground.models import PlaygroundSlot
from app.modules.staff.models import StaffAccount

LIVE_GRACE: Final = timedelta(minutes=2)
FUTURE_TOLERANCE: Final = timedelta(minutes=2)
AUDIT_APPROVE = "offline_scan.approve"
AUDIT_REJECT = "offline_scan.reject"


class OfflineScanError(DomainError):
    code = "OFFLINE_SCAN_REFUSED"
    status_code = 409

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.details = {"reason": reason}


class OfflineScanMissing(DomainError):
    code = "OFFLINE_SCAN_NOT_FOUND"
    status_code = 404


def queue_owner(staff_id: int) -> str:
    """The opaque label a phone files its queued scans under (domain-separated HMAC)."""
    from app.core.config import get_settings

    payload = f"facemark:offline-queue-owner:v1:{staff_id}".encode()
    return hmac.new(get_settings().scan_ref_secret.encode(), payload, hashlib.sha256).hexdigest()


def current_generation(db: Session) -> int:
    from app.modules.admin.models import EventConfig

    return int(
        db.execute(select(EventConfig.dataset_generation).where(EventConfig.id == 1)).scalar_one()
    )


def view(row: OfflineScan) -> dict[str, Any]:
    return {
        "id": row.id,
        "client_scan_id": row.client_scan_id,
        "kind": row.kind.lower(),
        "status": row.status.lower(),
        "outcome": row.outcome,
        "review_reason": row.review_reason,
        "device_scanned_at": row.device_scanned_at.isoformat(),
        "verified_at": row.verified_at.isoformat() if row.verified_at else None,
    }


def _check_authority(db: Session, staff: StaffAccount, kind: str, target_id: int) -> None:
    if kind == "MASTERCLASS_DOOR":
        require_scope(db, staff, "MASTERCLASS_DOOR", target_id)
    elif kind == "MENTOR_COMPLETE":
        require_scope(db, staff, "MENTOR_HOST", target_id)
    else:
        slot = db.get(PlaygroundSlot, target_id)
        if slot is None or not may_work_booth(db, staff, slot.booth_id):
            raise StaffNotAuthorizedError("Only this booth's staff can complete its sessions.")


def _apply(
    db: Session, staff: StaffAccount, row: OfflineScan, user: User, at: datetime
) -> dict[str, Any]:
    from app.modules.mentors import service as mentors

    if row.kind == "MASTERCLASS_DOOR":
        assert row.phase is not None
        return attendance.door_scan(
            db, staff, masterclass_id=row.target_id, phase=row.phase, now=at, attendee=user
        )
    if row.kind == "PLAYGROUND_COMPLETE":
        return attendance.playground_complete(
            db, staff, slot_id=row.target_id, now=at, attendee=user
        )
    return mentors.complete(db, staff, session_id=row.target_id, now=at, attendee=user)


def submit(
    db: Session,
    staff: StaffAccount,
    *,
    client_scan_id: str,
    kind: str,
    target_id: int,
    phase: str | None,
    card_token: str,
    scanned_at: datetime,
    now: datetime,
    owner: str,
    generation: int,
) -> dict[str, Any]:
    replay = db.execute(
        select(OfflineScan).where(
            OfflineScan.staff_account_id == staff.id,
            OfflineScan.client_scan_id == client_scan_id,
        )
    ).scalar_one_or_none()
    if replay is not None:
        return view(replay)
    # Defence in depth behind the phone's own filing: a scan queued under another
    # account, or before a dataset reset, is never accepted as this account's.
    if not hmac.compare_digest(owner, queue_owner(staff.id)):
        raise OfflineScanError("queue_owner_mismatch")
    if generation != current_generation(db):
        raise OfflineScanError("dataset_changed")
    if (kind == "MASTERCLASS_DOOR") != (phase is not None):
        raise OfflineScanError("phase_mismatch")
    if scanned_at > now + FUTURE_TOLERANCE:
        raise OfflineScanError("device_time_in_future")
    _check_authority(db, staff, kind, target_id)  # revoked now -> refused now
    user = resolve_active_attendee(db, card_token)  # rotated / inactive card -> refused
    card_id = db.execute(
        select(ContactCard.id).where(
            ContactCard.public_token == card_token, ContactCard.revoked_at.is_(None)
        )
    ).scalar_one()
    row = OfflineScan(
        card_id=card_id,
        staff_account_id=staff.id,
        client_scan_id=client_scan_id,
        kind=kind,
        target_id=target_id,
        phase=phase,
        user_id=user.id,
        device_scanned_at=scanned_at,
        received_at=now,
        status="PENDING_REVIEW",
    )
    close = closes_at(db)
    live = now - scanned_at <= LIVE_GRACE and (close is None or now < close)
    if live:
        # Effectively a live scan: the server clock decides, exactly as online.
        row.outcome = _apply(db, staff, row, user, now)
        row.status = "APPLIED"
        row.verified_at = now
    db.add(row)
    db.flush()
    return view(row)


def _locked(db: Session, scan_id: int) -> OfflineScan:
    row = db.execute(
        select(OfflineScan).where(OfflineScan.id == scan_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise OfflineScanMissing("No such scan.")
    return row


def _audit(db: Session, reviewer: StaffAccount, action: str, row: OfflineScan) -> None:
    db.add(
        AdminAudit(
            staff_account_id=reviewer.id,
            action=action,
            target_type="offline_scan",
            target_id=str(row.id),
            before_value={"id": row.id, "status": "pending_review"},
            after_value={
                "id": row.id,
                "status": row.status.lower(),
                "reason": row.review_reason,
                "device_scanned_at": row.device_scanned_at.isoformat(),
                "verified_at": row.verified_at.isoformat() if row.verified_at else None,
                "outcome": (row.outcome or {}).get("reason"),
            },
        )
    )
    db.flush()


def _refusal_reason(refused: DomainError) -> str:
    details = getattr(refused, "details", None)
    reason = details.get("reason") if isinstance(details, dict) else None
    return str(reason or refused.code)


def _card_evidence(
    db: Session, row: OfflineScan, card_token: str | None
) -> tuple[User | None, str | None]:
    """The attendee the scan may be applied to now, or (None, why not).
    Raises OfflineScanError when a pre-0027 row lacks card evidence and none was
    re-scanned: that is the reviewer's input to give, not an outcome to record."""
    if row.card_id is None:
        if not card_token:
            raise OfflineScanError("card_evidence_required")
        try:
            user = resolve_active_attendee(db, card_token)
        except DomainError:
            return None, "card_revoked"
        if user.id != row.user_id:
            raise OfflineScanError("card_mismatch")
        row.card_id = db.execute(
            select(ContactCard.id).where(
                ContactCard.public_token == card_token, ContactCard.revoked_at.is_(None)
            )
        ).scalar_one()
        return user, None
    card = db.execute(
        select(ContactCard).where(ContactCard.id == row.card_id).with_for_update()
    ).scalar_one_or_none()
    if card is None or card.revoked_at is not None or card.user_id != row.user_id:
        return None, "card_revoked"
    owner = db.get(User, row.user_id)
    if owner is None or owner.status != UserStatus.ACTIVE:
        return None, "attendee_inactive"
    return owner, None


def approve(
    db: Session,
    reviewer: StaffAccount,
    scan_id: int,
    reason: str,
    now: datetime,
    *,
    verified_at: datetime,
    card_token: str | None = None,
) -> dict[str, Any]:
    require_reviewer(db, reviewer)
    row = _locked(db, scan_id)
    if row.status != "PENDING_REVIEW":
        return view(row)  # a repeated decision changes nothing
    if row.staff_account_id == reviewer.id:
        # Separation of duties: the account that made a scan never confirms it,
        # whatever review rights it also holds.
        raise OfflineScanError("self_review")
    if verified_at > row.received_at:
        raise OfflineScanError("verified_after_received")
    close = closes_at(db)
    user, refused = _card_evidence(db, row, card_token)
    row.reviewed_by_staff_id = reviewer.id
    row.reviewed_at = now
    row.review_reason = reason
    row.verified_at = verified_at
    if close is not None and verified_at >= close:
        refused = "after_close"
    scanner = db.get(StaffAccount, row.staff_account_id)
    if refused is None:
        if scanner is None or not scanner.is_active:
            refused = "scanner_inactive"
        else:
            try:
                _check_authority(db, scanner, row.kind, row.target_id)
            except DomainError:
                refused = "scanner_not_authorized"
    if refused is None:
        assert scanner is not None and user is not None
        savepoint = db.begin_nested()  # a refused evaluation must leave nothing behind
        try:
            outcome = _apply(db, scanner, row, user, verified_at)
        except DomainError as error:
            savepoint.rollback()
            refused = _refusal_reason(error)
        else:
            savepoint.commit()
            row.outcome = outcome
            row.status = "APPLIED"
    if refused is not None:
        row.status = "FAILED"
        row.outcome = {"reason": refused}
    db.flush()
    _audit(db, reviewer, AUDIT_APPROVE, row)
    return view(row)


def reject(
    db: Session, reviewer: StaffAccount, scan_id: int, reason: str, now: datetime
) -> dict[str, Any]:
    require_reviewer(db, reviewer)
    row = _locked(db, scan_id)
    if row.status != "PENDING_REVIEW":
        return view(row)
    if row.staff_account_id == reviewer.id:
        raise OfflineScanError("self_review")
    row.status = "REJECTED"
    row.reviewed_by_staff_id = reviewer.id
    row.reviewed_at = now
    row.review_reason = reason
    db.flush()
    _audit(db, reviewer, AUDIT_REJECT, row)
    return view(row)


def pending(db: Session, reviewer: StaffAccount) -> list[dict[str, Any]]:
    require_reviewer(db, reviewer)
    rows = db.execute(
        select(OfflineScan, User, StaffAccount)
        .join(User, User.id == OfflineScan.user_id)
        .join(StaffAccount, StaffAccount.id == OfflineScan.staff_account_id)
        .where(OfflineScan.status == "PENDING_REVIEW")
        .order_by(OfflineScan.device_scanned_at, OfflineScan.id)
    ).all()
    return [
        {
            **view(s),
            "target_id": s.target_id,
            "phase": s.phase.lower() if s.phase else None,
            "attendee": f"{u.first_name} {u.last_name[:1]}.",
            "scanned_by": st.full_name,
            "received_at": s.received_at.isoformat(),
            # Pre-0027 rows: the reviewer must re-scan the attendee's card.
            "card_evidence": s.card_id is not None,
            "own_scan": s.staff_account_id == reviewer.id,
        }
        for s, u, st in rows
    ]


def pending_count(db: Session) -> int:
    return len(
        db.execute(select(OfflineScan.id).where(OfflineScan.status == "PENDING_REVIEW")).all()
    )
