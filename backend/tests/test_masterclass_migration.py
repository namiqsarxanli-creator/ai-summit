"""Migration 0017 (masterclass programme and reservations) on a database that
already holds attendees with interest cards, booths, booth weights and points.

A scratch database at 0016 is planted, upgraded and downgraded: the new tables
arrive EMPTY (no programme, no reservation, no attendance invented for anyone),
nothing that existed changes, the database-level invariants work, and the
models and migrations agree.
"""

from __future__ import annotations

import psycopg

from tests import suite_db
from tests.test_admin_lists_migration import _alembic

BEFORE = "0016_interest_card"
NEW_TABLES = {
    "masterclass_events",
    "masterclass_blocks",
    "masterclass_rooms",
    "masterclasses",
    "masterclass_reservations",
}


def _plant(dsn: str) -> int:
    with psycopg.connect(dsn) as conn:
        interest = conn.execute(
            "INSERT INTO interests (slug, name, sort_order, is_active) "
            "VALUES ('old-a', 'Old A', 1, true) RETURNING id"
        ).fetchone()
        booth = conn.execute(
            "INSERT INTO booths (slug, name) VALUES ('old-booth', 'Old Booth') RETURNING id"
        ).fetchone()
        user = conn.execute(
            "INSERT INTO users (first_name, last_name, email, phone, profession, "
            "sponsor_consent, status, activated_at, company, card_sector, card_topics) "
            "VALUES ('Old', 'User', 'old@x.test', '+1', 'Engineer', false, 'ACTIVE', now(), "
            "'Acme', 'energy', ARRAY['data']) RETURNING id"
        ).fetchone()
        assert interest and booth and user
        conn.execute(
            "INSERT INTO booth_interests (booth_id, interest_id, weight) VALUES (%s, %s, 0.75)",
            (booth[0], interest[0]),
        )
        conn.execute(
            "INSERT INTO user_interests (user_id, interest_id) VALUES (%s, %s)",
            (user[0], interest[0]),
        )
        conn.execute(
            "INSERT INTO points_log (user_id, booth_id, source, points, entered_by_system) "
            "VALUES (%s, %s, 'FEEDBACK', 5, true)",
            (user[0], booth[0]),
        )
        conn.commit()
        return int(user[0])


def _snapshot(dsn: str) -> tuple[list[tuple[object, ...]], ...]:
    with psycopg.connect(dsn) as conn:
        return (
            conn.execute(
                "SELECT id, email::text, status::text, company, card_sector, card_topics, "
                "profile_version FROM users ORDER BY id"
            ).fetchall(),
            conn.execute(
                "SELECT user_id, interest_id FROM user_interests ORDER BY 1, 2"
            ).fetchall(),
            conn.execute(
                "SELECT booth_id, interest_id, weight FROM booth_interests ORDER BY 1, 2"
            ).fetchall(),
            conn.execute("SELECT id, slug, name, is_active FROM booths ORDER BY id").fetchall(),
            conn.execute(
                "SELECT id, user_id, source::text, points FROM points_log ORDER BY id"
            ).fetchall(),
        )


def _tables(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        return {
            str(r[0])
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
        }


def test_0017_adds_empty_programme_tables_and_keeps_everything_else() -> None:
    with suite_db.scratch_database("masterclass", migrate_to=BEFORE) as scratch:
        dsn = scratch.psycopg_url
        user_id = _plant(dsn)
        before = _snapshot(dsn)
        assert not (NEW_TABLES & _tables(dsn))

        _alembic(scratch.url, "upgrade", "head")
        assert "(head)" in _alembic(scratch.url, "current")
        assert _tables(dsn) >= NEW_TABLES
        assert _snapshot(dsn) == before  # no existing row rewritten
        _alembic(scratch.url, "check")

        with psycopg.connect(dsn) as conn:
            for table in sorted(NEW_TABLES):
                count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
                assert count is not None and count[0] == 0, table  # nothing invented

            # the invariants are in the database itself
            event = conn.execute(
                "INSERT INTO masterclass_events (code, title, timezone, event_date) "
                "VALUES ('e', 'E', 'Asia/Baku', '2026-11-13') RETURNING id"
            ).fetchone()
            assert event is not None
            block = conn.execute(
                "INSERT INTO masterclass_blocks (event_id, code, title, starts_at, ends_at, "
                "change_deadline) VALUES (%s, 'b1', 'B1', '2026-11-13T11:30+04', "
                "'2026-11-13T13:00+04', '2026-11-13T11:00+04') RETURNING id",
                (event[0],),
            ).fetchone()
            assert block is not None
            mc = conn.execute(
                "INSERT INTO masterclasses (event_id, block_id, title, capacity, "
                "waitlist_capacity, is_published) VALUES (%s, %s, 'M', 1, 1, true) RETURNING id",
                (event[0], block[0]),
            ).fetchone()
            assert mc is not None
            conn.execute(
                "INSERT INTO masterclass_reservations (masterclass_id, block_id, user_id, status, "
                "confirmed_at) VALUES (%s, %s, %s, 'CONFIRMED', now())",
                (mc[0], block[0], user_id),
            )
            conn.commit()
            try:
                conn.execute(
                    "INSERT INTO masterclass_reservations (masterclass_id, block_id, user_id, "
                    "status, queued_at) VALUES (%s, %s, %s, 'WAITLISTED', now())",
                    (mc[0], block[0], user_id),
                )
                raise AssertionError("a second active choice in one block was accepted")
            except psycopg.errors.UniqueViolation as err:
                assert (
                    err.diag.constraint_name == "uq_masterclass_reservations_one_active_per_block"
                )
            conn.rollback()
            assert _snapshot(dsn) == before  # still: a reservation is not points or a profile edit

        # DOWN: exactly the new tables go; everything that existed stays
        _alembic(scratch.url, "downgrade", BEFORE)
        assert not (NEW_TABLES & _tables(dsn))
        assert _snapshot(dsn) == before
        with psycopg.connect(dsn) as conn:
            leftover = conn.execute(
                "SELECT count(*) FROM pg_proc WHERE proname IN "
                "('masterclass_reservations_capacity', 'masterclasses_capacity_guard')"
            ).fetchone()
            assert leftover is not None and leftover[0] == 0

        _alembic(scratch.url, "upgrade", "head")
        _alembic(scratch.url, "check")
