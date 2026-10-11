"""Migration 0011 (audit view indexes) on a database that already holds audit rows.

Builds its own scratch database at 0010 (tests/suite_db.py), plants audit rows,
upgrades and downgrades, and checks the migration only swaps indexes: no audit
row is read back changed, none is lost, and the model and the migration agree.
"""

from __future__ import annotations

import psycopg

from tests import suite_db
from tests.test_admin_lists_migration import _alembic, _index_names

NEW_INDEXES = {
    "ix_admin_audit_created_at_id",
    "ix_admin_audit_actor_created_at_id",
    "ix_admin_audit_action_created_at_id",
    "ix_admin_audit_target_created_at_id",
}
OLD_INDEXES = {"ix_admin_audit_created_at", "ix_admin_audit_staff_account_id"}


def _rows(dsn: str) -> list[tuple[object, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT id, staff_account_id, action, target_type, target_id, "
            "before_value, after_value, created_at FROM admin_audit ORDER BY id"
        ).fetchall()


def test_0011_only_swaps_indexes_and_keeps_every_audit_row() -> None:
    with suite_db.scratch_database("auditidx", migrate_to="0010_admin_lists") as scratch:
        dsn = scratch.psycopg_url
        assert _index_names(dsn) >= OLD_INDEXES
        assert not (NEW_INDEXES & _index_names(dsn))
        with psycopg.connect(dsn) as conn:
            staff_id = conn.execute(
                "INSERT INTO staff_accounts (email, password_hash, full_name, role) "
                "VALUES ('old-admin@x.test', 'h', 'Old Admin', 'ADMIN') RETURNING id"
            ).fetchone()
            assert staff_id is not None
            for n in range(5):
                conn.execute(
                    "INSERT INTO admin_audit (staff_account_id, action, target_type, target_id, "
                    "before_value, after_value) VALUES (%s, 'booth.update', 'booth', %s, "
                    '\'{"name": "a"}\', \'{"name": "b"}\')',
                    (staff_id[0], str(n)),
                )
            conn.commit()
        before = _rows(dsn)
        assert len(before) == 5

        _alembic(scratch.url, "upgrade", "0011_audit_view")
        assert "0011_audit_view" in _alembic(scratch.url, "current")
        names = _index_names(dsn)
        assert names >= NEW_INDEXES
        assert not (OLD_INDEXES & names)  # the two the new ones fully cover
        assert _rows(dsn) == before

        # the model and the migrations agree (no drift) - at the newest revision
        _alembic(scratch.url, "upgrade", "head")
        _alembic(scratch.url, "check")

        _alembic(scratch.url, "downgrade", "0010_admin_lists")
        names = _index_names(dsn)
        assert names >= OLD_INDEXES
        assert not (NEW_INDEXES & names)
        assert _rows(dsn) == before

        _alembic(scratch.url, "upgrade", "head")
        assert _index_names(dsn) >= NEW_INDEXES
        assert _rows(dsn) == before
