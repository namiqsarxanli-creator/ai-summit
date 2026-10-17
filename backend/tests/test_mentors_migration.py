"""Migration 0022 (Meet the Mentor) on a database with attendees: additive, empty,
the group-size guard holds in the database itself, and a downgrade never drops
attendee history silently."""

from __future__ import annotations

import psycopg
import pytest

from tests import suite_db
from tests.test_admin_lists_migration import _alembic

BEFORE = "0021_people"
NEW_TABLES = {"mentor_sessions", "mentor_bookings"}


def _tables(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        return {
            str(r[0])
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
        }


def _snapshot(dsn: str) -> list[tuple[object, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT id, email::text, status::text FROM users ORDER BY id"
        ).fetchall()


def test_0022_adds_empty_mentor_tables_whose_guards_hold() -> None:
    with suite_db.scratch_database("mentors", migrate_to=BEFORE) as scratch:
        dsn = scratch.psycopg_url
        with psycopg.connect(dsn) as conn:
            ids = [
                conn.execute(
                    "INSERT INTO users (first_name, last_name, email, phone, profession, "
                    "sponsor_consent, status, activated_at) VALUES ('U', %s, %s, '+1', 'E', "
                    "false, 'ACTIVE', now()) RETURNING id",
                    (str(i), f"u{i}@x.test"),
                ).fetchone()[0]  # type: ignore[index]
                for i in range(3)
            ]
            event = conn.execute(
                "INSERT INTO masterclass_events (code, title, timezone, event_date) "
                "VALUES ('e', 'E', 'Asia/Baku', '2026-11-13') RETURNING id"
            ).fetchone()
            assert event
            conn.commit()
        users_before = _snapshot(dsn)

        _alembic(scratch.url, "upgrade", "0022_mentors")
        assert _tables(dsn) >= NEW_TABLES
        assert _snapshot(dsn) == users_before
        with psycopg.connect(dsn) as conn:
            for table in sorted(NEW_TABLES):
                row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
                assert row is not None and row[0] == 0, table  # nothing invented
            with pytest.raises(psycopg.errors.CheckViolation):  # not 20 minutes
                conn.execute(
                    "INSERT INTO mentor_sessions (event_id, code, title, starts_at, ends_at) "
                    "VALUES (%s, 'x', 'T', '2026-11-13T13:10+04', '2026-11-13T13:40+04')",
                    (event[0],),
                )
            conn.rollback()
            session = conn.execute(
                "INSERT INTO mentor_sessions (event_id, code, title, starts_at, ends_at, "
                "capacity) VALUES (%s, 'm', 'T', '2026-11-13T13:10+04', "
                "'2026-11-13T13:30+04', 1) RETURNING id",
                (event[0],),
            ).fetchone()
            assert session
            conn.execute(
                "INSERT INTO mentor_bookings (session_id, user_id, status, command_id) "
                "VALUES (%s, %s, 'CONFIRMED', 'c1')",
                (session[0], ids[0]),
            )
            conn.commit()
            with pytest.raises(psycopg.errors.CheckViolation) as caught:  # the group is full
                conn.execute(
                    "INSERT INTO mentor_bookings (session_id, user_id, status, command_id) "
                    "VALUES (%s, %s, 'CONFIRMED', 'c2')",
                    (session[0], ids[1]),
                )
            assert caught.value.diag.constraint_name == "ck_mentor_capacity"
            conn.rollback()
            # Waiting is not a seat; promoting it past the size is refused too.
            waiting = conn.execute(
                "INSERT INTO mentor_bookings (session_id, user_id, status, command_id) "
                "VALUES (%s, %s, 'WAITLISTED', 'c3') RETURNING id",
                (session[0], ids[2]),
            ).fetchone()
            assert waiting
            conn.commit()
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "UPDATE mentor_bookings SET status = 'CONFIRMED' WHERE id = %s", (waiting[0],)
                )
            conn.rollback()

        # Bookings are attendee history: the downgrade refuses rather than drop them.
        with pytest.raises(AssertionError, match="attendee history"):
            _alembic(scratch.url, "downgrade", BEFORE)
        assert _tables(dsn) >= NEW_TABLES
        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM mentor_bookings")
            conn.commit()
        _alembic(scratch.url, "downgrade", BEFORE)
        assert not (NEW_TABLES & _tables(dsn))
        assert _snapshot(dsn) == users_before
        _alembic(scratch.url, "upgrade", "head")
        _alembic(scratch.url, "check")
