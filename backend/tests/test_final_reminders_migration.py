"""Migrations 0023 (final results) and 0024 (reminders) on a database with
attendees: additive, defaults that change nothing (reminders OFF, nobody opted
in), the immutability trigger holds, and no downgrade drops history silently."""

from __future__ import annotations

import psycopg
import pytest

from tests import suite_db
from tests.test_admin_lists_migration import _alembic

BEFORE = "0022_mentors"


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


def test_0023_and_0024_are_additive_and_keep_results_and_history() -> None:
    with suite_db.scratch_database("finalrem", migrate_to=BEFORE) as scratch:
        dsn = scratch.psycopg_url
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO users (first_name, last_name, email, phone, profession, "
                "sponsor_consent, status, activated_at) VALUES ('U', 'V', 'u@x.test', '+1', "
                "'E', false, 'ACTIVE', now())"
            )
            event = conn.execute(
                "INSERT INTO masterclass_events (code, title, timezone, event_date) "
                "VALUES ('e', 'E', 'Asia/Baku', '2026-11-13') RETURNING id"
            ).fetchone()
            staff = conn.execute(
                "INSERT INTO staff_accounts (email, password_hash, full_name, role, is_active) "
                "VALUES ('gm@x.test', 'x', 'GM', 'ADMIN', true) RETURNING id"
            ).fetchone()
            assert event and staff
            conn.commit()
        users_before = _snapshot(dsn)

        _alembic(scratch.url, "upgrade", "0024_reminders")
        assert _tables(dsn) >= {"final_results", "reminder_deliveries"}
        assert _snapshot(dsn) == users_before
        with psycopg.connect(dsn) as conn:
            # The scratch database has no seeded event_config row: check the column itself.
            assert conn.execute(
                "SELECT column_default, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'event_config' AND column_name = 'reminders_enabled'"
            ).fetchall() == [("false", "NO")]
            assert conn.execute(
                "SELECT count(*) FROM users WHERE reminder_consent_at IS NOT NULL"
            ).fetchone() == (0,)
            row = conn.execute(
                "INSERT INTO final_results (event_id, version, payload, created_by_staff_id, "
                "status, published_at, published_by_staff_id) VALUES (%s, 1, '{}'::jsonb, %s, "
                "'PUBLISHED', now(), %s) RETURNING id",
                (event[0], staff[0], staff[0]),
            ).fetchone()
            assert row
            conn.commit()
            with pytest.raises(psycopg.errors.CheckViolation) as caught:
                conn.execute(
                    "UPDATE final_results SET payload = '{\"x\": 1}'::jsonb WHERE id = %s",
                    (row[0],),
                )
            assert caught.value.diag.constraint_name == "ck_final_results_immutable"
            conn.rollback()
            with pytest.raises(psycopg.errors.UniqueViolation):  # one published per event
                conn.execute(
                    "INSERT INTO final_results (event_id, version, payload, "
                    "created_by_staff_id, status, published_at, published_by_staff_id) "
                    "VALUES (%s, 2, '{}'::jsonb, %s, 'PUBLISHED', now(), %s)",
                    (event[0], staff[0], staff[0]),
                )
            conn.rollback()

        # A published result is the event's record: the downgrade refuses.
        with pytest.raises(AssertionError, match="competition's results"):
            _alembic(scratch.url, "downgrade", BEFORE)
        # One transaction: nothing of the refused downgrade was applied.
        assert _tables(dsn) >= {"final_results", "reminder_deliveries"}
        with psycopg.connect(dsn) as conn:
            conn.execute("ALTER TABLE final_results DISABLE TRIGGER final_results_immutable")
            conn.execute("DELETE FROM final_results")
            conn.execute("ALTER TABLE final_results ENABLE TRIGGER final_results_immutable")
            conn.commit()
        _alembic(scratch.url, "downgrade", BEFORE)
        assert not ({"final_results", "reminder_deliveries"} & _tables(dsn))
        assert _snapshot(dsn) == users_before
        _alembic(scratch.url, "upgrade", "head")
        _alembic(scratch.url, "check")
