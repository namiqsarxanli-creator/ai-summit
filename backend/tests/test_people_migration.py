"""Migration 0021 (people) on a database with attendees: additive, empty, and the
double-booking guards hold in the database itself."""

from __future__ import annotations

import psycopg
import pytest

from tests import suite_db
from tests.test_admin_lists_migration import _alembic

BEFORE = "0020_competition"
NEW_TABLES = {
    "user_blocks",
    "meeting_requests",
    "match_tables",
    "meetings",
    "meeting_participants",
    "connection_scans",
    "connections",
}


def _tables(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        return {
            str(r[0])
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
        }


def test_0021_adds_empty_people_tables_whose_guards_hold() -> None:
    with suite_db.scratch_database("people", migrate_to=BEFORE) as scratch:
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

        _alembic(scratch.url, "upgrade", "0021_people")
        assert _tables(dsn) >= NEW_TABLES
        assert _snapshot(dsn) == users_before
        with psycopg.connect(dsn) as conn:
            for table in sorted(NEW_TABLES):
                row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
                assert row is not None and row[0] == 0, table
            table_id = conn.execute(
                "INSERT INTO match_tables (event_id, label) VALUES (%s, 'M1') RETURNING id",
                (event[0],),
            ).fetchone()
            assert table_id
            a, b, c = ids
            requests = []
            for x, y in ((a, b), (a, c)):
                low, high = sorted((x, y))
                r = conn.execute(
                    "INSERT INTO meeting_requests (requester_id, recipient_id, pair_low_id, "
                    "pair_high_id, status, command_id, created_at, responded_at) "
                    "VALUES (%s, %s, %s, %s, 'ACCEPTED', %s, now(), now()) RETURNING id",
                    (x, y, low, high, f"cmd-{y}"),
                ).fetchone()
                assert r
                requests.append(r[0])
            conn.execute(
                "INSERT INTO meetings (request_id, table_id, starts_at, ends_at, prompts) VALUES "
                "(%s, %s, '2026-11-13T10:00+04', '2026-11-13T10:15+04', ARRAY['intro','goal'])",
                (requests[0], table_id[0]),
            )
            conn.commit()
            with pytest.raises(psycopg.errors.UniqueViolation) as caught:  # same table, same slot
                conn.execute(
                    "INSERT INTO meetings (request_id, table_id, starts_at, ends_at, prompts) "
                    "VALUES (%s, %s, '2026-11-13T10:00+04', '2026-11-13T10:15+04', "
                    "ARRAY['intro','goal'])",
                    (requests[1], table_id[0]),
                )
            assert caught.value.diag.constraint_name == "uq_meetings_table_slot"
            conn.rollback()
            with pytest.raises(psycopg.errors.UniqueViolation):  # a second open request, reversed
                low, high = sorted((a, b))
                conn.execute(
                    "INSERT INTO meeting_requests (requester_id, recipient_id, pair_low_id, "
                    "pair_high_id, command_id, created_at) VALUES (%s, %s, %s, %s, 'x', now())",
                    (b, a, low, high),
                )
            conn.rollback()
            with pytest.raises(psycopg.errors.CheckViolation):  # a pair that lies about itself
                conn.execute(
                    "INSERT INTO meeting_requests (requester_id, recipient_id, pair_low_id, "
                    "pair_high_id, command_id, created_at) VALUES (%s, %s, %s, %s, 'y', now())",
                    (b, c, c + 1000, b),
                )
            conn.rollback()

        _alembic(scratch.url, "downgrade", BEFORE)
        assert not (NEW_TABLES & _tables(dsn))
        assert _snapshot(dsn) == users_before
        _alembic(scratch.url, "upgrade", "head")
        _alembic(scratch.url, "check")


def _snapshot(dsn: str) -> list[tuple[object, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT id, email::text, status::text FROM users ORDER BY id"
        ).fetchall()
