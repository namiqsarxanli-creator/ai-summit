"""Migration 0014 (profile version and photo key) on a database that already
holds attendees.

Builds its own scratch database at 0013 (tests/suite_db.py), plants attendees as
the code BEFORE this revision wrote them, upgrades and downgrades, and checks
that every existing row keeps its meaning (version 1, no photo), that the new
constraints hold, that a downgrade loses nothing else, and that the models and
the migrations agree.
"""

from __future__ import annotations

import psycopg
import pytest

from tests import suite_db
from tests.test_admin_lists_migration import _alembic

KEY_A = "a" * 32
KEY_B = "b" * 32


def _plant(dsn: str) -> list[int]:
    ids: list[int] = []
    with psycopg.connect(dsn) as conn:
        for index in range(3):
            consenting = index == 0
            row = conn.execute(
                "INSERT INTO users (first_name, last_name, email, phone, profession, "
                "sponsor_consent, sponsor_consent_at, sponsor_consent_version, status, "
                "activated_at) VALUES (%s, 'Old', %s, '+1', 'Eng', %s, "
                "CASE WHEN %s THEN now() END, CASE WHEN %s THEN 'registration-v1' END, "
                "'ACTIVE', now()) RETURNING id",
                (f"Old{index}", f"old{index}@x.test", consenting, consenting, consenting),
            ).fetchone()
            assert row is not None
            ids.append(int(row[0]))
        conn.execute(
            "INSERT INTO consent_audit (user_id, granted, consent_version, source) "
            "VALUES (%s, true, 'registration-v1', 'registration')",
            (ids[0],),
        )
        conn.commit()
    return ids


def _users(dsn: str) -> list[tuple[object, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT id, first_name, last_name, email::text, phone, profession, sponsor_consent, "
            "status::text FROM users ORDER BY id"
        ).fetchall()


def _columns(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        return {
            str(r[0])
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'"
            )
        }


def test_0014_adds_two_columns_and_keeps_every_attendee() -> None:
    with suite_db.scratch_database("photo", migrate_to="0013_staff_password_reset") as scratch:
        dsn = scratch.psycopg_url
        ids = _plant(dsn)
        before = _users(dsn)
        assert not ({"profile_version", "photo_key"} & _columns(dsn))

        _alembic(scratch.url, "upgrade", "head")

        assert "(head)" in _alembic(scratch.url, "current")  # 0014 or any later head
        assert {"profile_version", "photo_key"} <= _columns(dsn)
        assert _users(dsn) == before  # nothing rewritten, nothing lost
        _alembic(scratch.url, "check")  # the models and the migrations agree

        with psycopg.connect(dsn) as conn:
            rows = conn.execute("SELECT profile_version, photo_key FROM users").fetchall()
            assert rows == [(1, None)] * 3  # every old row reads as version 1, no photo
            assert conn.execute("SELECT count(*) FROM consent_audit").fetchone() == (1,)

            # an old-style insert (that knows nothing of the new columns) still works
            conn.execute(
                "INSERT INTO users (first_name, last_name, email, phone, profession, "
                "sponsor_consent, status) VALUES ('New', 'Old-style', 'n@x.test', '+1', 'Eng', "
                "false, 'PROVISIONAL')"
            )
            assert conn.execute(
                "SELECT profile_version, photo_key FROM users WHERE email = 'n@x.test'"
            ).fetchone() == (1, None)
            conn.commit()

            # a key must be exactly what the server makes
            for bad in ("", "short", "A" * 32, "g" * 32, "a" * 31, "a" * 33, "../" + "a" * 29):
                with pytest.raises(psycopg.errors.CheckViolation):
                    conn.execute("UPDATE users SET photo_key = %s WHERE id = %s", (bad, ids[0]))
                conn.rollback()
            # and no two attendees may share one
            conn.execute("UPDATE users SET photo_key = %s WHERE id = %s", (KEY_A, ids[0]))
            conn.commit()
            with pytest.raises(psycopg.errors.UniqueViolation):
                conn.execute("UPDATE users SET photo_key = %s WHERE id = %s", (KEY_A, ids[1]))
            conn.rollback()
            conn.execute("UPDATE users SET photo_key = %s WHERE id = %s", (KEY_B, ids[1]))
            # many attendees without a photo are fine (the index is partial)
            assert conn.execute(
                "SELECT count(*) FROM users WHERE photo_key IS NULL"
            ).fetchone() == (2,)
            conn.execute(
                "UPDATE users SET profile_version = profile_version + 1 WHERE id = %s", (ids[0],)
            )
            conn.commit()

        # DOWN: the two columns go, every attendee stays exactly as planted
        _alembic(scratch.url, "downgrade", "0013_staff_password_reset")
        assert not ({"profile_version", "photo_key"} & _columns(dsn))
        after = _users(dsn)
        assert [row for row in after if row[0] in ids] == before

        # and UP again, from the downgraded state
        _alembic(scratch.url, "upgrade", "head")
        assert "(head)" in _alembic(scratch.url, "current")  # 0014 or any later head
        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                "SELECT count(*) FROM users WHERE profile_version = 1 AND photo_key IS NULL"
            ).fetchone() == (4,)
        _alembic(scratch.url, "check")


def test_the_new_head_is_the_only_head() -> None:
    with suite_db.scratch_database("photohead", migrate_to="head") as scratch:
        heads = _alembic(scratch.url, "heads")
        assert heads.count("(head)") == 1
        assert "0028_playground_places" in heads
