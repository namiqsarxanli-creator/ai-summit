"""Beyond Now Photo: pair photos at the Connection Hub (migration 0025).

PDF ("Beyond Now Photo"): connected pairs take a photo in front of the checkered
wall and upload it; when BOTH agree, it runs on the main screen. No points.

* A pair photo is separate from either person's profile photo: its own random
  key, its own row, the same safe image pipeline (re-encoded JPEG renditions,
  metadata stripped) and the same media volume and backups.
* Only a CONNECTED pair (both scanned each other's QR) can share one; the
  uploader names the other person, who must ACCEPT it before it is theirs too.
* The big screen shows a photo only when both people have given their OWN
  screen consent (each can withdraw it any time) AND a moderator approved it.
  Sponsor consent is a different thing and is never read here.
* Either person can withdraw the photo: its files are deleted at once and the
  key cleared; the row stays as a record that nothing may be shown.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, IdentityPKMixin


class PairPhoto(IdentityPKMixin, Base):
    __tablename__ = "pair_photos"

    uploader_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_pair_photos_uploader_id"), nullable=False
    )
    partner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_pair_photos_partner_id"), nullable=False
    )
    photo_key: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="PENDING")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_pair_photos_withdrawn_by_id"), nullable=True
    )
    uploader_screen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    partner_screen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    moderation: Mapped[str] = mapped_column(Text, nullable=False, server_default="PENDING")
    moderated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    moderated_by_staff_id: Mapped[int | None] = mapped_column(
        ForeignKey("staff_accounts.id", name="fk_pair_photos_moderated_by"), nullable=True
    )

    __table_args__ = (
        CheckConstraint("uploader_id <> partner_id", name="not_self"),
        CheckConstraint(
            "status IN ('PENDING', 'ACCEPTED', 'DECLINED', 'WITHDRAWN')", name="status_known"
        ),
        CheckConstraint(
            "moderation IN ('PENDING', 'APPROVED', 'REJECTED')", name="moderation_known"
        ),
        # A withdrawn or declined photo has no files any more.
        CheckConstraint(
            "status NOT IN ('WITHDRAWN', 'DECLINED') OR photo_key IS NULL", name="gone_has_no_key"
        ),
        CheckConstraint(
            "(status = 'WITHDRAWN') = (withdrawn_at IS NOT NULL)", name="withdrawn_has_time"
        ),
        CheckConstraint("photo_key IS NULL OR photo_key ~ '^[0-9a-f]{32}$'", name="key_shape"),
        Index(
            "ix_pair_photos_screen",
            "moderation",
            "created_at",
            postgresql_where=text("status = 'ACCEPTED'"),
        ),
        Index("ix_pair_photos_partner", "partner_id", "status"),
        Index("ix_pair_photos_uploader", "uploader_id", "status"),
    )
