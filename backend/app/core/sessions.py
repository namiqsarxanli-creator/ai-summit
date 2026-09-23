"""Opaque server-side sessions — ARCHITECTURE.md §8.3.

The browser only ever holds the raw token, in an HttpOnly cookie; the
database only ever holds its SHA-256 hash. Everything that creates,
resolves or revokes a session goes through this module, so those rules
live in exactly one place.

`resolve_attendee_session` is also the contract M14's SSE streams will
call on every heartbeat (§8.3 "Session death must propagate to open
streams", §17.9): the same function, so a revoked or expired session
fails the same way everywhere.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.auth_session import AuthSession, PrincipalType
from app.core.config import get_settings
from app.core.errors import RegistrationNotVerifiedError, UnauthenticatedError
from app.core.security import generate_session_token, hash_session_token
from app.modules.attendees.models import User, UserStatus

# No architecture-mandated name exists for the cookie (only its
# attributes, §8.3) — fixed once here for every reader and writer.
SESSION_COOKIE_NAME = "session"
SESSION_TTL = timedelta(hours=12)


def create_attendee_session(db: Session, *, user_id: int, now: datetime) -> str:
    """Returns the raw token for the cookie; persists only its hash."""
    raw_token = generate_session_token()
    db.add(
        AuthSession(
            token_hash=hash_session_token(raw_token),
            principal_type=PrincipalType.ATTENDEE,
            user_id=user_id,
            issued_at=now,
            expires_at=now + SESSION_TTL,
        )
    )
    db.flush()
    return raw_token


def resolve_attendee_session(db: Session, raw_token: str | None) -> User:
    """The authenticated attendee, or an exception — never a guess.

    Missing, unknown, expired, revoked, or a session of another
    principal type are all UNAUTHENTICATED (§20.3). A valid session
    whose attendee is not ACTIVE is REGISTRATION_NOT_VERIFIED (§8.1:
    no capability before email ownership is proven). Expiry is checked
    against the database clock, like OTP expiry (§8.2).

    Read-only: pure reads take no reset fence (§19.4). A mutation that
    needs the attendee calls this INSIDE its own mutating_transaction.
    """
    if not raw_token:
        raise UnauthenticatedError("Please sign in.")
    user = db.execute(
        select(User)
        .join(AuthSession, AuthSession.user_id == User.id)
        .where(
            AuthSession.token_hash == hash_session_token(raw_token),
            AuthSession.principal_type == PrincipalType.ATTENDEE,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > func.now(),
        )
    ).scalar_one_or_none()
    if user is None:
        raise UnauthenticatedError("Please sign in.")
    if user.status != UserStatus.ACTIVE:
        raise RegistrationNotVerifiedError("Please verify your email first.")
    return user


def revoke_session(db: Session, raw_token: str | None) -> bool:
    """§8.3: "Logout revokes server-side (revoked_at), so the token is
    dead immediately." Any principal type — whoever holds the cookie is
    ending their own session. Returns whether a live session was
    revoked; revoking an absent or dead one is a harmless no-op."""
    if not raw_token:
        return False
    revoked = db.execute(
        update(AuthSession)
        .where(
            AuthSession.token_hash == hash_session_token(raw_token),
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=func.now())
        .returning(AuthSession.id)
    ).first()
    return revoked is not None


def set_session_cookie(response: Response, raw_token: str) -> None:
    """§8.3: `HttpOnly; SameSite=Lax; Path=/; Secure (when served over
    HTTPS); Max-Age = 12h`."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        samesite="lax",
        secure=get_settings().serves_https,
        path="/",
        max_age=int(SESSION_TTL.total_seconds()),
    )


def clear_session_cookie(response: Response) -> None:
    # Same attributes as when it was set, or some browsers keep it.
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=get_settings().serves_https,
    )
