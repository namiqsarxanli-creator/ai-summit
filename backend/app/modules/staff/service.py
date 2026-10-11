"""Staff login mechanics and preset resolution — ARCHITECTURE.md §12.1,
§21.1, §21.8, §7.10.

`login_staff` takes an already-fenced session (the caller opened
`mutating_transaction`) — the same contract `attendees/service.py`
documents for its own mutations. `resolved_presets_for_booth` is a pure
read and takes no fence (§19.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.security import hash_password, password_needs_rehash, verify_secret
from app.core.sessions import create_staff_session
from app.modules.admin.models import EventConfig
from app.modules.booths.models import Booth, Task, TaskPresetPoint
from app.modules.staff.models import StaffAccount

# ARCHITECTURE.md §21.8, verbatim.
FAILED_LOGIN_LOCK_THRESHOLD = 10
LOCKOUT_DURATION = timedelta(minutes=15)


@dataclass(frozen=True)
class LoginOutcome:
    """Deliberately NOT raised as an exception from inside the fenced
    transaction — mirrors `attendees.service.VerifyOutcome`: a failed
    attempt must still commit its `failed_login_count` increment
    (§21.8), so failure here is a normal return value. The caller (the
    router) turns `ok=False` into `ACCOUNT_LOCKED` or
    `INVALID_CREDENTIALS` only AFTER the transaction has committed."""

    ok: bool
    locked: bool = False
    staff: StaffAccount | None = None
    session_token: str | None = None


def login_staff(db: Session, *, email: str, password: str) -> LoginOutcome:
    """§12.1's flow, exactly:

    - Unknown email -> generic failure (§12.1: "no distinction between
      unknown email and wrong password"). No row, nothing to update.
    - Already locked (`locked_until` in the future) -> generic
      "locked" failure **without evaluating the password** (§21.8) —
      Argon2id's ~100ms is never spent on a request this policy has
      already decided to refuse.
    - Wrong password -> `failed_login_count` increments; the 10th such
      failure additionally arms `locked_until` for 15 minutes. This
      (the 10th) attempt itself still return the generic failure, not
      "locked" — §21.8 says "further attempts return ACCOUNT_LOCKED",
      i.e. the ones after the count is already at the threshold.
    - Correct password on a deactivated account (`is_active = False`,
      §7.7: "deactivation kills future logins") -> the SAME generic
      failure, no session, no counter change. Not named as its own
      case in §12.1/§21.8, but treated identically for the same
      enumeration-resistance reason those sections give for the other
      two cases — a distinguishable "your account is deactivated"
      response would itself be the oracle §12.1 rules out.
    - Correct password on an active account -> resets the counters,
      rehashes the password if `_hasher`'s parameters changed since it
      was last hashed (§21.1: "verify_and_update rehashes on parameter
      change"), and issues a session exactly as M05's attendee login
      does, with `principal_type = STAFF` (§12.1).
    """
    normalized_email = email.strip().lower()
    staff = db.execute(
        select(StaffAccount).where(StaffAccount.email == normalized_email).with_for_update()
    ).scalar_one_or_none()
    if staff is None:
        return LoginOutcome(ok=False)

    now = datetime.now(UTC)
    if staff.locked_until is not None and staff.locked_until > now:
        return LoginOutcome(ok=False, locked=True)

    if not verify_secret(staff.password_hash, password):
        new_count = staff.failed_login_count + 1
        values: dict[str, object] = {"failed_login_count": new_count}
        if new_count >= FAILED_LOGIN_LOCK_THRESHOLD:
            values["locked_until"] = now + LOCKOUT_DURATION
        db.execute(update(StaffAccount).where(StaffAccount.id == staff.id).values(**values))
        return LoginOutcome(ok=False)

    if not staff.is_active:
        return LoginOutcome(ok=False)

    if password_needs_rehash(staff.password_hash):
        staff.password_hash = hash_password(password)
    staff.failed_login_count = 0
    staff.locked_until = None

    raw_token = create_staff_session(db, staff_account_id=staff.id, now=now)
    return LoginOutcome(ok=True, staff=staff, session_token=raw_token)


def awardable_tasks_for_booth(db: Session, booth_id: int) -> list[dict[str, object]]:
    """The booth's ACTIVE activities that have a point value, in display
    order, with the repeat policy each one resolves to. A pure read (no
    fence, §19.4). Activities without a value are left out: staff cannot
    award what the server would refuse."""
    from app.modules.points.service import compute_rule_revision, resolve_repeat_policy

    booth_default = db.execute(
        select(Booth.allow_repeat_task_points).where(Booth.id == booth_id)
    ).scalar_one_or_none()
    event_default = db.execute(
        select(EventConfig.allow_repeat_task_points_default).where(EventConfig.id == 1)
    ).scalar_one()
    rows = db.execute(
        select(Task.id, Task.title, Task.description, Task.points, Task.allow_repeat_points)
        .where(
            Task.booth_id == booth_id,
            Task.is_active.is_(True),
            Task.points.is_not(None),
        )
        .order_by(Task.sort_order, Task.id)
    ).all()
    tasks: list[dict[str, object]] = []
    for r in rows:
        repeatable = resolve_repeat_policy(
            task=r.allow_repeat_points, booth=booth_default, event_default=event_default
        )
        tasks.append(
            {
                "id": r.id,
                "title": r.title,
                "description": r.description,
                "points": int(r.points),
                "repeatable": repeatable,
                # Echoed back on the award request to confirm THIS rule; the
                # server re-derives it at award time and refuses on mismatch.
                "rule_revision": compute_rule_revision(
                    task_id=r.id, points=int(r.points), repeatable=repeatable
                ),
            }
        )
    return tasks


def resolved_presets_for_booth(db: Session, booth_id: int) -> list[TaskPresetPoint]:
    """§7.10, verbatim: "presets(booth) = rows WHERE booth_id =
    booth.id AND is_active, falling back to rows WHERE booth_id IS
    NULL AND is_active when the first set is empty." Never mixes the
    two sets — a booth with its own rows overrides the defaults
    entirely."""
    own = list(
        db.execute(
            select(TaskPresetPoint)
            .where(TaskPresetPoint.booth_id == booth_id, TaskPresetPoint.is_active.is_(True))
            .order_by(TaskPresetPoint.sort_order, TaskPresetPoint.points)
        ).scalars()
    )
    if own:
        return own
    return list(
        db.execute(
            select(TaskPresetPoint)
            .where(TaskPresetPoint.booth_id.is_(None), TaskPresetPoint.is_active.is_(True))
            .order_by(TaskPresetPoint.sort_order, TaskPresetPoint.points)
        ).scalars()
    )
