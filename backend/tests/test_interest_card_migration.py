"""Migration 0016 (company and interest card) on a database that already holds
attendees, their older interests and the booth weights built on them.

A scratch database at 0015 (tests/suite_db.py) is planted the way the code
before this revision wrote it, upgraded and downgraded: no existing row may
change meaning, no older interest selection or booth weight may be touched, old
rows get an empty card and no company, and the models and migrations agree.
"""

from __future__ import annotations

import psycopg
import pytest

from tests import suite_db
from tests.test_admin_lists_migration import _alembic

BEFORE = "0015_registration_intent"
NEW_COLUMNS = {"company", "card_sector", "card_topics", "card_seeking", "card_offering"}


def _plant(dsn: str) -> list[int]:
    ids: list[int] = []
    with psycopg.connect(dsn) as conn:
        interest_ids = [
            int(r[0])
            for r in conn.execute(
                "INSERT INTO interests (slug, name, sort_order, is_active) VALUES "
                "('old-a', 'Old A', 1, true), ('old-b', 'Old B', 2, true), "
                "('old-c', 'Old C', 3, true), ('old-d', 'Old D', 4, true), "
                "('old-e', 'Old E', 5, false) RETURNING id"
            ).fetchall()
        ]
        booth = conn.execute(
            "INSERT INTO booths (slug, name) VALUES ('old-booth', 'Old Booth') RETURNING id"
        ).fetchone()
        assert booth is not None
        for interest_id in interest_ids[:2]:
            conn.execute(
                "INSERT INTO booth_interests (booth_id, interest_id, weight) VALUES (%s, %s, 0.75)",
                (booth[0], interest_id),
            )
        for index in range(2):
            row = conn.execute(
                "INSERT INTO users (first_name, last_name, email, phone, profession, "
                "sponsor_consent, status, activated_at) VALUES (%s, 'Old', %s, '+1', "
                "'Engineer', false, 'ACTIVE', now()) RETURNING id",
                (f"Old{index}", f"old{index}@x.test"),
            ).fetchone()
            assert row is not None
            ids.append(int(row[0]))
        # The first attendee holds all five older interests - more than the card's 3.
        for interest_id in interest_ids:
            conn.execute(
                "INSERT INTO user_interests (user_id, interest_id) VALUES (%s, %s)",
                (ids[0], interest_id),
            )
        conn.commit()
    return ids


def _snapshot(dsn: str) -> tuple[list[tuple[object, ...]], ...]:
    with psycopg.connect(dsn) as conn:
        return (
            conn.execute(
                "SELECT id, first_name, email::text, profession, sponsor_consent, "
                "status::text, profile_version FROM users ORDER BY id"
            ).fetchall(),
            conn.execute(
                "SELECT user_id, interest_id FROM user_interests ORDER BY 1, 2"
            ).fetchall(),
            conn.execute("SELECT slug, name, is_active FROM interests ORDER BY slug").fetchall(),
            conn.execute(
                "SELECT booth_id, interest_id, weight FROM booth_interests ORDER BY 1, 2"
            ).fetchall(),
        )


def _columns(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        return {
            str(r[0])
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'"
            )
        }


def test_0016_adds_the_card_and_keeps_every_attendee_and_interest() -> None:
    with suite_db.scratch_database("card", migrate_to=BEFORE) as scratch:
        dsn = scratch.psycopg_url
        ids = _plant(dsn)
        before = _snapshot(dsn)
        assert not (NEW_COLUMNS & _columns(dsn))

        _alembic(scratch.url, "upgrade", "head")
        assert "(head)" in _alembic(scratch.url, "current")
        assert _columns(dsn) >= NEW_COLUMNS
        assert _snapshot(dsn) == before  # nothing rewritten, no interest touched
        _alembic(scratch.url, "check")

        with psycopg.connect(dsn) as conn:
            rows = conn.execute(
                "SELECT company, card_sector, card_topics, card_seeking, card_offering "
                "FROM users ORDER BY id"
            ).fetchall()
            assert rows == [(None, None, [], [], [])] * 2  # nothing invented
            # an old-style insert still works and gets the same empty card
            conn.execute(
                "INSERT INTO users (first_name, last_name, email, phone, profession, "
                "sponsor_consent, status) VALUES ('N', 'O', 'n@x.test', '+1', 'Eng', false, "
                "'PROVISIONAL')"
            )
            conn.commit()
            for statement in (
                "UPDATE users SET card_topics = ARRAY['a','b','c','d'] WHERE id = %s",
                "UPDATE users SET card_seeking = ARRAY['a','b','c'] WHERE id = %s",
                "UPDATE users SET card_offering = ARRAY['a','b','c'] WHERE id = %s",
                "UPDATE users SET company = '' WHERE id = %s",
            ):
                with pytest.raises(psycopg.errors.CheckViolation):
                    conn.execute(statement, (ids[0],))
                conn.rollback()
            conn.execute(
                "UPDATE users SET company = 'Acme', card_sector = 'energy', "
                "card_topics = ARRAY['data','erp','hr'] WHERE id = %s",
                (ids[0],),
            )
            conn.commit()

        # DOWN: the new columns go; every attendee, interest and weight stays
        _alembic(scratch.url, "downgrade", BEFORE)
        assert not (NEW_COLUMNS & _columns(dsn))
        users, interests, catalogue, weights = _snapshot(dsn)
        assert [u for u in users if u[0] in ids] == before[0]
        assert (interests, catalogue, weights) == before[1:]

        _alembic(scratch.url, "upgrade", "head")
        _alembic(scratch.url, "check")
