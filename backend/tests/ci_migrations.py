"""CI migration validation: `python -m tests.ci_migrations`.

Runs the REAL Alembic chain, but only ever against a temporary database that
this process creates and drops itself (tests/suite_db.py: unique name, dropped
by its owner only). `DATABASE_URL` is used solely to open the connection that
issues CREATE / DROP DATABASE; no migration ever runs against it.

What it proves, in order:
  1. the chain has exactly one head (no two branches waiting to be merged);
  2. `upgrade head` on an empty database succeeds and the database ends at it;
  3. running it again changes nothing;
  4. `downgrade base` then `upgrade head` works on an empty schema, so every
     migration's downgrade still matches its upgrade;
  5. (the model/migration drift check, `alembic check`, is part of the test suite:
     tests/test_schema_metadata.py).

Output names steps and revisions only - never a URL or a password.
"""

from __future__ import annotations

import os

import psycopg

from tests import suite_db


def _heads() -> set[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(suite_db._BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(suite_db._BACKEND_ROOT / "alembic"))
    return set(ScriptDirectory.from_config(config).get_heads())


def _revision(db: suite_db.ScratchDatabase) -> str | None:
    with psycopg.connect(db.psycopg_url) as conn:
        exists = conn.execute("SELECT to_regclass('public.alembic_version')").fetchone()
        if exists is None or exists[0] is None:
            return None
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        return None if row is None else str(row[0])


def _alembic(db: suite_db.ScratchDatabase, *args: str) -> None:
    env = dict(os.environ, DATABASE_URL=db.url)
    suite_db._run_step(["alembic", *args], env, db.url, db.maintenance_url)


def main() -> int:
    heads = _heads()
    if len(heads) != 1:
        print(f"FAIL  the migration chain has {len(heads)} heads: {', '.join(sorted(heads))}")
        return 1
    (head,) = heads
    print(f"ok    one head: {head}")

    with suite_db.scratch_database("migr", migrate_to=None) as db:
        try:
            _alembic(db, "upgrade", "head")
            current = _revision(db)
            if current != head:
                print(f"FAIL  after upgrade the database is at {current}, expected {head}")
                return 1
            print("ok    upgrade head on an empty database")

            _alembic(db, "upgrade", "head")
            print("ok    a second upgrade head is a no-op")

            _alembic(db, "downgrade", "base")
            if _revision(db) is not None:
                print("FAIL  downgrade base left a revision behind")
                return 1
            print("ok    downgrade base")

            _alembic(db, "upgrade", "head")
            if _revision(db) != head:
                print("FAIL  re-upgrade after a full downgrade did not reach head")
                return 1
            print("ok    upgrade head again after a full downgrade")
        except suite_db.SetupFailed as err:
            print(f"FAIL  {err}")
            return 1
    print("Migrations validated on a temporary database (dropped).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
