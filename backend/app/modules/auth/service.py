"""`POST /api/auth/otp/request` orchestration — ARCHITECTURE.md §8.2.

Resolves which purpose of code an email needs (or that it needs none
at all) and delegates the actual issuance to
`app.modules.attendees.service.issue_otp`, which owns the
`otp_challenges` table. This module owns the *resolution* — a
standalone "send me a code" request has no registration payload to
work from, only an email.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.languages import DEFAULT_LANGUAGE
from app.modules.attendees.models import OtpPurpose, User, UserStatus
from app.modules.attendees.service import PendingOtpDelivery, issue_otp


def request_otp(
    db: Session, email: str, language: str = DEFAULT_LANGUAGE
) -> PendingOtpDelivery | None:
    """Returns None when no user exists for this email — the caller
    (the router) must still return the standard generic response
    either way (ARCHITECTURE.md §8.2: "returns the same response
    whether or not the email is registered")."""
    normalized_email = email.strip().lower()
    user = db.execute(
        select(User).where(User.email == normalized_email).with_for_update()
    ).scalar_one_or_none()
    if user is None:
        return None

    purpose = OtpPurpose.ACTIVATION if user.status == UserStatus.PROVISIONAL else OtpPurpose.LOGIN
    return issue_otp(
        db, email=normalized_email, user_id=user.id, purpose=purpose, language=language
    )
