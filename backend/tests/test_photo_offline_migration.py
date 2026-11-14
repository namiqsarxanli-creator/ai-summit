"""Migrations 0025 (pair photos) and 0026 (offline scans) on a database with
attendees: additive, empty, their guards hold, a refused downgrade changes nothing."""

from __future__ import annotations

import psycopg
import pytest

from tests import suite_db
from tests.test_admin_lists_migration import _alembic

BEFORE = "0024_reminders"
NEW = {"pair_photos", "offline_scans"}


def _tables(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        return {
            str(r[0])
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
        }


def test_0025_and_0026_are_additive_and_guarded() -> None:
    with suite_db.scratch_database("photooff", migrate_to=BEFORE) as scratch:
        dsn = scratch.psycopg_url
        with psycopg.connect(dsn) as conn:
            a, b = (
                conn.execute(
                    "INSERT INTO users (first_name, last_name, email, phone, profession, "
                    "sponsor_consent, status, activated_at) VALUES ('U', %s, %s, '+1', 'E', "
                    "false, 'ACTIVE', now()) RETURNING id",
                    (str(i), f"u{i}@x.test"),
                ).fetchone()[0]  # type: ignore[index]
                for i in range(2)
            )
            staff = conn.execute(
                "INSERT INTO staff_accounts (email, password_hash, full_name, role, is_active) "
                "VALUES ('v@x.test', 'x', 'V', 'ADMIN', true) RETURNING id"
            ).fetchone()
            assert staff
            conn.commit()
            before = conn.execute("SELECT id, email::text FROM users ORDER BY id").fetchall()

        _alembic(scratch.url, "upgrade", "0026_offline_scans")
        assert _tables(dsn) >= NEW
        with psycopg.connect(dsn) as conn:
            for table in sorted(NEW):
                assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)  # noqa: S608
            with pytest.raises(psycopg.errors.CheckViolation):  # a withdrawn photo keeps a key
                conn.execute(
                    "INSERT INTO pair_photos (uploader_id, partner_id, photo_key, status, "
                    "withdrawn_at) VALUES (%s, %s, %s, 'WITHDRAWN', now())",
                    (a, b, "0" * 32),
                )
            conn.rollback()
            with pytest.raises(psycopg.errors.CheckViolation):  # a key that names a path
                conn.execute(
                    "INSERT INTO pair_photos (uploader_id, partner_id, photo_key) "
                    "VALUES (%s, %s, '../../etc/passwd')",
                    (a, b),
                )
            conn.rollback()
            insert = (
                "INSERT INTO offline_scans (staff_account_id, client_scan_id, kind, target_id, "
                "user_id, device_scanned_at, status) VALUES (%s, 'scan-0001', "
                "'MENTOR_COMPLETE', 1, %s, now(), 'PENDING_REVIEW')"
            )
            conn.execute(insert, (staff[0], a))
            conn.commit()
            with pytest.raises(psycopg.errors.UniqueViolation) as caught:  # a replay
                conn.execute(insert, (staff[0], a))
            assert caught.value.diag.constraint_name == "uq_offline_scans_replay"
            conn.rollback()

        with pytest.raises(AssertionError, match="offline_scans holds"):
            _alembic(scratch.url, "downgrade", BEFORE)
        assert _tables(dsn) >= NEW  # one transaction: nothing of it was applied
        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM offline_scans")
            conn.commit()
        _alembic(scratch.url, "downgrade", BEFORE)
        assert not (NEW & _tables(dsn))
        with psycopg.connect(dsn) as conn:
            after = conn.execute("SELECT id, email::text FROM users ORDER BY id").fetchall()
            assert after == before
        _alembic(scratch.url, "upgrade", "head")
        _alembic(scratch.url, "check")
