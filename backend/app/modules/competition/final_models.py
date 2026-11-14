"""The final result of the competition (migration 0023).

`final_results` holds VERSIONED snapshots of the ranking taken after the 17:30
close. A Game Master (or an administrator) creates a DRAFT, reviews it and
publishes it explicitly for the 17:40 Grand Final; publishing a later version
supersedes the earlier one. The database itself keeps a snapshot immutable: its
content, version and authorship can never change, a status only moves
DRAFT -> PUBLISHED | DISCARDED and PUBLISHED -> SUPERSEDED, and a published or
superseded snapshot cannot be deleted. A correction after publication therefore
never silently changes a published result: it needs a new version.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, IdentityPKMixin


class FinalResult(IdentityPKMixin, Base):
    __tablename__ = "final_results"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("masterclass_events.id", name="fk_final_results_event_id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="DRAFT")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by_staff_id: Mapped[int] = mapped_column(
        ForeignKey("staff_accounts.id", name="fk_final_results_created_by"), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by_staff_id: Mapped[int | None] = mapped_column(
        ForeignKey("staff_accounts.id", name="fk_final_results_published_by"), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'SUPERSEDED', 'DISCARDED')", name="status_known"
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "(status IN ('PUBLISHED', 'SUPERSEDED')) = (published_at IS NOT NULL) "
            "AND (published_at IS NULL) = (published_by_staff_id IS NULL)",
            name="publication_shape",
        ),
        Index("uq_final_results_event_version", "event_id", "version", unique=True),
        Index(
            "uq_final_results_one_published",
            "event_id",
            unique=True,
            postgresql_where=text("status = 'PUBLISHED'"),
        ),
    )
