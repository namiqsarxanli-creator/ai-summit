"""M02 acceptance: the model registry matches the architecture inventory,
and the migrations produce byte-identical schema to the models (no
drift) — reproducible from pytest, not only from a manual CLI run.
"""

from __future__ import annotations

import subprocess
import sys

from app.db import all_models  # noqa: F401 — registers every model with Base.metadata
from app.db.base import Base

EXPECTED_TABLES = {
    "users",
    "interests",
    "user_interests",
    "booths",
    "booth_interests",
    "contact_cards",
    "staff_accounts",
    "sessions",
    "tasks",
    "task_preset_points",
    "feedback",
    "points_log",
    "idempotency_records",
    "event_config",
    "otp_challenges",
    "consent_audit",
    "admin_audit",
}


def test_all_seventeen_architecture_tables_are_registered() -> None:
    """ARCHITECTURE.md §7 + §8.2, plus Implementation Clarification C4
    (admin_audit) — IMPLEMENTATION_PLAN.md M02's own 16-row table plus
    the one added entity. Exactly 17, no more, no fewer."""
    assert set(Base.metadata.tables.keys()) == EXPECTED_TABLES
    assert len(EXPECTED_TABLES) == 17


def test_no_model_migration_drift() -> None:
    """Shells out to `alembic check` — the same command used for manual
    verification — so this is reproducible in CI, not only by hand.
    Requires migrations to already be at head; if this fails with
    "Target database is not up to date" rather than a real diff, run
    `alembic upgrade head` first.
    """
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell, no external input
        [sys.executable, "-m", "alembic", "check"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"alembic check reported drift or an error:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
