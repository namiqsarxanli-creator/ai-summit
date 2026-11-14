"""The fictional attendees are optional even within the demo seed.

Their contact-card tokens are fixed and published in the repository
(ARCHITECTURE.md §19.8) — acceptable only for demo identities. Whether
the demo seed runs at all is covered by test_real_bootstrap.py."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from app.db.session import SessionLocal
from app.seed.seeder import seed_all


def test_seed_all_without_demo_attendees_creates_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Against a database with the demo attendees removed (a real
    deployment), re-running the seed must not bring them back. Runs
    inside one transaction that is always rolled back: `seed_all`'s own
    commit is turned into a flush, so the shared suite database is never
    altered."""
    db = SessionLocal()
    try:
        monkeypatch.setattr(db, "commit", db.flush)
        db.execute(text("DELETE FROM contact_cards"))
        db.execute(text("DELETE FROM user_interests"))
        db.execute(text("DELETE FROM users WHERE email LIKE '%@attendee.facemark.demo'"))
        result = seed_all(db, admin_password="a", staff_password="s", include_demo_attendees=False)
        assert result.attendees_created == 0
        assert result.contact_cards_created == 0
        remaining: Any = db.execute(
            text("SELECT count(*) FROM users WHERE email LIKE '%@attendee.facemark.demo'")
        ).scalar_one()
        assert remaining == 0
    finally:
        db.rollback()
        db.close()
