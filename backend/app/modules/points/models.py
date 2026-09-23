"""PointsLog — the source of truth — and idempotency storage.

ARCHITECTURE.md §7.12, §7.12.1. M02 is storage only: no PointsService,
no award endpoint (M11). ``users.total_points`` does not exist anywhere
in this schema, by design — a score is always ``SUM(points_log.points)``
(§16.1), computed at read time.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as PgEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, IdentityPKMixin


class PointsSource(enum.StrEnum):
    TASK = "TASK"
    FEEDBACK = "FEEDBACK"


class PointsEntryType(enum.StrEnum):
    AWARD = "AWARD"
    REVERSAL = "REVERSAL"


class PointsLog(IdentityPKMixin, Base):
    """The sole source of truth for points (ARCHITECTURE.md §14.1,
    invariant #1). Append-only in application code — no UPDATE, no
    DELETE; a correction is a new REVERSAL row (§7.12).

    ``id`` is an IDENTITY, never a commit-order cursor — see the
    warning in ARCHITECTURE.md §7.12 / invariant #22. Nothing in this
    model, or anywhere else in M02, treats it as one.
    """

    __tablename__ = "points_log"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_points_log_user_id_users"), nullable=False
    )
    booth_id: Mapped[int | None] = mapped_column(
        ForeignKey("booths.id", name="fk_points_log_booth_id_booths"), nullable=True
    )
    source: Mapped[PointsSource] = mapped_column(
        PgEnum(PointsSource, name="points_source", native_enum=True), nullable=False
    )
    entry_type: Mapped[PointsEntryType] = mapped_column(
        PgEnum(PointsEntryType, name="points_entry_type", native_enum=True),
        nullable=False,
        server_default=PointsEntryType.AWARD.value,
    )
    # Positive for AWARD, negative for REVERSAL — enforced by the
    # entry_type_consistency CHECK below, not merely by convention.
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reverses_points_log_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entered_by_staff_id: Mapped[int | None] = mapped_column(
        ForeignKey("staff_accounts.id", name="fk_points_log_entered_by_staff_id_staff_accounts"),
        nullable=True,
    )
    entered_by_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # TASK repeat-policy de-duplication only. NULL for FEEDBACK (that
    # uniqueness is structural, see uq_points_feedback_user_booth
    # below) and for REVERSAL rows (§14.5: "a reversal must never be
    # blocked by the policy of the row it reverses").
    dedupe_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("points <> 0", name="points_nonzero"),
        CheckConstraint(
            "(entry_type = 'AWARD' AND points > 0 AND reverses_points_log_id IS NULL) OR "
            "(entry_type = 'REVERSAL' AND points < 0 AND reverses_points_log_id IS NOT NULL)",
            name="entry_type_consistency",
        ),
        CheckConstraint(
            "(source = 'TASK' AND booth_id IS NOT NULL "
            "AND entered_by_staff_id IS NOT NULL AND entered_by_system = false) OR "
            "(source = 'FEEDBACK' AND booth_id IS NOT NULL "
            "AND entered_by_staff_id IS NULL AND entered_by_system = true)",
            name="source_attribution",
        ),
        # Structural feedback-reward uniqueness (F, ARCHITECTURE.md
        # §7.12) — NOT string-based like dedupe_key, so a bug in key
        # construction can never disable the invariant it protects.
        # Restricted to entry_type='AWARD' so a REVERSAL of a feedback
        # award frees the index again, correctly making the attendee
        # eligible once more rather than being blocked by their own
        # reversed row.
        Index(
            "uq_points_feedback_user_booth",
            "user_id",
            "booth_id",
            unique=True,
            postgresql_where=text("source = 'FEEDBACK' AND entry_type = 'AWARD'"),
        ),
        # Named exactly per §7.14 — the naming convention would
        # otherwise render the table name in full ("points_log"); the
        # documented name shortens it to "points".
        UniqueConstraint("dedupe_key", name="uq_points_dedupe_key"),
        UniqueConstraint("reverses_points_log_id", name="uq_points_reverses"),
        ForeignKeyConstraint(
            ["reverses_points_log_id"],
            ["points_log.id"],
            name="fk_points_log_reverses_points_log_id_points_log",
        ),
        # "Without it, a row could reference a task belonging to a
        # DIFFERENT booth, and nothing in the schema would object."
        # (§7.12) — named exactly per §7.14.
        ForeignKeyConstraint(
            ["task_id", "booth_id"],
            ["tasks.id", "tasks.booth_id"],
            name="fk_points_task_booth",
        ),
        Index("ix_points_log_user_id", "user_id"),
        Index("ix_points_log_user_id_created_at", "user_id", text("created_at DESC")),
        Index("ix_points_log_booth_id_created_at", "booth_id", text("created_at DESC")),
    )


class IdempotencyRecord(Base):
    """Award idempotency storage — ARCHITECTURE.md §7.12.1.

    NO status column and NO IN_PROGRESS state, by design: the record
    is claimed and filled inside ONE transaction together with the
    points_log insert, so no other transaction can ever observe a
    half-finished record. ``points_log_id`` / ``response_snapshot``
    are nullable ONLY because they are written a few statements after
    the claim, within that same transaction (§7.12.1) — never because
    an incomplete-but-visible state is a real possibility.
    """

    __tablename__ = "idempotency_records"

    staff_account_id: Mapped[int] = mapped_column(
        ForeignKey(
            "staff_accounts.id", name="fk_idempotency_records_staff_account_id_staff_accounts"
        ),
        primary_key=True,
    )
    # Client-supplied, but the key is scoped to (staff_account_id,
    # idempotency_key) — "one staff member's key can never collide
    # with another's, and a key cannot be replayed across accounts."
    idempotency_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    points_log_id: Mapped[int | None] = mapped_column(
        ForeignKey("points_log.id", name="fk_idempotency_records_points_log_id_points_log"),
        nullable=True,
    )
    # The exact response body originally returned, replayed verbatim
    # on a repeat request — ARCHITECTURE.md §13.4's Option A. JSONB is
    # justified here (not "avoiding relational modelling"): this is an
    # append-only, opaque snapshot that is never queried by field,
    # only ever fetched whole and replayed byte-for-byte.
    response_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
