"""Migration 0020 (competition foundation) on a database holding attendees, legacy
booth points, staff and the programme.

Upgrade: nothing that existed changes, nobody gets competition points, the
ranking stays in legacy mode, everyone stays eligible. Downgrade: refused while
competition history exists (no silent loss), clean otherwise.
"""

from __future__ import annotations

import psycopg
import pytest

from tests import suite_db
from tests.test_admin_lists_migration import _alembic

BEFORE = "0019_playground"
NEW_TABLES = {"staff_scopes", "panels", "panel_scans", "masterclass_attendance"}


def _plant(dsn: str) -> int:
    with psycopg.connect(dsn) as conn:
        booth = conn.execute(
            "INSERT INTO booths (slug, name) VALUES ('old', 'Old') RETURNING id"
        ).fetchone()
        user = conn.execute(
            "INSERT INTO users (first_name, last_name, email, phone, profession, "
            "sponsor_consent, status, activated_at) VALUES ('Old', 'User', 'o@x.test', '+1', "
            "'Eng', false, 'ACTIVE', now()) RETURNING id"
        ).fetchone()
        assert booth and user
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
            conn.execute("SELECT id, email::text, status::text FROM users ORDER BY id").fetchall(),
            conn.execute(
                "SELECT id, user_id, source::text, points FROM points_log ORDER BY id"
            ).fetchall(),
            conn.execute("SELECT id, role::text FROM staff_accounts ORDER BY id").fetchall(),
        )


def _tables(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        return {
            str(r[0])
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
        }


def test_0020_is_additive_and_refuses_a_lossy_downgrade() -> None:
    with suite_db.scratch_database("competition", migrate_to=BEFORE) as scratch:
        dsn = scratch.psycopg_url
        uid = _plant(dsn)
        before = _snapshot(dsn)

        _alembic(scratch.url, "upgrade", "0020_competition")
        assert _tables(dsn) >= NEW_TABLES
        assert _snapshot(dsn) == before
        with psycopg.connect(dsn) as conn:
            mode = conn.execute("SELECT competition_mode FROM event_config").fetchall()
            assert all(row[0] is False for row in mode)  # ranking unchanged until activated
            excluded = conn.execute(
                "SELECT count(*) FROM users WHERE competition_excluded_reason IS NOT NULL"
            ).fetchone()
            assert excluded is not None and excluded[0] == 0
            for table in sorted(NEW_TABLES):
                row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
                assert row is not None and row[0] == 0, table
            # a competition entry is now a valid ledger row...
            conn.execute(
                "INSERT INTO points_log (user_id, source, points, entered_by_system, dedupe_key) "
                "VALUES (%s, 'PROFILE', 5, true, 'COMP:PROFILE:x:profile')",
                (uid,),
            )
            # ...but a system entry claiming a staff author is not
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO points_log (user_id, source, points, entered_by_system, "
                    "entered_by_staff_id) VALUES (%s, 'PANEL', 8, true, 1)",
                    (uid,),
                )
            conn.rollback()
            conn.execute(
                "INSERT INTO points_log (user_id, source, points, entered_by_system, dedupe_key) "
                "VALUES (%s, 'PROFILE', 5, true, 'COMP:PROFILE:x:profile')",
                (uid,),
            )
            conn.commit()

        # competition history exists: the downgrade must refuse, not drop it
        with pytest.raises(AssertionError):
            _alembic(scratch.url, "downgrade", BEFORE)
        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM points_log WHERE source = 'PROFILE'")
            conn.commit()
        _alembic(scratch.url, "downgrade", BEFORE)
        assert not (NEW_TABLES & _tables(dsn))
        assert _snapshot(dsn) == before

        _alembic(scratch.url, "upgrade", "head")
        _alembic(scratch.url, "check")
