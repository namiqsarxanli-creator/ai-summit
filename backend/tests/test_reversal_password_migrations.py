"""Migrations 0012 (points reversal) and 0013 (staff password reset purpose) on a
database that already holds ledger and OTP rows.

Builds its own scratch database at 0011 (tests/suite_db.py), plants rows, upgrades
and downgrades, and checks that every existing row keeps its meaning, that the new
form is accepted while the old refusals still hold, that a downgrade which cannot
keep the data refuses instead of losing it, and that the model and the migrations
agree.
"""

from __future__ import annotations

import os
import subprocess
import sys

import psycopg
import pytest

from tests import ledger_snapshot, suite_db
from tests.reset_support import BACKEND_ROOT
from tests.test_admin_lists_migration import _alembic

ATTRIBUTION = "ck_points_log_source_attribution"
NOTE_LENGTH = "ck_points_log_note_length"


def _plant(dsn: str) -> dict[str, int]:
    """Rows as the code BEFORE 0012/0013 wrote them."""
    with psycopg.connect(dsn) as conn:

        def one(sql: str, *params: object) -> int:
            row = conn.execute(sql, params).fetchone()
            assert row is not None
            return int(row[0])

        booth = one("INSERT INTO booths (slug, name) VALUES ('old-booth', 'Old') RETURNING id")
        booth2 = one("INSERT INTO booths (slug, name) VALUES ('old-booth-2', 'Old 2') RETURNING id")
        task = one(
            "INSERT INTO tasks (booth_id, title, points) VALUES (%s, 'Old task', 10) RETURNING id",
            booth,
        )
        user = one(
            "INSERT INTO users (first_name, last_name, email, phone, profession, sponsor_consent, "
            "status, activated_at) VALUES ('Old', 'Attendee', 'old@x.test', '+1', 'Eng', false, "
            "'ACTIVE', now()) RETURNING id"
        )
        admin = one(
            "INSERT INTO staff_accounts (email, password_hash, full_name, role) "
            "VALUES ('a@x.test', 'h', 'Admin', 'ADMIN') RETURNING id"
        )
        staff = one(
            "INSERT INTO staff_accounts (email, password_hash, full_name, role, booth_id) "
            "VALUES ('s@x.test', 'h', 'Staff', 'BOOTH_STAFF', %s) RETURNING id",
            booth,
        )
        task_award = one(
            "INSERT INTO points_log (user_id, booth_id, source, points, task_id, "
            "entered_by_staff_id, entered_by_system) VALUES (%s, %s, 'TASK', 10, %s, %s, false) "
            "RETURNING id",
            user,
            booth,
            task,
            staff,
        )
        feedback = one(
            "INSERT INTO points_log (user_id, booth_id, source, points, entered_by_staff_id, "
            "entered_by_system) VALUES (%s, %s, 'FEEDBACK', 5, NULL, true) RETURNING id",
            user,
            booth,
        )
        feedback2 = one(
            "INSERT INTO points_log (user_id, booth_id, source, points, entered_by_staff_id, "
            "entered_by_system) VALUES (%s, %s, 'FEEDBACK', 5, NULL, true) RETURNING id",
            user,
            booth2,
        )
        # the OLD forms of a reversal: the system's FEEDBACK one, and a staff TASK one
        conn.execute(
            "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
            "reverses_points_log_id, entered_by_staff_id, entered_by_system) "
            "VALUES (%s, %s, 'FEEDBACK', 'REVERSAL', -5, %s, NULL, true)",
            (user, booth, feedback),
        )
        conn.execute(
            "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, task_id, "
            "reverses_points_log_id, entered_by_staff_id, entered_by_system) "
            "VALUES (%s, %s, 'TASK', 'REVERSAL', -10, %s, %s, %s, false)",
            (user, booth, task, task_award, staff),
        )
        challenge = one(
            "INSERT INTO otp_challenges (email, purpose, code_hash, expires_at) VALUES "
            "('old@x.test', 'LOGIN', 'hash', now() + interval '10 minutes') RETURNING id"
        )
        conn.execute(
            "INSERT INTO otp_deliveries (challenge_id, email, purpose, language, idempotency_key, "
            "payload_ciphertext, expires_at) VALUES (%s, 'old@x.test', 'LOGIN', 'en', 'k1', "
            "'\\x00', now() + interval '10 minutes')",
            (challenge,),
        )
        conn.commit()
    return {
        "booth": booth,
        "booth2": booth2,
        "user": user,
        "admin": admin,
        "staff": staff,
        "feedback2": feedback2,
    }


def _snapshot(
    dsn: str, ledger_columns: list[str]
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    """The ledger by the columns that existed before the upgrade (by name), and
    the OTP challenges - comparable across later revisions that add columns."""
    with psycopg.connect(dsn) as conn:
        ledger = ledger_snapshot.rows(conn, "points_log", ledger_columns)
        otp = conn.execute(
            "SELECT id, email::text, purpose::text, code_hash FROM otp_challenges ORDER BY id"
        ).fetchall()
    return ledger, otp


def _constraint_names(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        return {
            str(r[0])
            for r in conn.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'points_log'::regclass "
                "AND contype = 'c'"
            )
        }


def _enum_values(dsn: str) -> list[str]:
    with psycopg.connect(dsn) as conn:
        return [
            str(r[0])
            for r in conn.execute(
                "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = 'otp_purpose' ORDER BY e.enumsortorder"
            )
        ]


def _admin_feedback_reversal(dsn: str, ids: dict[str, int]) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
            "reverses_points_log_id, entered_by_staff_id, entered_by_system) "
            "VALUES (%s, %s, 'FEEDBACK', 'REVERSAL', -5, %s, %s, false)",
            (ids["user"], ids["booth2"], ids["feedback2"], ids["admin"]),
        )
        conn.commit()


