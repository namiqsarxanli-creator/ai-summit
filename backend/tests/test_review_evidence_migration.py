"""Migration 0027 (review evidence) over data written before it: nothing is
rewritten, old offline scans keep NULL card evidence (and are therefore not
treated as verified), and a downgrade refuses once evidence exists."""

from __future__ import annotations

import psycopg
import pytest

from tests import suite_db
from tests.test_admin_lists_migration import _alembic

BEFORE = "0026_offline_scans"


def _columns(dsn: str, table: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        return {
            str(r[0])
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (table,),
            )
        }


def test_0027_adds_nullable_evidence_and_keeps_old_rows_unverified() -> None:
    with suite_db.scratch_database("evidence", migrate_to=BEFORE) as scratch:
        dsn = scratch.psycopg_url
        with psycopg.connect(dsn) as conn:
            uid = conn.execute(
                "INSERT INTO users (first_name, last_name, email, phone, profession, "
                "sponsor_consent, status, activated_at) VALUES ('U', 'V', 'u@x.test', '+1', "
                "'E', false, 'ACTIVE', now()) RETURNING id"
            ).fetchone()[0]  # type: ignore[index]
            staff = conn.execute(
                "INSERT INTO staff_accounts (email, password_hash, full_name, role, is_active) "
                "VALUES ('v@x.test', 'x', 'V', 'ADMIN', true) RETURNING id"
            ).fetchone()[0]  # type: ignore[index]
            conn.execute(
                "INSERT INTO offline_scans (staff_account_id, client_scan_id, kind, target_id, "
                "user_id, device_scanned_at, status) VALUES (%s, 'old-scan-1', "
                "'MENTOR_COMPLETE', 1, %s, now(), 'PENDING_REVIEW')",
                (staff, uid),
            )
            conn.execute(
                "INSERT INTO points_log (user_id, source, entry_type, points, entered_by_system, "
                "dedupe_key) VALUES (%s, 'PANEL', 'AWARD', 8, true, 'COMP:PANEL:old')",
                (uid,),
            )
            conn.commit()
            ledger_before = conn.execute(
                "SELECT id, points, created_at FROM points_log ORDER BY id"
            ).fetchall()

        _alembic(scratch.url, "upgrade", "0027_review_evidence")
        assert {"card_id", "verified_at"} <= _columns(dsn, "offline_scans")
        assert "occurred_at" in _columns(dsn, "points_log")
        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT card_id, verified_at FROM offline_scans").fetchall() == [
                (None, None)
            ]  # old evidence is NOT invented
            assert conn.execute("SELECT occurred_at FROM points_log").fetchall() == [(None,)]
            assert (
                conn.execute("SELECT id, points, created_at FROM points_log ORDER BY id").fetchall()
                == ledger_before
            )
            conn.execute("UPDATE offline_scans SET verified_at = now()")
            conn.commit()

        with pytest.raises(AssertionError, match="would lose them"):
            _alembic(scratch.url, "downgrade", BEFORE)
        with psycopg.connect(dsn) as conn:
            conn.execute("UPDATE offline_scans SET verified_at = NULL")
            conn.commit()
        _alembic(scratch.url, "downgrade", BEFORE)
        assert "card_id" not in _columns(dsn, "offline_scans")
        _alembic(scratch.url, "upgrade", "head")
        _alembic(scratch.url, "check")
