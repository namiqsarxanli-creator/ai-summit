"""Attendee identity, lifecycle, interests, consent and OTP challenges.

ARCHITECTURE.md §7.1 (users), §7.3 (user_interests), §8.1 (the
PROVISIONAL -> ACTIVE lifecycle, F06), §8.2 (otp_challenges), and §7.1
again for consent_audit.

M02 is schema only: no registration endpoint, no OTP endpoint, no
email delivery. See IMPLEMENTATION_PLAN.md M02 "Explicitly Out of
Scope".
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy import Enum as PgEnum
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, IdentityPKMixin


class UserStatus(enum.StrEnum):
    """ARCHITECTURE.md §8.1:

        PROVISIONAL ──(OTP verified)──► ACTIVE

    A PROVISIONAL user older than 24h is purged by
    `python -m app.cli purge-provisional` (M04+), not transitioned.
    """

    PROVISIONAL = "PROVISIONAL"
    ACTIVE = "ACTIVE"


class OtpPurpose(enum.StrEnum):
    """Keeps an activation code from being replayed as a login code,
    or vice versa (ARCHITECTURE.md §8.2)."""

    ACTIVATION = "ACTIVATION"
    LOGIN = "LOGIN"


class User(IdentityPKMixin, Base):
    """The attendee identity and profile — ARCHITECTURE.md §7.1, §8.1."""

    __tablename__ = "users"

    first_name: Mapped[str] = mapped_column(Text, nullable=False)
    last_name: Mapped[str] = mapped_column(Text, nullable=False)
    # citext: case-insensitive equality/uniqueness at the database
    # level, so "Alice@X.com" and "alice@x.com" collide on the same
    # UNIQUE constraint — required by §8.1's "Email normalised (trim,
    # lowercase); citext uniqueness enforced by the database".
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    profession: Mapped[str] = mapped_column(Text, nullable=False)
    linkedin_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    sponsor_consent: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sponsor_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sponsor_consent_version: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[UserStatus] = mapped_column(
        PgEnum(UserStatus, name="user_status", native_enum=True),
        nullable=False,
        server_default=UserStatus.PROVISIONAL.value,
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "sponsor_consent = false OR sponsor_consent_at IS NOT NULL",
            name="sponsor_consent_requires_timestamp",
        ),
        CheckConstraint(
            "status = 'PROVISIONAL' OR activated_at IS NOT NULL",
            name="status_requires_activated_at",
        ),
    )


class UserInterest(Base):
    """One attendee's interest selection — ARCHITECTURE.md §7.3.

    Composite PK "prevents duplicate selection at the database level."
    """

    __tablename__ = "user_interests"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_user_interests_user_id_users", ondelete="CASCADE"),
        primary_key=True,
    )
    interest_id: Mapped[int] = mapped_column(
        ForeignKey("interests.id", name="fk_user_interests_interest_id_interests"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_user_interests_interest_id", "interest_id"),)


class ConsentAudit(IdentityPKMixin, Base):
    """Independently auditable sponsor-consent history — ARCHITECTURE.md
    §7.1: "the current value alone cannot show a later withdrawal."
    """

    __tablename__ = "consent_audit"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_consent_audit_user_id_users"), nullable=False
    )
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    consent_version: Mapped[str] = mapped_column(Text, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # What triggered the change, e.g. "registration", "settings_update"
    # — free text (an open, ever-growing vocabulary), matching how
    # admin_audit.action is modelled (app/modules/admin/models.py).
    source: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_consent_audit_user_id_changed_at", "user_id", "changed_at"),)


class OtpChallenge(IdentityPKMixin, Base):
    """A single OTP code's lifecycle — ARCHITECTURE.md §8.2.

    Hash storage only, 10-minute expiry, single use, 5-attempt cap;
    the concurrency-safe verification transaction (SELECT ... FOR
    UPDATE) is M04 service-layer work — this is the storage contract.
    """

    __tablename__ = "otp_challenges"

    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_otp_challenges_user_id_users"), nullable=True
    )
    purpose: Mapped[OtpPurpose] = mapped_column(
        PgEnum(OtpPurpose, name="otp_purpose", native_enum=True), nullable=False
    )
    # Argon2id encoded hash of the 6-digit code. Never plaintext —
    # ARCHITECTURE.md §8.2: "storing it in plaintext makes a database
    # read an account takeover."
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    request_ip: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # "at most one live challenge per email and purpose, so
        # 'supersede the previous code' is enforced by the database
        # rather than by a best-effort update." (§8.2)
        Index(
            "uq_otp_challenges_email_purpose_live",
            "email",
            "purpose",
            unique=True,
            postgresql_where=sql_text("consumed_at IS NULL"),
        ),
    )