def _alembic_status(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND_ROOT),
        env=dict(os.environ, DATABASE_URL=url),
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_0012_and_0013_keep_every_row_and_only_widen_what_is_allowed() -> None:
    with suite_db.scratch_database("reversal", migrate_to="0011_audit_view") as scratch:
        dsn = scratch.psycopg_url
        ids = _plant(dsn)
        with psycopg.connect(dsn) as conn:
            old_columns = ledger_snapshot.columns(conn, "points_log")
            old_ids = [int(r[0]) for r in conn.execute("SELECT id FROM points_log")]
        before = _snapshot(dsn, old_columns)
        assert _enum_values(dsn) == ["ACTIVATION", "LOGIN"]
        assert NOTE_LENGTH not in _constraint_names(dsn)
        # under the OLD rule an administrator's FEEDBACK reversal is refused
        with pytest.raises(psycopg.errors.CheckViolation):
            _admin_feedback_reversal(dsn, ids)

        _alembic(scratch.url, "upgrade", "head")

        # (the head moves on with later revisions; this test is about 0012 and 0013)
        assert "(head)" in _alembic(scratch.url, "current")
        # Every historical value, column by column, is exactly what it was.
        assert _snapshot(dsn, old_columns) == before  # nothing changed, nothing lost
        # Columns added by later revisions invent nothing for these old rows.
        with psycopg.connect(dsn) as conn:
            added = ledger_snapshot.added_values(conn, "points_log", old_columns, old_ids)
        assert "occurred_at" in added  # migration 0027's evidence field exists...
        assert all(v is None for values in added.values() for v in values), added
        assert _enum_values(dsn) == ["ACTIVATION", "LOGIN", "STAFF_PASSWORD_RESET"]
        assert {ATTRIBUTION, NOTE_LENGTH} <= _constraint_names(dsn)
        _alembic(scratch.url, "check")  # the models and the migrations agree

        # the new form is accepted ...
        _admin_feedback_reversal(dsn, ids)
        # ... and nothing else that was refused before became allowed
        refused = [
            # a FEEDBACK AWARD by a staff account
            "INSERT INTO points_log (user_id, booth_id, source, points, entered_by_staff_id, "
            "entered_by_system) VALUES ({u}, {b}, 'FEEDBACK', 5, {s}, false)",
            # a TASK AWARD by the system
            "INSERT INTO points_log (user_id, booth_id, source, points, entered_by_staff_id, "
            "entered_by_system) VALUES ({u}, {b}, 'TASK', 5, NULL, true)",
            # a FEEDBACK REVERSAL that names a staff account but says "system"
            "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
            "reverses_points_log_id, entered_by_staff_id, entered_by_system) "
            "VALUES ({u}, {b}, 'FEEDBACK', 'REVERSAL', -5, 1, {s}, true)",
            # an over-long note
            "INSERT INTO points_log (user_id, booth_id, source, points, entered_by_staff_id, "
            "entered_by_system, note) VALUES ({u}, {b}, 'TASK', 5, {s}, false, repeat('x', 301))",
        ]
        for statement in refused:
            with pytest.raises(psycopg.errors.CheckViolation), psycopg.connect(dsn) as conn:
                conn.execute(statement.format(u=ids["user"], b=ids["booth"], s=ids["staff"]))
                conn.commit()
        # the new enum value is usable
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO otp_challenges (email, purpose, code_hash, expires_at) VALUES "
                "('a@x.test', 'STAFF_PASSWORD_RESET', 'hash', now() + interval '10 minutes')"
            )
            conn.commit()

        # DOWN 0013: the staff reset challenge goes, the attendee's rows stay
        _alembic(scratch.url, "downgrade", "0012_points_reversal")
        assert _enum_values(dsn) == ["ACTIVATION", "LOGIN"]
        assert _snapshot(dsn, old_columns)[1] == before[1]

        # DOWN 0012 refuses while the administrator-made reversal exists, changing nothing
        refusal = _alembic_status(scratch.url, "downgrade", "0011_audit_view")
        assert refusal.returncode != 0
        assert "0012_points_reversal" in _alembic(scratch.url, "current")
        assert {ATTRIBUTION, NOTE_LENGTH} <= _constraint_names(dsn)
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "DELETE FROM points_log WHERE entered_by_staff_id = %s AND entry_type = 'REVERSAL' "
                "AND source = 'FEEDBACK'",
                (ids["admin"],),
            )
            conn.commit()

        _alembic(scratch.url, "downgrade", "0011_audit_view")
        assert NOTE_LENGTH not in _constraint_names(dsn)
        with psycopg.connect(dsn) as conn:  # the old schema exactly, column for column
            assert ledger_snapshot.columns(conn, "points_log") == old_columns
        assert _snapshot(dsn, old_columns) == before
        with pytest.raises(psycopg.errors.CheckViolation):
            _admin_feedback_reversal(dsn, ids)  # the old rule is back

        _alembic(scratch.url, "upgrade", "head")
        assert "(head)" in _alembic(scratch.url, "current")
        assert _snapshot(dsn, old_columns) == before
        with psycopg.connect(dsn) as conn:
            again = ledger_snapshot.added_values(conn, "points_log", old_columns, old_ids)
        assert all(v is None for values in again.values() for v in values), again
