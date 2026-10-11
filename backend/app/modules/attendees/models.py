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
    LargeBinary,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as PgEnum
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
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
    # A STAFF member's password-reset code (migration 0013). A kind of its own:
    # an attendee's activation / login code never verifies it, and it never
    # verifies one of those - every query matches on purpose.
    STAFF_PASSWORD_RESET = "STAFF_PASSWORD_RESET"


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
    # Optimistic concurrency of the attendee's own profile edits (migration
    # 0014): the client sends the version it loaded, the edit is accepted only
    # while it is still current, and each accepted edit increments it.
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # The server-made random name of the profile photo's files (see
    # app/core/media.py); NULL = no photo. Unique, so a key belongs to one row.
    photo_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "photo_key IS NULL OR photo_key ~ '^[0-9a-f]{32}$'", name="photo_key_format"
        ),
        Index(
            "uq_users_photo_key",
            "photo_key",
            unique=True,
            postgresql_where=sql_text("photo_key IS NOT NULL"),
        ),
        CheckConstraint(
            "sponsor_consent = false OR sponsor_consent_at IS NOT NULL",
            name="sponsor_consent_requires_timestamp",
        ),
        CheckConstraint(
            "status = 'PROVISIONAL' OR activated_at IS NOT NULL",
            name="status_requires_activated_at",
        ),
        # The admin attendee list: newest registration first, keyset-paged on
        # (created_at, id) - the unique id makes the order total.
        Index("ix_users_created_at_id", sql_text("created_at DESC"), sql_text("id DESC")),
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
    # Binds an ACTIVATION code to the browser that submitted the registration
    # (migration 0015). `registration_intent_hash` is the SHA-256 of a random
    # value held only in that browser's HttpOnly cookie; `registration_snapshot`
    # is what that submission entered. At verification the snapshot whose intent
    # the verifying browser presents is what gets activated - so someone who
    # re-submits a pending registration from another browser cannot make the
    # owner activate their data or a sponsor consent the owner never gave.
    registration_intent_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    registration_snapshot: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)

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


class OtpDeliveryStatus(enum.StrEnum):
    """Lifecycle of one OTP email in the delivery outbox.

    `ACCEPTED` means the email PROVIDER took the message (HTTP 2xx). It does
    not mean the message reached an inbox - bounces, spam placement and
    delays happen after that and are invisible here.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    ACCEPTED = "accepted"
    FAILED = "failed"
    EXPIRED = "expired"


OTP_LIVE_STATUSES = (OtpDeliveryStatus.PENDING.value, OtpDeliveryStatus.PROCESSING.value)
OTP_LANGUAGES = ("az", "en", "ru")


class OtpDelivery(IdentityPKMixin, Base):
    """Durable outbox row for one OTP email (app/modules/auth/otp_outbox.py).

    Written in the SAME transaction as its `otp_challenges` row, so a
    rolled-back registration/login leaves nothing to send and a committed
    one can never lose its email. A separate worker process sends it.

    The code is held only as AES-256-GCM ciphertext (`payload_ciphertext`),
    and only while the delivery is live: the database itself refuses a
    ciphertext on a finished row. Verification still uses the Argon2id
    hash on `otp_challenges`; nothing here is consulted for it.
    """

    __tablename__ = "otp_deliveries"

    challenge_id: Mapped[int] = mapped_column(
        ForeignKey(
            "otp_challenges.id",
            name="fk_otp_deliveries_challenge_id_otp_challenges",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    purpose: Mapped[OtpPurpose] = mapped_column(
        PgEnum(OtpPurpose, name="otp_purpose", native_enum=True, create_type=False),
        nullable=False,
    )
    language: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Processing lease: a worker that dies leaves `locked_until` in the past
    # and another worker reclaims the row.
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(Text)
    # Fixed at creation, sent with EVERY attempt, so a retry after an
    # ambiguous timeout cannot make the provider send the email twice.
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    payload_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    payload_key_id: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    # A short machine code, never provider text. See otp_outbox.ERROR_MESSAGES.
    reason_code: Mapped[str | None] = mapped_column(Text)
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Copy of the challenge's expiry: after this the code must not be sent.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("challenge_id"),
        UniqueConstraint("idempotency_key"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'accepted', 'failed', 'expired')",
            name="status_valid",
        ),
        CheckConstraint("language IN ('az', 'en', 'ru')", name="language_valid"),
        # The code may be stored only while the delivery is live...
        CheckConstraint(
            "(status IN ('pending', 'processing')) = (payload_ciphertext IS NOT NULL)",
            name="payload_only_while_live",
        ),
        CheckConstraint(
            "(status = 'processing') = (locked_until IS NOT NULL)", name="lease_iff_processing"
        ),
        CheckConstraint("(locked_until IS NULL) = (locked_by IS NULL)", name="lease_complete"),
        CheckConstraint(
            "(status = 'accepted') = (accepted_at IS NOT NULL)", name="accepted_has_timestamp"
        ),
        CheckConstraint(
            "(status IN ('accepted', 'failed', 'expired')) = (finished_at IS NOT NULL)",
            name="finished_has_timestamp",
        ),
        Index(
            "ix_otp_deliveries_pending_due",
            "next_attempt_at",
            postgresql_where=sql_text("status = 'pending'"),
        ),
        Index(
            "ix_otp_deliveries_processing_lease",
            "locked_until",
            postgresql_where=sql_text("status = 'processing'"),
        ),
        Index("ix_otp_deliveries_status_id", "status", sql_text("id DESC")),
    )
