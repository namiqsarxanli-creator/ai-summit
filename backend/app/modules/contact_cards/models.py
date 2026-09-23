"""The opaque public networking token — ARCHITECTURE.md §7.6, §9.

The token itself is just an opaque ``text`` column — it carries no
structure to decode, matching §9.1: "not derived from any attendee
attribute." Issuance is ``app.core.security.generate_card_token``;
resolution is ``app.modules.contact_cards.service`` (M06).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdentityPKMixin


class ContactCard(IdentityPKMixin, Base):
    __tablename__ = "contact_cards"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_contact_cards_user_id_users"), nullable=False
    )
    public_token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    __table_args__ = (
        # "an attendee has at most one LIVE card, while revoked cards
        # are retained for audit. This is what makes rotation possible
        # later without deleting history." (§7.6) — one of §7.14's nine
        # exactly-named constraints.
        Index(
            "uq_contact_card_live_user",
            "user_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
