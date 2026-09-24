"""M15 test support: helpers for the ISOLATED PostgreSQL database
destructive reset tests run against.

`app/modules/admin/reset.py` genuinely `TRUNCATE`s `users ... RESTART
IDENTITY CASCADE` — running that against the same database the rest of
this suite (and the developer's own running stack) uses would destroy
real, non-test data. Every destructive M15 test in `tests/test_reset.py`
therefore runs against its own dedicated database, created once per
test session on the SAME PostgreSQL server (the `facemark` role has
`CREATEDB`), migrated with the real Alembic chain, and dropped at the
end of the run. Nothing in this module ever opens a connection to the
real configured database.

The actual pytest fixtures (`reset_db_url`, `reset_db_engine`,
`clean_reset_db`) live in `conftest.py`, not here — a fixture used as a
test-function PARAMETER must be visible to pytest's fixture discovery
without an explicit import, or (as tried initially) importing it by
name to make that work makes ruff flag every test's parameter as
"redefining" the import. Only plain helpers/constants live here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg

from app.core.config import get_settings

RESET_TEST_DB_NAME = "facemark_m15_reset_test"

SEED_ADMIN_PASSWORD = "m15-test-admin-password"
SEED_STAFF_PASSWORD = "m15-test-staff-password"

BACKEND_ROOT = Path(__file__).resolve().parent.parent

# Every table `app.modules.admin.reset` either truncates or reconciles,
# plus `event_config` itself — truncating ALL of them between tests is
# what lets `clean_reset_db` (conftest.py) guarantee a byte-identical
# starting point for every test, including ones that assert
# reconciliation removes rehearsal extras the PREVIOUS test may have
# inserted.
ALL_RESET_RELEVANT_TABLES: tuple[str, ...] = (
    "points_log",
    "idempotency_records",
    "feedback",
    "sessions",
    "otp_challenges",
    "consent_audit",
    "user_interests",
    "contact_cards",
    "users",
    "admin_audit",
    "booth_interests",
    "tasks",
    "task_preset_points",
    "staff_accounts",
    "booths",
    "interests",
)


def admin_dsn() -> str:
    """A plain `postgresql://` DSN to the REAL configured database,
    used only to issue `CREATE DATABASE`/`DROP DATABASE` — DDL that
    cannot run inside a transaction block and needs an autocommit
    connection to some existing database on the same server."""
    return get_settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def isolated_url() -> str:
    """SQLAlchemy-style URL (`postgresql+psycopg://...`) for the
    isolated reset-test database, derived from the real configured URL
    so credentials/host are never duplicated as a second literal."""
    base, _, _dbname = get_settings().database_url.rpartition("/")
    return f"{base}/{RESET_TEST_DB_NAME}"


def create_isolated_database() -> None:
    with psycopg.connect(admin_dsn(), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {RESET_TEST_DB_NAME} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {RESET_TEST_DB_NAME}")


def drop_isolated_database() -> None:
    with psycopg.connect(admin_dsn(), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {RESET_TEST_DB_NAME} WITH (FORCE)")


def migrate_isolated_database(database_url: str) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_ROOT),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def subprocess_env(database_url: str, **overrides: str) -> dict[str, str]:
    """The environment for a genuine child OS process (CLI or Uvicorn)
    under test — a copy of this process's own environment (already
    carries every required setting inside the backend container — see
    the module docstring) with `DATABASE_URL` repointed at the isolated
    database and any test-specific overrides applied."""
    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    env["SEED_ADMIN_PASSWORD"] = SEED_ADMIN_PASSWORD
    env["SEED_STAFF_PASSWORD"] = SEED_STAFF_PASSWORD
    env.setdefault("DEMO_MODE", "true")
    env.setdefault("ENVIRONMENT", "development")
    env.update(overrides)
    return env
