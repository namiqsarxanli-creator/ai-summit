"""Booth staff and admin accounts — ARCHITECTURE.md §7.7.

"It is not representable in this schema for a booth staff account to
lack a booth." No self-registration endpoint exists anywhere in the
API surface (§12.1) — M02 is storage only; login lands in M09.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Text
from sqlalchemy import Enum as PgEnum
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import conv

from app.db.base import Base, IdentityPKMixin


class StaffRole(enum.StrEnum):
    BOOTH_STAFF = "BOOTH_STAFF"
    ADMIN = "ADMIN"


class StaffAccount(IdentityPKMixin, Base):
    __tablename__ = "staff_accounts"

    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    # Argon2id encoded string. NEVER a plaintext password — ARCHITECTURE.md
    # §7.7 / §21.1.
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[StaffRole] = mapped_column(
        PgEnum(StaffRole, name="staff_role", native_enum=True), nullable=False
    )
    booth_id: Mapped[int | None] = mapped_column(
        ForeignKey("booths.id", name="fk_staff_accounts_booth_id_booths"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # The structural half of the booth invariant (ARCHITECTURE.md
        # §7.7, §12.2). This is one of the nine constraints §7.14 names
        # EXACTLY: "ck_staff_role_booth". A plain `name="ck_staff_role_booth"`
        # is NOT enough — the metadata naming convention's "ck" template
        # is `ck_%(table_name)s_%(constraint_name)s`, and for CHECK
        # constraints that template's %(constraint_name)s token is
        # filled from whatever `name=` you pass, then WRAPPED again
        # with the table-name prefix — producing the broken
        # `ck_staff_accounts_ck_staff_role_booth` (verified empirically
        # via a throwaway autogenerate run). `conv(...)` is SQLAlchemy's
        # documented escape hatch: it marks the string as an
        # already-final name, so the convention leaves it untouched.
        CheckConstraint(
            "(role = 'BOOTH_STAFF' AND booth_id IS NOT NULL) OR "
            "(role = 'ADMIN' AND booth_id IS NULL)",
            name=conv("ck_staff_role_booth"),
        ),
    )
