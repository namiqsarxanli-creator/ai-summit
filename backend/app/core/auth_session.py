"""Server-side sessions — ARCHITECTURE.md §7.8.

Named ``auth_session.py`` (table ``sessions``) and the model class
``AuthSession`` rather than ``Session`` to avoid colliding with
``sqlalchemy.orm.Session`` (the ORM's own unit-of-work object), which
``app/db/session.py`` already imports under that exact name.

Lives in ``app/core/`` rather than inside the ``attendees`` or
``staff`` domain module because it is genuinely shared by both:
ARCHITECTURE.md §5 places session RESOLUTION (``current_attendee`` /
``current_staff`` / ``current_admin``) in ``core/deps.py``, and one
attendee session and one staff session are rows of the exact same
table, distinguished only by ``principal_type`` — putting the model
under either domain module would be an arbitrary choice the
architecture itself does not make.

"Only the hash of the session token is stored. A database leak must
not yield usable sessions." M02 is storage only — no login/logout
endpoint exists yet (M05/M09).
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text
from sqlalchemy import Enum as PgEnum
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdentityPKMixin


class PrincipalType(enum.StrEnum):
    ATTENDEE = "ATTENDEE"
    STAFF = "STAFF"


class AuthSession(IdentityPKMixin, Base):
    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    principal_type: Mapped[PrincipalType] = mapped_column(
        PgEnum(PrincipalType, name="principal_type", native_enum=True), nullable=False
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_sessions_user_id_users"), nullable=True
    )
    staff_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("staff_accounts.id", name="fk_sessions_staff_account_id_staff_accounts"),
        nullable=True,
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(user_id, staff_account_id) = 1",
            name="exactly_one_principal",
        ),
        CheckConstraint(
            "(principal_type = 'ATTENDEE' AND user_id IS NOT NULL) OR "
            "(principal_type = 'STAFF' AND staff_account_id IS NOT NULL)",
            name="principal_type_matches_reference",
        ),
        Index("ix_sessions_expires_at", "expires_at"),
    )
