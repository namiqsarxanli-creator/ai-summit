"""The controlled interest vocabulary — ARCHITECTURE.md §7.2.

"Matching is meaningless unless both sides reference the same rows —
free-text interests are explicitly rejected for the demo."
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdentityPKMixin


class Interest(IdentityPKMixin, Base):
    __tablename__ = "interests"

    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
