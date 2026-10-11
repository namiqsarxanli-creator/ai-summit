"""Event configuration and the admin audit trail.

ARCHITECTURE.md §7.13 (event_config, an "added entity" per §6.2).
``admin_audit`` implements IMPLEMENTATION_PLAN.md clarification C4:
ARCHITECTURE.md §18 / §21.9 require an admin mutation audit log but §7
never names its exact table, "shaped like consent_audit (actor,
action, target, before/after, timestamp)". Deliberately minimal — one
relational table, no generic event-sourcing system.

M02 is storage only: no admin endpoints, no reset CLI (M15/M16).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, IdentityPKMixin


class EventConfig(Base):
    """Single-row event-wide configuration — ARCHITECTURE.md §7.13.

    "Bounds are enforced in the database, not only in the admin form.
    ... A CHECK makes the bad state unreachable regardless of which
    code path writes it."

    Deliberately NOT using IdentityPKMixin: this table structurally
    holds exactly one row forever (CHECK (id = 1)), so there is
    nothing for an auto-incrementing identity to generate — the single
    permitted id is a fixed literal, inserted once by the M03 seed.
    """

    __tablename__ = "event_config"

    # autoincrement=False is required, not cosmetic: SQLAlchemy's
    # default ("auto") treats any single-column Integer primary key as
    # SERIAL, which silently attached an event_config_id_seq sequence
    # here (caught by comparing the live PostgreSQL catalog against
    # this model — information_schema showed `nextval(...)` as the
    # column default). That contradicts the singleton design above: id
    # is always the fixed literal 1, inserted once by the M03 seed,
    # never generated.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)

    feedback_points: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    allow_repeat_task_points_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    leaderboard_top_n: Mapped[int] = mapped_column(Integer, nullable=False, server_default="10")
    # Monotonically incremented by demo reset (M15). bigint, not int:
    # a demo run rehearsed many times over months must never wrap.
    dataset_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    maintenance_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    updated_by_staff_id: Mapped[int | None] = mapped_column(
        ForeignKey("staff_accounts.id", name="fk_event_config_updated_by_staff_id_staff_accounts"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("feedback_points BETWEEN 1 AND 100", name="feedback_points_range"),
        CheckConstraint("leaderboard_top_n BETWEEN 1 AND 100", name="leaderboard_top_n_range"),
    )


class AdminAudit(IdentityPKMixin, Base):
    """Admin mutation audit trail — Implementation Clarification C4.

    "Every admin mutation and its audit row are written in the same
    transaction. An audit log that can be missing entries for
    committed changes is worse than none, because it is trusted."
    (ARCHITECTURE.md §18) — the SAME-TRANSACTION guarantee is a
    service-layer responsibility (M16); this is the storage contract
    it writes into.
    """

    __tablename__ = "admin_audit"

    staff_account_id: Mapped[int] = mapped_column(
        ForeignKey("staff_accounts.id", name="fk_admin_audit_staff_account_id_staff_accounts"),
        nullable=False,
    )
    # Short machine-readable action identifier, e.g. "booth.update",
    # "staff.create" — free text (an open, growing vocabulary), not an
    # enum: constraining it would mean a migration for every new admin
    # action IMPLEMENTATION_PLAN.md's M16 introduces.
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSONB is justified here for the same reason as
    # idempotency_records.response_snapshot: an opaque, append-only
    # snapshot of whatever the mutation changed, never queried by
    # field — not a substitute for relational modelling of the actual
    # business entities, which all have their own proper tables.
    before_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # The audit view (admin/audit.py) lists `(created_at DESC, id DESC)` and
    # filters by actor, action or target: each filter's index ends in that order.
    __table_args__ = (
        Index("ix_admin_audit_created_at_id", text("created_at DESC"), text("id DESC")),
        Index(
            "ix_admin_audit_actor_created_at_id",
            "staff_account_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index(
            "ix_admin_audit_action_created_at_id",
            "action",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index(
            "ix_admin_audit_target_created_at_id",
            "target_type",
            "target_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )
