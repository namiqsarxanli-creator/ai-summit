"""Migration 0019 (Playground slots and bookings) on a database that already holds
the masterclass programme, a masterclass reservation, attendees, booths and points.

The new tables arrive EMPTY (no schedule is generated, no booking or attendance is
invented), nothing that existed changes, the capacity guard works for direct SQL,
and downgrade removes exactly the new objects.
"""

from __future__ import annotations

import psycopg
import pytest

from tests import suite_db
from tests.test_admin_lists_migration import _alembic

BEFORE = "0018_masterclass_labels"
NEW_TABLES = {"playground_slots", "playground_bookings"}


def _plant(dsn: str) -> tuple[int, int, int]:
    with psycopg.connect(dsn) as conn:
        booth = conn.execute(
            "INSERT INTO booths (slug, name) VALUES ('old-booth', 'Old Booth') RETURNING id"
        ).fetchone()
        users = [
            conn.execute(
                "INSERT INTO users (first_name, last_name, email, phone, profession, "
                "sponsor_consent, status, activated_at) VALUES (%s, 'User', %s, '+1', 'Eng', "
                "false, 'ACTIVE', now()) RETURNING id",
                (f"Old{i}", f"old{i}@x.test"),
            ).fetchone()
            for i in range(4)
        ]
        event = conn.execute(
            "INSERT INTO masterclass_events (code, title, timezone, event_date) "
            "VALUES ('e', 'E', 'Asia/Baku', '2026-11-13') RETURNING id"
        ).fetchone()
        assert booth and event and all(users)
        block = conn.execute(
            "INSERT INTO masterclass_blocks (event_id, code, title, starts_at, ends_at, "
            "change_deadline) VALUES (%s, 'b1', 'B1', '2026-11-13T11:30+04', "
            "'2026-11-13T13:00+04', '2026-11-13T11:00+04') RETURNING id",
            (event[0],),
        ).fetchone()
        assert block
        mc = conn.execute(
            "INSERT INTO masterclasses (event_id, block_id, title, is_published) "
            "VALUES (%s, %s, 'M', true) RETURNING id",
            (event[0], block[0]),
        ).fetchone()
        assert mc
        conn.execute(
            "INSERT INTO masterclass_reservations (masterclass_id, block_id, user_id, status, "
            "confirmed_at) VALUES (%s, %s, %s, 'CONFIRMED', now())",
            (mc[0], block[0], users[0][0]),  # type: ignore[index]
        )
        conn.execute(
            "INSERT INTO points_log (user_id, booth_id, source, points, entered_by_system) "
            "VALUES (%s, %s, 'FEEDBACK', 5, true)",
            (users[0][0], booth[0]),  # type: ignore[index]
        )
        conn.commit()
        return int(event[0]), int(booth[0]), int(users[0][0])  # type: ignore[index]


def _snapshot(dsn: str) -> tuple[list[tuple[object, ...]], ...]:
    with psycopg.connect(dsn) as conn:
        return (
            conn.execute("SELECT id, email::text, status::text FROM users ORDER BY id").fetchall(),
            conn.execute("SELECT id, slug, is_active FROM booths ORDER BY id").fetchall(),
            conn.execute(
                "SELECT id, user_id, status FROM masterclass_reservations ORDER BY id"
            ).fetchall(),
            conn.execute("SELECT id, user_id, points FROM points_log ORDER BY id").fetchall(),
        )


def _tables(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        return {
            str(r[0])
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
        }


def test_0019_adds_empty_playground_tables_and_keeps_everything_else() -> None:
    with suite_db.scratch_database("playground", migrate_to=BEFORE) as scratch:
        dsn = scratch.psycopg_url
        event, booth, _ = _plant(dsn)
        before = _snapshot(dsn)
        assert not (NEW_TABLES & _tables(dsn))

        _alembic(scratch.url, "upgrade", "0019_playground")
        assert _tables(dsn) >= NEW_TABLES
        assert _snapshot(dsn) == before
        with psycopg.connect(dsn) as conn:
            for table in sorted(NEW_TABLES):
                row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
                assert row is not None and row[0] == 0, table  # no schedule generated

            slot = conn.execute(
                "INSERT INTO playground_slots (event_id, booth_id, number, starts_at, ends_at) "
                "VALUES (%s, %s, 1, '2026-11-13T09:00+04', '2026-11-13T09:15+04') RETURNING id",
                (event, booth),
            ).fetchone()
            assert slot
            users = [r[0] for r in conn.execute("SELECT id FROM users ORDER BY id").fetchall()]
            for i, uid in enumerate(users[:3]):
                conn.execute(
                    "INSERT INTO playground_bookings (slot_id, user_id, command_id, status) "
                    "VALUES (%s, %s, %s, 'CONFIRMED')",
                    (slot[0], uid, f"c{i}"),
                )
            conn.commit()
            with pytest.raises(psycopg.errors.CheckViolation) as caught:
                conn.execute(
                    "INSERT INTO playground_bookings (slot_id, user_id, command_id, status) "
                    "VALUES (%s, %s, 'c4', 'CONFIRMED')",
                    (slot[0], users[3]),
                )
            assert caught.value.diag.constraint_name == "ck_playground_capacity"
            conn.rollback()
            with pytest.raises(psycopg.errors.CheckViolation):  # a 15-minute slot only
                conn.execute(
                    "INSERT INTO playground_slots (event_id, booth_id, number, starts_at, "
                    "ends_at) VALUES (%s, %s, 2, '2026-11-13T09:15+04', '2026-11-13T09:45+04')",
                    (event, booth),
                )
            conn.rollback()
        assert _snapshot(dsn) == before

        _alembic(scratch.url, "downgrade", BEFORE)
        assert not (NEW_TABLES & _tables(dsn))
        assert _snapshot(dsn) == before
        with psycopg.connect(dsn) as conn:
            left = conn.execute(
                "SELECT count(*) FROM pg_proc WHERE proname = 'playground_capacity_guard'"
            ).fetchone()
            assert left is not None and left[0] == 0

        _alembic(scratch.url, "upgrade", "head")
        _alembic(scratch.url, "check")
