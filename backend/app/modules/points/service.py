"""`PointsService.award_task()` — ARCHITECTURE.md §13.4, §14.2, §14.3.

The stage split mirrors the architecture's own lettering exactly, as
comments throughout: A1-A6 (pure, pre-fence, in `points/routers.py`)
and B1-B12 (fenced, here in `complete_award`). This is deliberate and
load-bearing — "the order is contractual, not merely an optimisation"
(§14.3) — so every stage below is commented with its letter rather than
left to be inferred from surrounding code.

No crash-recovery branch and no heuristic recovery lookup exist here,
by design (§13.4): a visible `idempotency_records` row is always
complete, because it is claimed and filled in one transaction together
with the `points_log` insert.
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
    InvalidPointValueError,
    MaintenanceError,
    StaffNotAuthorizedError,
    UnexpectedConstraintViolationError,
    translate_integrity_error,
)
from app.core.security import canonical_json, hash_card_token
from app.core.sessions import resolve_staff_session, resolve_staff_session_id
from app.modules.admin.models import EventConfig
from app.modules.booths.models import Booth
from app.modules.contact_cards.service import resolve_active_attendee
from app.modules.points.models import IdempotencyRecord, PointsLog, PointsSource
from app.modules.points.schemas import AwardResponse, AwardTaskPointsRequest
from app.modules.staff.models import StaffRole
from app.modules.staff.scan_ref import (
    ScanRefPayload,
    verify_not_expired,
    verify_staff_and_session_binding,
)
from app.modules.staff.service import resolved_presets_for_booth


def compute_award_fingerprint(*, card_token: str, points: int) -> str:
    """§13.4: `fingerprint = sha256(canonical_json({"v":1, "tok":
    sha256(card_token), "points": points}))`. Deliberately excludes
    `scan_ref` (time-bound by design), the booth (derived, not
    supplied) and all configuration — it answers exactly one question:
    is this the same logical award? A5, pre-fence: pure hashing."""
    payload: dict[str, object] = {
        "v": 1,
        "tok": hash_card_token(card_token),
        "points": points,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def maintenance_fast_path_check(db: Session) -> None:
    """A6, §19.4: "may also run [pre-fence], but it is advisory only —
    a courtesy that returns 503 quickly. It is never the authoritative
    gate." Reads and immediately ends its own transaction, so the
    fence stays the true first statement of the mutation's own
    transaction — this check is not allowed to leave a transaction
    open on `db` for `mutating_transaction` to inherit."""
    maintenance = db.execute(
        select(EventConfig.maintenance_mode).where(EventConfig.id == 1)
    ).scalar_one_or_none()
    db.rollback()
    if maintenance:
        raise MaintenanceError("The service is briefly unavailable. Please try again in a moment.")


@dataclass(frozen=True)
class AwardOutcome:
    """Wraps the canonical `AwardResponse` with the realtime publish
    target (M14) — kept OUT of `AwardResponse` itself, since that is
    the locked §13.4 response contract, not a place for internal
    plumbing. `newly_awarded_user_id` is `None` on a replay: nothing
    committed that anyone needs to be notified about (§17.7.1's "a
    rolled-back award cannot emit one" applies just as much to a
    replay, which changes nothing new either)."""

    response: AwardResponse
    newly_awarded_user_id: int | None


def complete_award(
    db: Session,
    *,
    raw_session_token: str | None,
    scan_payload: ScanRefPayload,
    request: AwardTaskPointsRequest,
    fingerprint: str,
    now: datetime,
) -> AwardOutcome:
    """B1-B11. Called inside the caller's `mutating_transaction(db)`
    block — the fence (B0) is already held by the time this runs.
    Returns the canonical response wrapped with the M14 publish target;
    the caller's context manager commits (B12) on a normal return and
    rolls back on any exception, so a failure anywhere below leaves no
    award and no claimed key."""
    # B1 — AUTHORITATIVE identity. A pre-fence cookie read (A2) is a
    # string extraction, not authentication (§14.3) — this is the real
    # check, and it happens only now, inside the fence.
    staff = resolve_staff_session(db, raw_session_token)
    if staff.role != StaffRole.BOOTH_STAFF:
        # Mirrors app.core.deps.current_booth_staff's role narrowing.
        # Admins deliberately cannot award (§12.3) — the account that
        # manages presets must not also be the one spending them.
        raise StaffNotAuthorizedError("This action requires a booth staff account.")
    session_id = resolve_staff_session_id(db, raw_session_token)
    verify_staff_and_session_binding(scan_payload, staff_id=staff.id, session_id=session_id)

    # B2 — booth_id is DERIVED from the authenticated staff account,
    # never read from the request (§12.2) — there is no parameter here
    # that could express a client-chosen booth.
    booth_id = staff.booth_id
    if booth_id is None:
        # Structurally impossible: §7.7's CHECK constraint guarantees
        # every BOOTH_STAFF row has a booth_id. Defensive only.
        raise UnexpectedConstraintViolationError("booth staff account has no booth_id")

    # B3 — IDEMPOTENCY LOOKUP, before any mutable validation.
    existing = db.execute(
        select(IdempotencyRecord).where(
            IdempotencyRecord.staff_account_id == staff.id,
            IdempotencyRecord.idempotency_key == request.idempotency_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise IdempotencyKeyReuseError()
        # A completed record is always whole (§7.12.1) — claim and
        # completion are the same transaction. Replayed verbatim;
        # `scan_ref` freshness is NOT evaluated on this path.
        return AwardOutcome(response=_replay(existing), newly_awarded_user_id=None)

    # B3a — scan_ref EXPIRY (check 8), new awards only, reached only
    # because B3 found no record.
    verify_not_expired(scan_payload, now=now)

    # B4 — resolve token -> attendee; unknown, revoked or PROVISIONAL
    # all raise InvalidQrError from inside resolve_active_attendee.
    attendee = resolve_active_attendee(db, request.card_token)

    # B5 — points must be ALLOW-LIST membership, never a range check.
    presets = resolved_presets_for_booth(db, booth_id)
    if request.points not in {p.points for p in presets}:
        raise InvalidPointValueError()

    # B6 — repeat policy -> dedupe_key (§14.5).
    booth = db.get(Booth, booth_id)
    if booth is None:
        raise UnexpectedConstraintViolationError("staff.booth_id references no booth")
    event_config = db.execute(select(EventConfig).where(EventConfig.id == 1)).scalar_one()
    allow_repeat = booth.allow_repeat_task_points
    if allow_repeat is None:
        allow_repeat = event_config.allow_repeat_task_points_default
    dedupe_key = None if allow_repeat else f"TASK:{attendee.id}:{booth_id}"

    # B7 — CLAIM the idempotency key: the FIRST write of the
    # transaction. A SAVEPOINT is essential here, not decorative: an
    # uncaught IntegrityError/OperationalError would otherwise abort
    # the WHOLE fenced transaction, and the wait-then-replay contract
    # (§13.4) requires continuing in the SAME transaction to re-read
    # the winner's now-complete record. Mirrors the SAVEPOINT pattern
    # `attendees/service.py._register_new` uses for its own email race.
    savepoint = db.begin_nested()
    record = IdempotencyRecord(
        staff_account_id=staff.id,
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
            # T1 committed first: its row is now visible AND complete.
            winner = db.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.staff_account_id == staff.id,
                    IdempotencyRecord.idempotency_key == request.idempotency_key,
                )
            ).scalar_one()
            if winner.request_fingerprint != fingerprint:
                raise IdempotencyKeyReuseError() from err
            return AwardOutcome(response=_replay(winner), newly_awarded_user_id=None)
        raise translated from err
    except OperationalError as err:
        savepoint.rollback()
        # 55P03 = lock_not_available: `lock_timeout` (2s) expired while
        # blocked on this same-key claim — a stuck/pathologically slow
        # transaction, not normal contention (§13.4). The safety-valve
        # code, never part of the normal contract.
        if getattr(err.orig, "sqlstate", None) == "55P03":
            raise AwardInProgressError() from err
        raise
    else:
        savepoint.commit()

    # B8 — insert points_log; violations dispatched by constraint name
    # (§7.14) — a `uq_points_dedupe_key` collision here means the
    # repeat policy genuinely forbids this award.
    points_log = PointsLog(
        user_id=attendee.id,
        booth_id=booth_id,
        source=PointsSource.TASK,
        points=request.points,
        entered_by_staff_id=staff.id,
        entered_by_system=False,
        dedupe_key=dedupe_key,
    )
    db.add(points_log)
    try:
        db.flush()
    except IntegrityError as err:
        raise translate_integrity_error(err) from err

    # B9 — resulting total, computed in-transaction over the ledger —
    # never a denormalised column (§14.1, invariant #1).
    resulting_total = db.execute(
        select(func.coalesce(func.sum(PointsLog.points), 0)).where(PointsLog.user_id == attendee.id)
    ).scalar_one()

    # B10 — the complete canonical response (§13.4). `attendee_name` is
    # first-name-plus-last-initial, exactly as the architecture's own
    # response example shows ("Leyla A.") — a narrower disclosure than
    # the full name the confirm screen already showed this same staff
    # member, appropriate for a value that is now persisted verbatim
    # into a snapshot. `rank` is deliberately absent.
    response = AwardResponse(
        points_log_id=points_log.id,
        points_awarded=request.points,
        resulting_total=int(resulting_total),
        attendee_name=f"{attendee.first_name} {attendee.last_name[0]}.",
        booth_name=booth.name,
        awarded_at=points_log.created_at,
        replayed=False,
    )

    # B11 — persist it as the snapshot, same transaction, before commit.
    record.points_log_id = points_log.id
    record.response_snapshot = response.model_dump(mode="json")
    record.completed_at = now

    return AwardOutcome(response=response, newly_awarded_user_id=attendee.id)
    # B12 — COMMIT happens in the caller's `with mutating_transaction`
    # block on normal return; award, claim and snapshot become visible
    # together, and the fence releases here. The router publishes the
    # M14 realtime signal only AFTER that commit — see points/routers.py.


def _replay(record: IdempotencyRecord) -> AwardResponse:
    """§13.4: "the response is computed before it is persisted and
    before commit — a replay is byte-identical to the original reply,"
    except `replayed` itself, which is stored `false` and always
    reported `true` on a replay."""
    snapshot = dict(record.response_snapshot or {})
    snapshot["replayed"] = True
    return AwardResponse.model_validate(snapshot)
