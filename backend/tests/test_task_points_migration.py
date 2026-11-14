"""Migration 0009 (activity award rules) against a database that already
holds data written by the previous release.

Builds its OWN throw-away database at revision 0008, plants rows exactly as
the previous award flow wrote them, upgrades, and checks that nothing was
rewritten or attached to an invented activity. Never touches any other
database.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator

import psycopg
import pytest

from tests import ledger_snapshot, suite_db
from tests.reset_support import BACKEND_ROOT


def _alembic(url: str, *args: str) -> str:
    env = dict(os.environ, DATABASE_URL=url)
    done = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout + done.stderr


@pytest.fixture
def legacy_db() -> Iterator[tuple[str, str]]:
    """(sqlalchemy url, psycopg dsn) of a database at 0008 holding legacy data."""
    with suite_db.scratch_database("migration", migrate_to="0008_otp_outbox") as scratch:
        yield scratch.url, scratch.psycopg_url


def _plant_legacy(dsn: str) -> dict[str, int]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO booths (slug, name) VALUES ('legacy-booth', 'Legacy') RETURNING id"
        )
        booth_id = cur.fetchone()[0]  # type: ignore[index]
        cur.execute(
            "INSERT INTO tasks (booth_id, title) VALUES (%s, 'Old descriptive task') RETURNING id",
            (booth_id,),
        )
        task_id = cur.fetchone()[0]  # type: ignore[index]
        cur.execute(
            "INSERT INTO users (first_name, last_name, email, phone, profession, "
            "sponsor_consent, status, activated_at) VALUES ('Old','Attendee','old@x.test','+1',"
            "'Eng', false, 'ACTIVE', now()) RETURNING id"
        )
        user_id = cur.fetchone()[0]  # type: ignore[index]
        cur.execute(
            "INSERT INTO staff_accounts (email, password_hash, full_name, role, booth_id) "
            "VALUES ('old-staff@x.test', 'h', 'Old Staff', 'BOOTH_STAFF', %s) RETURNING id",
            (booth_id,),
        )
        staff_id = cur.fetchone()[0]  # type: ignore[index]
        cur.execute(
            "INSERT INTO points_log (user_id, booth_id, source, points, entered_by_staff_id, "
            "dedupe_key) VALUES (%s, %s, 'TASK', 10, %s, %s) RETURNING id",
            (user_id, booth_id, staff_id, f"TASK:{user_id}:{booth_id}"),
        )
        award_id = cur.fetchone()[0]  # type: ignore[index]
        conn.commit()
    return {
        "booth": booth_id,
        "task": task_id,
        "user": user_id,
        "staff": staff_id,
        "award": award_id,
    }


def test_upgrading_keeps_every_existing_row_and_invents_nothing(
    legacy_db: tuple[str, str],
) -> None:
    url, dsn = legacy_db
    ids = _plant_legacy(dsn)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        old_columns = ledger_snapshot.columns(conn, "points_log")
        before_ledger = ledger_snapshot.rows(conn, "points_log", old_columns)
        cur.execute("SELECT id, booth_id, title, description, is_active, sort_order FROM tasks")
        before_tasks = cur.fetchall()

    out = _alembic(url, "upgrade", "head")
    assert "0009_task_points" in out

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        # Every value of every pre-upgrade column, by name, is unchanged.
        assert ledger_snapshot.rows(conn, "points_log", old_columns) == before_ledger
        # Columns added to the ledger since (occurred_at in 0027) invent nothing.
        added = ledger_snapshot.added_values(conn, "points_log", old_columns, [ids["award"]])
        assert "occurred_at" in added, added
        assert all(v is None for values in added.values() for v in values), added
        cur.execute("SELECT id, booth_id, title, description, is_active, sort_order FROM tasks")
        assert cur.fetchall() == before_tasks
        cur.execute("SELECT points, allow_repeat_points FROM tasks WHERE id = %s", (ids["task"],))
        assert cur.fetchone() == (None, None)  # no value is guessed for an old activity
        cur.execute("SELECT task_id FROM points_log")
        assert [r[0] for r in cur.fetchall()] == [None]
        cur.execute("SELECT sum(points) FROM points_log WHERE user_id = %s", (ids["user"],))
        assert cur.fetchone() == (10,)  # scores are unchanged


def test_the_new_rules_are_enforced_by_the_database_after_upgrade(
    legacy_db: tuple[str, str],
) -> None:
    url, dsn = legacy_db
    ids = _plant_legacy(dsn)
    _alembic(url, "upgrade", "head")

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET points = 20, allow_repeat_points = false WHERE id = %s",
            (ids["task"],),
        )
        conn.commit()
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute("UPDATE tasks SET points = 0 WHERE id = %s", (ids["task"],))
        conn.rollback()
        insert = (
            "INSERT INTO points_log (user_id, booth_id, source, points, task_id, "
            "entered_by_staff_id, dedupe_key) VALUES (%s, %s, 'TASK', 20, %s, %s, %s)"
        )
        cur.execute(insert, (ids["user"], ids["booth"], ids["task"], ids["staff"], "a"))
        conn.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(insert, (ids["user"], ids["booth"], ids["task"], ids["staff"], "b"))
        conn.rollback()


def test_downgrade_and_re_upgrade_are_clean(legacy_db: tuple[str, str]) -> None:
    url, dsn = legacy_db
    ids = _plant_legacy(dsn)
    _alembic(url, "upgrade", "head")
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("UPDATE tasks SET points = 9 WHERE id = %s", (ids["task"],))
        conn.commit()

    _alembic(url, "downgrade", "0008_otp_outbox")
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'tasks' AND column_name IN ('points', 'allow_repeat_points')"
        )
        assert cur.fetchone() == (0,)
        cur.execute("SELECT count(*) FROM points_log")
        assert cur.fetchone() == (1,)  # the award survived the downgrade

    _alembic(url, "upgrade", "head")
    assert "(head)" in _alembic(url, "current")  # whatever the newest revision is
    out = _alembic(url, "check")
    assert "No new upgrade operations detected" in out
