"""Reversing an AWARD in full - an administrator's correction of the ledger.

What it does, in ONE fenced transaction (the caller's `mutating_transaction`):

1. authenticates an ADMINISTRATOR (booth staff and attendees are refused);
2. looks the request's idempotency key up (a repeat replays the first answer);
3. locks the original entry `FOR UPDATE` and checks it is an AWARD that has no
   reversal yet;
4. claims the idempotency key, inserts the REVERSAL row and the admin-audit row,
   and stores the answer as the key's snapshot - all committed together or not
   at all.

The original row is never changed or deleted: a reversal is a NEW row with the
negative of the original's points, the same attendee, booth, activity and
source, `reverses_points_log_id` pointing at the original, and the
administrator recorded as the one who entered it. Every one of those values is
derived here from the original, never read from the request.

Guarantees, and where each one comes from:

* once per entry - the unique `uq_points_reverses` (a second reversal of the
  same row, however it is attempted, violates it) AND the row lock above;
* a retry or a double click replays - `idempotency_records` (primary key
  `(staff_account_id, idempotency_key)`), the same mechanism as awards, with a
  fingerprint of (entry, reason) so a key cannot be reused for another entry or
  another reason;
* a reversal that fails halfway leaves nothing - claim, row and audit share one
  transaction;
* a REVERSAL cannot be reversed (refused; a mistaken reversal is corrected by
  a new award).

What a reversal does NOT do: it does not make the activity earnable again. The
original AWARD keeps occupying the once-per-attendee slot (`dedupe_key` and
`uq_points_task_user_once` for activities, `uq_points_feedback_user_booth` for
feedback), because the award did happen and the reversal is a compensating
entry (ARCHITECTURE.md 7.12). A repeatable activity can of course be earned
again, as it always could. Reopening a once-only activity is deliberately a
separate, explicit decision this code does not make.

The reason is stored in `points_log.note` (<= 300 characters, database CHECK) and
in the audit row. It is never logged.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.errors import (
    AwardInProgressError,
    IdempotencyKeyConflict,
    IdempotencyKeyReuseError,
    PointsAlreadyReversedError,
    PointsEntryNotFoundError,
    PointsNotReversibleError,
    translate_integrity_error,
)
from app.core.security import canonical_json
from app.modules.admin import service as admin_service
from app.modules.admin.models import AdminAudit
from app.modules.points.models import (
    IdempotencyRecord,
    PointsEntryType,
    PointsLog,
    PointsSource,
)
from app.modules.points.schemas import ReversalRequest, ReversalResponse
from app.modules.staff.models import StaffAccount

AUDIT_ACTION = "points.reverse"
AUDIT_TARGET_TYPE = "points_log"


def compute_reversal_fingerprint(*, entry_id: int, reason: str) -> str:
    """What a repeat of THIS request must match: the entry and the reason (its
    hash - the text itself is not copied anywhere else). The key alone is not
    enough: the same key for another entry or another reason is a different
    request and is refused, not silently answered with the first one."""
    payload: dict[str, object] = {
        "v": 1,
        "op": AUDIT_ACTION,
        "entry": entry_id,
        "reason": hashlib.sha256(reason.encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


@dataclass(frozen=True)
class ReversalOutcome:
    response: ReversalResponse
    # Who to notify AFTER the commit; None on a replay (nothing new happened).
    reversed_user_id: int | None


def _find_record(db: Session, actor_id: int, request: ReversalRequest) -> IdempotencyRecord | None:
    return db.execute(
        select(IdempotencyRecord).where(
            IdempotencyRecord.staff_account_id == actor_id,
            IdempotencyRecord.idempotency_key == request.idempotency_key,
        )
    ).scalar_one_or_none()


def _replay(record: IdempotencyRecord) -> ReversalResponse:
    snapshot = dict(record.response_snapshot or {})
    snapshot["replayed"] = True
    return ReversalResponse.model_validate(snapshot)


def _replayed_or(
    db: Session, actor_id: int, request: ReversalRequest, fingerprint: str, error: Exception
) -> ReversalOutcome:
    """Replay this request if it committed in the meantime, else raise `error`.
    A repeat that overlapped its original waits on the entry's row lock; when
    it gets the lock the original has committed and the entry looks "already
    reversed" - which for THIS key is a replay, not a refusal. A fresh statement
    (READ COMMITTED) sees the newly committed record."""
    done = _find_record(db, actor_id, request)
    if done is None:
        raise error
    if done.request_fingerprint != fingerprint:
        raise IdempotencyKeyReuseError() from error
    return ReversalOutcome(response=_replay(done), reversed_user_id=None)


def _audit_values(
    original: PointsLog, reversal: PointsLog | None
) -> tuple[dict[str, object], dict[str, object]]:
    """The audit row's before / after. Facts and ids only; the reason is the one
    free text. (The audit view shows them through its own allowlist.)"""
    context: dict[str, object] = {
        "points_log_id": original.id,
        "user_id": original.user_id,
        "booth_id": original.booth_id,
        "task_id": original.task_id,
        "source": original.source.value,
    }
    before = {**context, "points": original.points, "reversed": False}
    after: dict[str, object] = {**context, "points": 0, "reversed": True}
    if reversal is not None:
        after["reversal_id"] = reversal.id
        after["reason"] = reversal.note
    return before, after


def reverse_entry(
    db: Session,
    *,
    raw_session_token: str | None,
    entry_id: int,
    request: ReversalRequest,
    now: datetime,
) -> ReversalOutcome:
    """Called inside the caller's `mutating_transaction`; it commits on a
    normal return and rolls back on any exception."""
    actor: StaffAccount = admin_service.authenticate_admin(db, raw_session_token)
    fingerprint = compute_reversal_fingerprint(entry_id=entry_id, reason=request.reason)

    # A repeat of a completed request replays it, whatever the entry looks like now.
    existing = _find_record(db, actor.id, request)
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise IdempotencyKeyReuseError()
        return ReversalOutcome(response=_replay(existing), reversed_user_id=None)

    original = db.execute(
        select(PointsLog).where(PointsLog.id == entry_id).with_for_update()
    ).scalar_one_or_none()
    if original is None:
        raise PointsEntryNotFoundError("No such points entry.")
    if original.source == PointsSource.FOUNDER:
        # Derived from the four zones: correct the evidence, and the bonus follows.
        return _replayed_or(
            db,
            actor.id,
            request,
            fingerprint,
            PointsNotReversibleError(
                "The Founder bonus follows its evidence; reverse the zone's award instead."
            ),
        )
    if original.entry_type != PointsEntryType.AWARD:
        return _replayed_or(
            db,
            actor.id,
            request,
            fingerprint,
            PointsNotReversibleError(
                "Only an award can be reversed; a reversal cannot be reversed."
            ),
        )
    already = db.execute(
        select(PointsLog.id).where(PointsLog.reverses_points_log_id == original.id)
    ).first()
    if already is not None:
        return _replayed_or(
            db,
            actor.id,
            request,
            fingerprint,
            PointsAlreadyReversedError("This award has already been reversed."),
        )

    # Claim the key (a SAVEPOINT, as for awards: a lost race must be able to
    # continue in this same transaction and replay the winner).
    savepoint = db.begin_nested()
    record = IdempotencyRecord(
        staff_account_id=actor.id,
        idempotency_key=request.idempotency_key,
        request_fingerprint=fingerprint,
        created_at=now,
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError as err:
        savepoint.rollback()
        translated = translate_integrity_error(err)
        if isinstance(translated, IdempotencyKeyConflict):
            winner = _find_record(db, actor.id, request)
            assert winner is not None  # the conflicting row is committed and visible
            if winner.request_fingerprint != fingerprint:
                raise IdempotencyKeyReuseError() from err
            return ReversalOutcome(response=_replay(winner), reversed_user_id=None)
        raise translated from err
    except OperationalError as err:
        savepoint.rollback()
        if getattr(err.orig, "sqlstate", None) == "55P03":  # lock_timeout while waiting
            raise AwardInProgressError() from err
        raise
    else:
        savepoint.commit()

    # Everything about the reversal comes from the original, not from the request.
    reversal = PointsLog(
        user_id=original.user_id,
        booth_id=original.booth_id,
        source=original.source,
        entry_type=PointsEntryType.REVERSAL,
        points=-original.points,
        task_id=original.task_id,
        reverses_points_log_id=original.id,
        entered_by_staff_id=actor.id,
        entered_by_system=False,
        dedupe_key=None,
        note=request.reason,
    )
    db.add(reversal)
    try:
        db.flush()
    except IntegrityError as err:
        # uq_points_reverses -> PointsAlreadyReversedError (a reversal that got
        # in between, which the row lock should make impossible: a backstop).
        raise translate_integrity_error(err) from err

    from app.modules.competition.founder import ZONES, recompute

    if any(original.source in sources for sources in ZONES.values()):
        recompute(db, original.user_id)  # e.g. the only masterclass of a Founder
    total = db.execute(
        select(func.coalesce(func.sum(PointsLog.points), 0)).where(
            PointsLog.user_id == original.user_id
        )
    ).scalar_one()
    response = ReversalResponse(
        reversal_id=reversal.id,
        reversed_entry_id=original.id,
        points_reversed=original.points,
        resulting_total=int(total),
        created_at=reversal.created_at,
        replayed=False,
    )
    record.points_log_id = reversal.id
    record.response_snapshot = response.model_dump(mode="json")
    record.completed_at = now

    before, after = _audit_values(original, reversal)
    db.add(
        AdminAudit(
            staff_account_id=actor.id,
            action=AUDIT_ACTION,
            target_type=AUDIT_TARGET_TYPE,
            target_id=str(original.id),
            before_value=before,
            after_value=after,
        )
    )
    db.flush()
    return ReversalOutcome(response=response, reversed_user_id=original.user_id)
