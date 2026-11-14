"""Creating the first administrator from the command line.

There is no self-registration anywhere in the API (ARCHITECTURE.md
§12.1), so a brand-new deployment needs one out-of-band way to get its
first ADMIN account — this is it. The password is never an argument
here that a caller could log: it is a parameter of one function, hashed
with Argon2id before it touches the database, and `app.cli` collects it
from a hidden interactive prompt.

Idempotent by design: an existing account with the same email is left
completely untouched — its password hash, name, role and active flag
are never rewritten. Changing an admin's password is deliberately not
this command's job.
"""

from __future__ import annotations

from dataclasses import dataclass

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.modules.staff.models import StaffAccount, StaffRole

MIN_PASSWORD_LENGTH = 12  # same floor as the admin console's StaffCreate
MAX_PASSWORD_LENGTH = 256
MAX_NAME_LENGTH = 200


class InitialAdminError(ValueError):
    """The request is invalid; nothing was written."""


@dataclass(frozen=True)
class InitialAdminResult:
    email: str
    created: bool


def normalise_email(raw: str) -> str:
    try:
        return validate_email(raw.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise InitialAdminError(f"not a valid email address: {exc}") from exc


def normalise_name(raw: str) -> str:
    name = raw.strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        raise InitialAdminError(f"the name must be 1-{MAX_NAME_LENGTH} characters")
    return name


def validate_password(password: str) -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise InitialAdminError(
            f"the password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters long"
        )


def find_account(db: Session, email: str) -> StaffAccount | None:
    return db.execute(
        select(StaffAccount).where(StaffAccount.email == normalise_email(email))
    ).scalar_one_or_none()


def ensure_not_a_conflicting_account(existing: StaffAccount) -> None:
    if existing.role != StaffRole.ADMIN:
        raise InitialAdminError(
            f"{existing.email} already exists as a {existing.role.value} account; "
            "refusing to touch it. Choose another email."
        )


def create_initial_admin(
    db: Session, *, email: str, full_name: str, password: str
) -> InitialAdminResult:
    """Creates the ADMIN account if its email is free. Does not commit.

    Returns ``created=False`` and writes nothing when an ADMIN with this
    email already exists; raises `InitialAdminError` (also without
    writing) when the email belongs to a non-admin account."""
    address = normalise_email(email)
    name = normalise_name(full_name)

    existing = find_account(db, address)
    if existing is not None:
        ensure_not_a_conflicting_account(existing)
        return InitialAdminResult(email=address, created=False)

    validate_password(password)
    inserted = db.execute(
        pg_insert(StaffAccount)
        .values(
            email=address,
            password_hash=hash_password(password),
            full_name=name,
            role=StaffRole.ADMIN,
            booth_id=None,
            is_active=True,
        )
        .on_conflict_do_nothing(index_elements=["email"])
        .returning(StaffAccount.id)
    ).all()
    # Empty means a concurrent run created it between our SELECT and
    # INSERT; the unique key kept it to one row, and we leave theirs be.
    return InitialAdminResult(email=address, created=bool(inserted))
