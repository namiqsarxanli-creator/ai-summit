"""Display labels are additive: historical names and reservations stay intact."""

from __future__ import annotations

import psycopg

from tests import suite_db
from tests.test_admin_lists_migration import _alembic
from tests.test_masterclass_migration import _plant, _snapshot


def test_labels_upgrade_and_downgrade_keep_custom_names_and_reservations() -> None:
    with suite_db.scratch_database("mclabels", migrate_to="0017_masterclasses") as scratch:
        dsn = scratch.psycopg_url
        uid = _plant(dsn)
        before = _snapshot(dsn)
        with psycopg.connect(dsn) as c:
            c.execute(
                "INSERT INTO masterclass_events(code,title,timezone,event_date) VALUES "
                "('custom','Event','Asia/Baku','2026-11-13')"
            )
            event = c.execute("SELECT id FROM masterclass_events WHERE code='custom'").fetchone()
            assert event
            c.execute(
                "INSERT INTO "
                "masterclass_blocks(event_id,code,title,starts_at,ends_at,change_deadline) "
                "VALUES (%s,'b1','Custom block','2026-11-13 11:30+04','2026-11-13 "
                "13:00+04','2026-11-13 11:00+04')",
                (event[0],),
            )
            c.execute(
                "INSERT INTO masterclass_rooms(event_id,name) VALUES (%s,'Custom hall')",
                (event[0],),
            )
            block = c.execute("SELECT id FROM masterclass_blocks").fetchone()
            room = c.execute("SELECT id FROM masterclass_rooms").fetchone()
            assert block and room
            mc = c.execute(
                "INSERT INTO masterclasses(event_id,block_id,room_id,title) VALUES "
                "(%s,%s,%s,'Custom workshop') RETURNING id",
                (event[0], block[0], room[0]),
            ).fetchone()
            assert mc
            c.execute(
                "INSERT INTO "
                "masterclass_reservations "
                "(masterclass_id, block_id, user_id, status, confirmed_at) "
                "VALUES (%s,%s,%s,'CONFIRMED',now())",
                (mc[0], block[0], uid),
            )
            c.commit()
            reservations = c.execute(
                "SELECT row_to_json(r)::text FROM masterclass_reservations r ORDER BY id"
            ).fetchall()
        for target in ["0018_masterclass_labels", "0017_masterclasses", "0018_masterclass_labels"]:
            _alembic(scratch.url, "upgrade" if target.endswith("labels") else "downgrade", target)
            assert _snapshot(dsn) == before
            with psycopg.connect(dsn) as c:
                assert c.execute("SELECT title FROM masterclass_blocks").fetchone() == (
                    "Custom block",
                )
                assert c.execute("SELECT name FROM masterclass_rooms").fetchone() == (
                    "Custom hall",
                )
                assert (
                    c.execute(
                        "SELECT row_to_json(r)::text FROM masterclass_reservations r ORDER BY id"
                    ).fetchall()
                    == reservations
                )
                if target.endswith("labels"):
                    assert c.execute(
                        "SELECT display_number FROM masterclass_blocks"
                    ).fetchone() == (None,)
                    assert c.execute("SELECT display_number FROM masterclass_rooms").fetchone() == (
                        None,
                    )
        _alembic(scratch.url, "upgrade", "head")
        _alembic(scratch.url, "check")
