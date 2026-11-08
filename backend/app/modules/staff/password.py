"""Staff and administrator passwords: changing one, and getting back in by email.

Three flows, all inside the caller's fenced `mutating_transaction`:

* `change_password` - a signed-in staff member proves the CURRENT password and
  sets a new one;
* `request_reset` / `confirm_reset` - the forgotten-password flow: a one-time,
  short-lived code is emailed through the existing OTP outbox, and the code plus
  the new password set the password;
* `start_reset_for` - an administrator makes the system email that code to a
  staff member's OWN address. The administrator never sees the code and never
  chooses the new password: they only trigger the letter;
* `set_by_admin` - an administrator sets a staff member's new password
  directly, with no email (owner's decision, 8 Nov 2026: the booth logins use
  addresses that receive no mail). Never for an administrator account, never for
  a deactivated one; every session of the account ends, as with any change.

Rules that hold for all three:

* A successful change ENDS EVERY SESSION of the account, the current one
  included (`sessions.revoked_at`): the person signs in again with the new
  password. A `scan_ref` is bound to the session that issued it, so the ones
  issued before die with it; the realtime streams belong to attendees, not to
  staff, so none of them is a staff credential.
* The policy for a new password is the one the console already applies when it
  creates an account (`initial_admin.MIN_PASSWORD_LENGTH`..`MAX`).
* Passwords, hashes and reset codes are never logged, returned or written to
  the audit trail. The audit rows say WHAT happened (the account, who started
  it, how many sessions ended) and nothing secret.
* The reset code is a staff-only kind of challenge (`STAFF_PASSWORD_RESET`):
  every query here matches on that purpose, and the attendee verification
  matches on its own, so a code of one kind can never be used as the other.
  The code is verified against an Argon2id hash, is single-use, expires after
  ten minutes and allows five wrong tries; the outbox holds it only as AES-GCM
  ciphertext while the email is waiting to be sent.
* Nothing says whether an email belongs to a staff account: the public request
  gives one answer whatever the address, the confirmation fails with the same
  error for an unknown address, a deactivated account, a missing, expired or
  exhausted code and a wrong one, and every one of those paths does the same
  expensive Argon2id work, so the time does not tell them apart either.
* A deactivated account is never reactivated by a reset, and cannot start or
  finish one.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.auth_session import AuthSession
from app.core.config import get_settings
from app.core.errors import (
    AccountLockedError,
    CurrentPasswordInvalidError,
    PasswordResetUnavailableError,
    PasswordUnchangedError,
)
from app.core.ratelimit import (
    ADMIN_RESET_START_PER_ADMIN,
    STAFF_PASSWORD_CHANGE_PER_STAFF,
    STAFF_RESET_REQUEST_PER_EMAIL,
    limiter,
)
from app.core.security import (
    generate_otp_code,
    hash_otp_code,
    hash_password,
    verify_secret,
)
from app.core.sessions import resolve_staff_session
from app.modules.admin.models import AdminAudit
from app.modules.admin.service import AdminInvalid, AdminNotFound, authenticate_admin
from app.modules.attendees.models import OtpChallenge, OtpPurpose
from app.modules.attendees.service import OTP_MAX_ATTEMPTS, issue_otp
from app.modules.staff.models import StaffAccount, StaffRole
from app.modules.staff.service import FAILED_LOGIN_LOCK_THRESHOLD, LOCKOUT_DURATION

logger = logging.getLogger(__name__)

# The audit actions this module writes (admin/audit.py has an allowlist for each).
AUDIT_PASSWORD_CHANGE = "staff.password_change"
AUDIT_RESET_REQUESTED = "staff.password_reset_requested"
AUDIT_RESET_DONE = "staff.password_reset"
AUDIT_ADMIN_SET = "staff.password_admin_set"
AUDIT_TARGET = "staff_account"


@functools.cache
def _decoy_hash() -> str:
    """An Argon2id hash nobody knows the preimage of, to spend the same time on
    a request that has nothing real to check."""
    return hash_otp_code(generate_otp_code())


def _spend_the_same_time(code: str) -> None:
    verify_secret(_decoy_hash(), code)


def _audit(
    db: Session, *, actor_id: int, action: str, staff_id: int, after: dict[str, Any]
) -> None:
    db.add(
        AdminAudit(
            staff_account_id=actor_id,
            action=action,
            target_type=AUDIT_TARGET,
            target_id=str(staff_id),
            before_value=None,
            after_value={"id": staff_id, **after},
        )
    )
    db.flush()


def _end_all_sessions(db: Session, staff_id: int) -> int:
    """Revokes every live session of the account; returns how many."""
    revoked = db.execute(
        update(AuthSession)
        .where(AuthSession.staff_account_id == staff_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=func.now())
        .returning(AuthSession.id)
    ).all()
    return len(revoked)


def _set_password(db: Session, staff: StaffAccount, new_password: str) -> int:
    """New hash, lockout cleared, every session ended. Returns sessions ended."""
    staff.password_hash = hash_password(new_password)
    staff.failed_login_count = 0
    staff.locked_until = None
    db.flush()
    return _end_all_sessions(db, staff.id)


# --- change (signed in) ---------------------------------------------------------


@dataclass(frozen=True)
class ChangeOutcome:
    """A wrong current password is a RETURN value, not an exception: the failed
    attempt must still commit its counter (exactly like login), so the router
    raises the error only after the transaction has committed."""

    ok: bool
    locked: bool = False
    sessions_ended: int = 0


def change_password(
    db: Session, *, raw_session_token: str | None, current_password: str, new_password: str
) -> ChangeOutcome:
    signed_in = resolve_staff_session(db, raw_session_token)
    limiter.check([(STAFF_PASSWORD_CHANGE_PER_STAFF, str(signed_in.id))])
    staff = db.execute(
        select(StaffAccount).where(StaffAccount.id == signed_in.id).with_for_update()
    ).scalar_one()

    now = datetime.now(UTC)
    if staff.locked_until is not None and staff.locked_until > now:
        return ChangeOutcome(ok=False, locked=True)

    if not verify_secret(staff.password_hash, current_password):
        count = staff.failed_login_count + 1
        values: dict[str, object] = {"failed_login_count": count}
        if count >= FAILED_LOGIN_LOCK_THRESHOLD:
            values["locked_until"] = now + LOCKOUT_DURATION
        db.execute(update(StaffAccount).where(StaffAccount.id == staff.id).values(**values))
        return ChangeOutcome(ok=False)

    if verify_secret(staff.password_hash, new_password):
        raise PasswordUnchangedError("The new password must differ from the current one.")

    ended = _set_password(db, staff, new_password)
    _audit(
        db,
        actor_id=staff.id,
        action=AUDIT_PASSWORD_CHANGE,
        staff_id=staff.id,
        after={"sessions_revoked": ended},
    )
    return ChangeOutcome(ok=True, sessions_ended=ended)


def raise_for_failed_change(outcome: ChangeOutcome) -> None:
    """After the commit: the error the failed outcome stands for."""
    if outcome.locked:
        raise AccountLockedError("Too many failed attempts. Try again later.")
    if not outcome.ok:
        raise CurrentPasswordInvalidError("The current password is not correct.")


# --- reset by email: starting it ----------------------------------------------


def _issue_reset_code(db: Session, staff: StaffAccount, language: str) -> None:
    """Writes the challenge and its outbox row (same transaction). The plain code
    exists only inside `issue_otp` and the outbox ciphertext: it is dropped here
    - never returned, logged, shown to an administrator or put in the audit."""
    issue_otp(
        db,
        email=str(staff.email).lower(),
        user_id=None,
        purpose=OtpPurpose.STAFF_PASSWORD_RESET,
        language=language,
    )


def request_reset(db: Session, *, email: str, language: str) -> None:
    """The public "I forgot my password". Returns nothing, whatever the address."""
    normalized = email.strip().lower()
    staff = db.execute(
        select(StaffAccount).where(StaffAccount.email == normalized).with_for_update()
    ).scalar_one_or_none()
    usable = staff is not None and staff.is_active
    outbox = get_settings().outbox_enabled
    if staff is not None and usable and outbox:
        _issue_reset_code(db, staff, language)
        _audit(
            db,
            actor_id=staff.id,
            action=AUDIT_RESET_REQUESTED,
            staff_id=staff.id,
            after={"initiated_by": "self"},
        )
        return
    # Nothing to send (unknown, deactivated, or no email outbox here): do the
    # work a real request does, so the answer takes as long.
    hash_otp_code(generate_otp_code())
    if usable and not outbox:
        logger.warning("a staff password reset was requested but no email outbox is configured")


def start_reset_for(
    db: Session, *, raw_session_token: str | None, staff_id: int, language: str
) -> None:
    """An administrator makes the system email a reset code to the staff member's
    own address. Authority is checked here, inside the fence."""
    admin = authenticate_admin(db, raw_session_token)
    limiter.check([(ADMIN_RESET_START_PER_ADMIN, str(admin.id))])
    staff = db.get(StaffAccount, staff_id)
    if staff is None:
        raise AdminNotFound("No such staff account.")
    if not staff.is_active:
        raise AdminInvalid("A deactivated account cannot get a reset email.")
    if not get_settings().outbox_enabled:
        raise PasswordResetUnavailableError("Email is not configured on this server.")
    # The same per-address cap as the public request: an administrator cannot
    # be used to flood one person's inbox.
    limiter.check([(STAFF_RESET_REQUEST_PER_EMAIL, str(staff.email).lower())])
    _issue_reset_code(db, staff, language)
    _audit(
        db,
        actor_id=admin.id,
        action=AUDIT_RESET_REQUESTED,
        staff_id=staff.id,
        after={"initiated_by": "admin"},
    )


# --- set by an administrator, without email ---------------------------------------


def set_by_admin(
    db: Session, *, raw_session_token: str | None, staff_id: int, new_password: str
) -> int:
    """An administrator gives a staff account a new password and tells its owner.
    Authority is checked here, inside the fence. Returns the sessions ended."""
    admin = authenticate_admin(db, raw_session_token)
    limiter.check([(ADMIN_RESET_START_PER_ADMIN, str(admin.id))])
    staff = db.get(StaffAccount, staff_id)
    if staff is None:
        raise AdminNotFound("No such staff account.")
    if staff.role == StaffRole.ADMIN:
        raise AdminInvalid("An administrator changes their own password with the current one.")
    if not staff.is_active:
        raise AdminInvalid("A deactivated account cannot get a new password.")
    ended = _set_password(db, staff, new_password)
    _audit(
        db,
        actor_id=admin.id,
        action=AUDIT_ADMIN_SET,
        staff_id=staff.id,
        after={"sessions_revoked": ended},
    )
    return ended


# --- reset by email: finishing it -----------------------------------------------


@dataclass(frozen=True)
class ResetOutcome:
    """Like a login: a wrong code still commits its attempt counter, so failure
    is a value here and the router raises after the commit."""

    ok: bool
    sessions_ended: int = 0


def confirm_reset(db: Session, *, email: str, code: str, new_password: str) -> ResetOutcome:
    normalized = email.strip().lower()
    staff = db.execute(
        select(StaffAccount).where(StaffAccount.email == normalized).with_for_update()
    ).scalar_one_or_none()
    challenge = None
    if staff is not None and staff.is_active:
        challenge = db.execute(
            select(OtpChallenge)
            .where(
                OtpChallenge.email == normalized,
                OtpChallenge.purpose == OtpPurpose.STAFF_PASSWORD_RESET,
                OtpChallenge.consumed_at.is_(None),
            )
            .with_for_update()
        ).scalar_one_or_none()

    now = datetime.now(UTC)
    if (
        staff is None
        or challenge is None
        or challenge.expires_at <= now
        or challenge.attempt_count >= OTP_MAX_ATTEMPTS
    ):
        _spend_the_same_time(code)
        return ResetOutcome(ok=False)

    if not verify_secret(challenge.code_hash, code):
        db.execute(
            update(OtpChallenge)
            .where(OtpChallenge.id == challenge.id, OtpChallenge.consumed_at.is_(None))
            .values(attempt_count=OtpChallenge.attempt_count + 1)
        )
        return ResetOutcome(ok=False)

    consumed = db.execute(
        update(OtpChallenge)
        .where(OtpChallenge.id == challenge.id, OtpChallenge.consumed_at.is_(None))
        .values(consumed_at=now)
        .returning(OtpChallenge.id)
    ).first()
    if (
        consumed is None
    ):  # a concurrent confirmation used it first (the lock makes this unreachable)
        return ResetOutcome(ok=False)

    ended = _set_password(db, staff, new_password)
    _audit(
        db,
        actor_id=staff.id,
        action=AUDIT_RESET_DONE,
        staff_id=staff.id,
        after={"sessions_revoked": ended},
    )
    return ResetOutcome(ok=True, sessions_ended=ended)
