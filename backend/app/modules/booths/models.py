"""Booths and their booth-owned configuration — ARCHITECTURE.md §7.4,
§7.5, §7.9, §7.10.

Tasks and preset point values are grouped here rather than in
``points`` because they are booth-authored *configuration* an admin
manages per booth (§18: "manage tasks and preset point values"), not
part of the points ledger itself. ``points`` (M02) only holds
``PointsLog`` and ``IdempotencyRecord`` — the things ``PointsService``
actually writes.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdentityPKMixin


class Booth(IdentityPKMixin, Base):
    __tablename__ = "booths"

    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    location_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    # Nullable per ARCHITECTURE.md §7.4 / §14.5: NULL means "inherit
    # event_config.allow_repeat_task_points_default"; a non-NULL value
    # overrides the default for this booth specifically.
    allow_repeat_task_points: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class BoothInterest(Base):
    """The booth's weighted interest profile — ARCHITECTURE.md §7.5.

    The sole input to matching from the booth side. No surrogate `id`:
    the composite (booth_id, interest_id) IS the identity, matching
    "PRIMARY KEY (booth_id, interest_id)" exactly.
    """

    __tablename__ = "booth_interests"

    booth_id: Mapped[int] = mapped_column(
        ForeignKey("booths.id", name="fk_booth_interests_booth_id_booths"), primary_key=True
    )
    interest_id: Mapped[int] = mapped_column(
        ForeignKey("interests.id", name="fk_booth_interests_interest_id_interests"),
        primary_key=True,
    )
    # Decimal, not float: numeric(3,2) round-trips exactly, and matching
    # sums these weights (ARCHITECTURE.md §11.3 — no float drift).
    weight: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)

    __table_args__ = (CheckConstraint("weight > 0 AND weight <= 1", name="weight_range"),)


class Task(IdentityPKMixin, Base):
    """A descriptive activity at a booth — ARCHITECTURE.md §7.9.

    "It is descriptive, not the authorization object — the staff flow
    in §13 awards points without selecting a task, so
    points_log.task_id is nullable."
    """

    __tablename__ = "tasks"

    booth_id: Mapped[int] = mapped_column(
        ForeignKey("booths.id", name="fk_tasks_booth_id_booths"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    __table_args__ = (
        # Exists SOLELY so points_log can declare the composite FK
        # (task_id, booth_id) -> tasks (id, booth_id) — ARCHITECTURE.md
        # §7.12: "Without it, a row could reference a task belonging to
        # a different booth, and nothing in the schema would object."
        UniqueConstraint("id", "booth_id", name="uq_tasks_id_booth_id"),
    )


class TaskPresetPoint(IdentityPKMixin, Base):
    """The server-side award-value allow-list — ARCHITECTURE.md §7.10.

    booth_id IS NULL rows are the event-wide defaults; a booth with
    its own rows overrides the defaults entirely (resolved in the
    service layer, not here — M02 is storage only).
    """

    __tablename__ = "task_preset_points"

    booth_id: Mapped[int | None] = mapped_column(
        ForeignKey("booths.id", name="fk_task_preset_points_booth_id_booths"), nullable=True
    )
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    __table_args__ = (
        CheckConstraint("points > 0 AND points <= 100", name="points_range"),
        # Corrected (review finding, ARCHITECTURE.md §7.10): a plain
        # UNIQUE(booth_id, points) does NOT deduplicate the global
        # rows, because NULL is never equal to NULL in SQL. Two
        # PARTIAL UNIQUE INDEXES express the actual intent instead —
        # note this must be Index(unique=True, postgresql_where=...),
        # not UniqueConstraint(postgresql_where=...): PostgreSQL table
        # CONSTRAINTs cannot carry a WHERE clause at all (only INDEXes
        # can), so a UniqueConstraint here would silently become a
        # full, non-partial unique constraint and reintroduce the
        # exact NULL-collision bug this comment describes.
        Index(
            "uq_task_preset_points_booth_id_points",
            "booth_id",
            "points",
            unique=True,
            postgresql_where=text("booth_id IS NOT NULL"),
        ),
        Index(
            "uq_preset_global_points",
            "points",
            unique=True,
            postgresql_where=text("booth_id IS NULL"),
        ),
    )
