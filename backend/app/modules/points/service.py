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
import hmac
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import (
    AwardInProgressError,
    DomainError,
    DuplicateTaskRewardError,
    IdempotencyKeyConflict,
    IdempotencyKeyReuseError,
    MaintenanceError,
    StaffNotAuthorizedError,
    TaskInactiveError,
    TaskNotConfiguredError,
    TaskNotFoundError,
    TaskRuleChangedError,
    UnexpectedConstraintViolationError,
    translate_integrity_error,
)
from app.core.security import canonical_json, hash_card_token, sign_hmac_sha256
from app.core.sessions import resolve_staff_session, resolve_staff_session_id
from app.db.session import gate_on_maintenance
from app.modules.admin.models import EventConfig
from app.modules.booths.models import Booth, Task
from app.modules.contact_cards.service import resolve_active_attendee
from app.modules.points.models import (
    IdempotencyRecord,
    PointsEntryType,
    PointsLog,
    PointsSource,
)
from app.modules.points.schemas import AwardResponse, AwardTaskPointsRequest
from app.modules.staff.models import StaffRole
from app.modules.staff.scan_ref import (
    ScanRefPayload,
    verify_not_expired,
    verify_staff_and_session_binding,
)


def compute_award_fingerprint(*, card_token: str, task_id: int) -> str:
    """§13.4's fingerprint, version 2: `sha256(canonical_json({"v":2,
    "tok": sha256(card_token), "task": task_id}))`. The point value is not
    part of the request any more (it is the activity's server-side rule), so
    the activity takes its place: the same key with another attendee OR
    another activity is a different logical award and is refused as key
    reuse. Deliberately excludes `scan_ref` (time-bound by design), the booth
    (derived, not supplied) and all configuration. A5, pre-fence: pure
    hashing."""
    payload: dict[str, object] = {
        "v": 2,
        "tok": hash_card_token(card_token),
        "task": task_id,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def resolve_repeat_policy(*, task: bool | None, booth: bool | None, event_default: bool) -> bool:
    """May one attendee earn the same activity more than once?

    The most specific setting that exists wins: the activity's own
    `allow_repeat_points`, else the booth's `allow_repeat_task_points`
    (which now supplies the default for ITS activities - it no longer means
    "one award per booth"), else the event-wide default. With nothing set
    anywhere the event default (False out of the box) makes every activity
    once-per-attendee."""
    if task is not None:
        return task
    if booth is not None:
        return booth
    return event_default


def compute_rule_revision(*, task_id: int, points: int, repeatable: bool) -> str:
    """The server-issued revision of an activity's AWARD RULE: what one award
    pays and whether the same attendee may earn it again.

    It is derived, not stored, from the rule as it is IN FORCE (the activity's
    points and the repeat policy after activity -> booth -> event resolution),
    so every way of changing the rule changes it - editing the activity, the
    booth's repeat flag or the event default - without a counter that each of
    those edit paths would have to remember to bump. Keyed with a server
    secret and bound to the activity id, so it is opaque: a client can only
    echo one it was given. It confirms a rule; it never sets one."""
    message = f"task-rule:v1:{task_id}:{points}:{int(repeatable)}"
    return sign_hmac_sha256(get_settings().scan_ref_secret, message)[:22]


def maintenance_fast_path_check(db: Session) -> None:
    """A6, §19.4: "may also run [pre-fence], but it is advisory only —
    a courtesy that returns 503 quickly. It is never the authoritative
    gate." Reads and immediately ends its own transaction, so the
    fence stays the true first statement of the mutation's own
    transaction — this check is not allowed to leave a transaction
    open on `db` for `mutating_transaction` to inherit.

    With read caching on, the flag comes from the once-a-second read the
    X-Dataset-Generation header already makes (`cached_maintenance_mode`): no
    statement, no extra pooled-connection checkout and ping, no rollback here.
    That cached value is only the quick answer. The authoritative one is read
    fresh inside every fenced transaction of this request (`gate_on_maintenance`),
    so a stale cache can never let a write through while maintenance is on."""
    from app.modules.realtime.service import cached_maintenance_mode

    gate_on_maintenance(db)
    maintenance = cached_maintenance_mode()
    if maintenance is None:  # read caching off: read it fresh
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

    # B5 — the ACTIVITY and its server-side point rule. The activity must be
    # one of THIS booth's (derived at B2, never client-supplied), active, and
    # configured with a value. It is read FOR SHARE: an admin editing or
    # deactivating it waits until this award commits, so the value awarded is
    # exactly the value this transaction validated.
    task = db.execute(
        select(
            Task.id,
            Task.booth_id,
            Task.title,
            Task.is_active,
            Task.points,
            Task.allow_repeat_points,
        )
        .where(Task.id == request.task_id)
        .with_for_update(read=True, of=Task)
    ).first()
    if task is None or task.booth_id != booth_id:
        raise TaskNotFoundError()  # unknown and other-booth are indistinguishable
    if not task.is_active:
        return _replay_or_raise(
            db,
            staff_id=staff.id,
            request=request,
            fingerprint=fingerprint,
            error=TaskInactiveError(),
        )
    if task.points is None:
        return _replay_or_raise(
            db,
            staff_id=staff.id,
            request=request,
            fingerprint=fingerprint,
            error=TaskNotConfiguredError(),
        )
    awarded_points = int(task.points)

    # B6 — repeat policy (activity -> booth -> event default) -> dedupe_key.
    # Booth and event config are read FOR SHARE like the task: an admin switching
    # the repeat policy, or deactivating the booth, waits until this award commits,
    # so the policy applied is the one this transaction read.
    booth = db.execute(
        select(Booth).where(Booth.id == booth_id).with_for_update(read=True)
    ).scalar_one_or_none()
    if booth is None:
        raise UnexpectedConstraintViolationError("staff.booth_id references no booth")
    if not booth.is_active:
        # A deactivated booth awards nothing, exactly like a deactivated activity
        # (feedback and staff creation already refuse inactive booths).
        return _replay_or_raise(
            db,
            staff_id=staff.id,
            request=request,
            fingerprint=fingerprint,
            error=TaskInactiveError(),
        )
    event_config = db.execute(
        select(EventConfig).where(EventConfig.id == 1).with_for_update(read=True)
    ).scalar_one()
    allow_repeat = resolve_repeat_policy(
        task=task.allow_repeat_points,
        booth=booth.allow_repeat_task_points,
        event_default=event_config.allow_repeat_task_points_default,
    )
    # Once-only activities carry a dedupe key (the unique indexes are the
    # race-proof backstop). The explicit "already earned" check for them runs
    # AFTER the idempotency claim below (B7a): a retry of an award that has
    # since committed must reach the claim conflict and be replayed, not be
    # reported as a duplicate first.
    dedupe_key: str | None = None
    if not allow_repeat:
        dedupe_key = f"TASK:{attendee.id}:task:{task.id}"

    # B6a - the rule the staff member confirmed must be the rule in force.
    # Same activity, different points or repeat policy -> nothing is awarded
    # and the current rule is returned for a fresh confirmation. This runs
    # AFTER the idempotency lookup (B3), so a completed award still replays
    # whatever the rule is now.
    current_revision = compute_rule_revision(
        task_id=task.id, points=awarded_points, repeatable=allow_repeat
    )
    if not hmac.compare_digest(current_revision, request.rule_revision):
        return _replay_or_raise(
            db,
            staff_id=staff.id,
            request=request,
            fingerprint=fingerprint,
            error=TaskRuleChangedError(
                {
                    "task_id": task.id,
                    "task_title": task.title,
                    "points": awarded_points,
                    "repeatable": allow_repeat,
                    "rule_revision": current_revision,
                }
            ),
        )

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

    # B7a - once-only policy: refuse if this attendee already earned this
    # activity, even when the repeats were made while the policy still allowed
    # them (those rows carry no dedupe key, so only a look at the ledger sees
    # them). Raising here rolls the claim above back with everything else.
    if dedupe_key is not None:
        already = db.execute(
            select(PointsLog.id)
            .where(
                PointsLog.user_id == attendee.id,
                PointsLog.task_id == task.id,
                PointsLog.source == PointsSource.TASK,
                PointsLog.entry_type == PointsEntryType.AWARD,
            )
            .limit(1)
        ).first()
        if already is not None:
            raise DuplicateTaskRewardError()

    # B8 — insert points_log; violations dispatched by constraint name
    # (§7.14) — a `uq_points_dedupe_key` collision here means the
    # repeat policy genuinely forbids this award.
    points_log = PointsLog(
        user_id=attendee.id,
        booth_id=booth_id,
        source=PointsSource.TASK,
        points=awarded_points,
        task_id=task.id,
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
        points_awarded=awarded_points,
        resulting_total=int(resulting_total),
        attendee_name=f"{attendee.first_name} {attendee.last_name[0]}.",
        booth_name=booth.name,
        awarded_at=points_log.created_at,
        replayed=False,
        task_id=task.id,
        task_title=task.title,
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


def _replay_or_raise(
    db: Session,
    *,
    staff_id: int,
    request: AwardTaskPointsRequest,
    fingerprint: str,
    error: DomainError,
) -> AwardOutcome:
    """Replay this award if it committed in the meantime, else raise `error`.

    B3 looked for a completed record before the activity was read. A retry
    that overlapped its own original can lose a race: the original commits,
    an admin changes or switches off the activity, and only then does the
    retry read the activity. The change must not turn an award that already
    happened into a refusal, so the record is looked up once more (a fresh
    statement sees the newly committed row) and replayed if it exists."""
    done = db.execute(
        select(IdempotencyRecord).where(
            IdempotencyRecord.staff_account_id == staff_id,
            IdempotencyRecord.idempotency_key == request.idempotency_key,
        )
    ).scalar_one_or_none()
    if done is None:
        raise error
    if done.request_fingerprint != fingerprint:
        raise IdempotencyKeyReuseError() from error
    return AwardOutcome(response=_replay(done), newly_awarded_user_id=None)


def _replay(record: IdempotencyRecord) -> AwardResponse:
    """§13.4: "the response is computed before it is persisted and
    before commit — a replay is byte-identical to the original reply,"
    except `replayed` itself, which is stored `false` and always
    reported `true` on a replay."""
    snapshot = dict(record.response_snapshot or {})
    snapshot["replayed"] = True
    return AwardResponse.model_validate(snapshot)
