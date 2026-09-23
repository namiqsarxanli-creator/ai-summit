"""Booth feedback — ARCHITECTURE.md §7.11.

Deliberately NOT unique on (user_id, booth_id): "feedback is rewarded
once per booth, not... may only be submitted once. Multiple
submissions are allowed and stored; only the reward is de-duplicated"
— that de-duplication is structural on points_log
(uq_points_feedback_user_booth, see app/modules/points/models.py), not
here. M02 is storage only; the submission/reward flow is M12.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, SmallInteger, Text, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, IdentityPKMixin


class Feedback(IdentityPKMixin, Base):
    __tablename__ = "feedback"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_feedback_user_id_users"), nullable=False
    )
    booth_id: Mapped[int] = mapped_column(
        ForeignKey("booths.id", name="fk_feedback_booth_id_booths"), nullable=False
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5", name="rating_range"),
        Index("ix_feedback_booth_id_created_at", "booth_id", text("created_at DESC")),
        Index("ix_feedback_user_id_booth_id", "user_id", "booth_id"),
    )
