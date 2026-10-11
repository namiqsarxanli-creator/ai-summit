"""Migration 0010 (admin list indexes) on a database that already holds data.

Builds its own scratch database at 0009 (tests/suite_db.py), plants rows,
upgrades and downgrades, and checks the migration only adds and removes
indexes: no row is read back changed, none is lost.
"""

from __future__ import annotations

import os
import subprocess
import sys

import psycopg

from tests import suite_db
from tests.reset_support import BACKEND_ROOT

NEW_INDEXES = {
    "ix_users_created_at_id",
    "ix_points_log_created_at_id",
    "ix_points_log_task_id_created_at_id",
    "ix_points_log_staff_id_created_at_id",
}


def _alembic(url: str, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND_ROOT),
        env=dict(os.environ, DATABASE_URL=url),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout + done.stderr


def _index_names(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        return {
            str(r[0])
            for r in conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        }


def test_0010_adds_only_indexes_and_keeps_every_row() -> None:
    with suite_db.scratch_database("listidx", migrate_to="0009_task_points") as scratch:
        dsn = scratch.psycopg_url
        assert not (NEW_INDEXES & _index_names(dsn))
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO users (first_name, last_name, email, phone, profession, "
                "sponsor_consent, status, activated_at) VALUES "
                "('Old','Attendee','old@x.test','+1','Eng', false, 'ACTIVE', now())"
            )
            conn.commit()
            before = conn.execute("SELECT id, email::text, created_at FROM users").fetchall()

        _alembic(scratch.url, "upgrade", "0010_admin_lists")
        assert _index_names(dsn) >= NEW_INDEXES
        assert "0010_admin_lists" in _alembic(scratch.url, "current")
        with psycopg.connect(dsn) as conn:
            assert (
                conn.execute("SELECT id, email::text, created_at FROM users").fetchall() == before
            )

        # the model and the migration agree (no drift)
        _alembic(scratch.url, "upgrade", "head")
        _alembic(scratch.url, "check")

        _alembic(scratch.url, "downgrade", "0009_task_points")
        assert not (NEW_INDEXES & _index_names(dsn))
        with psycopg.connect(dsn) as conn:
            assert (
                conn.execute("SELECT id, email::text, created_at FROM users").fetchall() == before
            )
        _alembic(scratch.url, "upgrade", "head")
        assert _index_names(dsn) >= NEW_INDEXES
