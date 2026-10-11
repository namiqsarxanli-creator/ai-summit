"""Operator health checks: `python -m app.cli ops-check`.

The public endpoints (`/api/health/live`, `/api/health/ready`) answer one
question - "may traffic come here?" - and nothing else. This module answers the
operator's questions, from a shell on the server (never over HTTP):

* is the database reachable, and at the revision this code expects?
* is the OTP worker's loop turning, and getting anywhere?
* is the OTP outbox backed up, retrying, or giving up on codes?
* can the media volume be written, and how full is it?

Every result is a level (`ok` / `warn` / `crit` / `skip`), a short name and one
line of detail made ONLY of counts, ages, fixed reason codes and error CLASS
names - never an address, a code, a token, a path from a request, a
connection string or provider text. The exit code is the worst level (0 ok,
1 warn, 2 crit), so cron, a watchdog or an uptime tool can act on it.

All thresholds are named in `Thresholds`; the defaults suit the 10-minute code
lifetime (a code that waits minutes for its email is already a failure for
the attendee).
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.core.media import MediaStore

Level = Literal["ok", "warn", "crit", "skip"]
_RANK: dict[str, int] = {"skip": 0, "ok": 0, "warn": 1, "crit": 2}
_SAFE_CODE = re.compile(r"[^a-z0-9_]")

# Failure reasons that mean "everything will fail until someone acts" (the key
# or the sending domain is wrong), not "a message was bounced".
SYSTEMIC_REASONS = frozenset({"provider_auth_rejected", "payload_unreadable"})


@dataclass(frozen=True)
class Thresholds:
    outbox_due_warn_seconds: int = 60
    outbox_due_crit_seconds: int = 180
    outbox_stuck_lease_seconds: int = 120
    recent_window_seconds: int = 900
    expired_warn: int = 1
    expired_crit: int = 5
    failed_warn: int = 1
    failed_crit: int = 10
    worker_stale_seconds: int = 60
    worker_no_progress_seconds: int = 120
    worker_failures_warn: int = 3
    worker_failures_crit: int = 10
    disk_warn_free_fraction: float = 0.15
    disk_crit_free_fraction: float = 0.05


DEFAULT_THRESHOLDS = Thresholds()


@dataclass(frozen=True)
class CheckResult:
    name: str
    level: Level
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "level": self.level, "detail": self.detail}


def exit_code(results: Sequence[CheckResult]) -> int:
    return max((_RANK[r.level] for r in results), default=0)


def format_text(results: Sequence[CheckResult]) -> str:
    return "\n".join(f"{r.level.upper():5} {r.name}: {r.detail}" for r in results)


def format_json(results: Sequence[CheckResult]) -> str:
    worst = exit_code(results)
    return json.dumps(
        {
            "status": ("ok", "warn", "crit")[worst],
            "checks": [r.to_dict() for r in results],
        },
        indent=2,
    )


def _code(value: object) -> str:
    """A reason code made printable-safe (fixed vocabulary in practice)."""
    return _SAFE_CODE.sub("_", str(value).lower())[:40] or "unknown"


# --- database -----------------------------------------------------------------


def check_database(engine: Engine) -> CheckResult:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - the class name is the whole diagnosis
        return CheckResult("database", "crit", f"unreachable ({type(exc).__name__})")
    return CheckResult("database", "ok", "reachable")


def code_heads() -> set[str]:
    """The revision(s) this code's migrations end in."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    return set(ScriptDirectory.from_config(config).get_heads())


def check_migrations(db: Session, heads: Callable[[], set[str]] = code_heads) -> CheckResult:
    try:
        current = {r[0] for r in db.execute(text("SELECT version_num FROM alembic_version")).all()}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return CheckResult("migrations", "crit", f"revision unreadable ({type(exc).__name__})")
    expected = heads()
    if current == expected:
        return CheckResult("migrations", "ok", f"at head ({', '.join(sorted(current))})")
    return CheckResult(
        "migrations",
        "warn",
        f"database at {', '.join(sorted(current)) or 'none'}, code expects "
        f"{', '.join(sorted(expected))} - run the migrate step",
    )


# --- OTP outbox -----------------------------------------------------------------

_BACKLOG_SQL = text(
    """
    SELECT
      count(*) FILTER (WHERE status = 'pending' AND next_attempt_at <= now()
                       AND expires_at > now()) AS due,
      EXTRACT(EPOCH FROM now() - min(next_attempt_at) FILTER (
        WHERE status = 'pending' AND next_attempt_at <= now() AND expires_at > now())) AS oldest,
      count(*) FILTER (WHERE status = 'pending' AND attempt_count > 0) AS retrying,
      count(*) FILTER (WHERE status = 'processing'
                       AND locked_until < now() - make_interval(secs => :stuck)) AS stuck
    FROM otp_deliveries
    """
)

_FAILURES_SQL = text(
    """
    SELECT status, coalesce(reason_code, 'unknown'), count(*)
    FROM otp_deliveries
    WHERE status IN ('failed', 'expired')
      AND finished_at > now() - make_interval(secs => :window)
      AND coalesce(reason_code, '') <> 'challenge_consumed'
    GROUP BY 1, 2
    """
)


def check_outbox(db: Session, thresholds: Thresholds = DEFAULT_THRESHOLDS) -> list[CheckResult]:
    try:
        due, oldest, retrying, stuck = db.execute(
            _BACKLOG_SQL, {"stuck": thresholds.outbox_stuck_lease_seconds}
        ).one()
        failures = db.execute(_FAILURES_SQL, {"window": thresholds.recent_window_seconds}).all()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return [CheckResult("outbox", "crit", f"unreadable ({type(exc).__name__})")]
    finally:
        db.rollback()  # read-only: end the snapshot, hold nothing

    age = int(oldest) if oldest is not None else 0
    backlog_level: Level = "ok"
    notes = [f"due now={int(due)}", f"oldest due={age}s", f"retrying={int(retrying)}"]
    if age >= thresholds.outbox_due_crit_seconds:
        backlog_level = "crit"
        notes.append("worker stopped or blocked?")
    elif age >= thresholds.outbox_due_warn_seconds:
        backlog_level = "warn"
    if int(stuck) > 0:
        notes.append(f"expired leases={int(stuck)}")
        if backlog_level == "ok":
            backlog_level = "warn"
    results = [CheckResult("outbox-backlog", backlog_level, ", ".join(notes))]

    expired = failed = 0
    systemic: list[str] = []
    parts: list[str] = []
    for status, reason, n in failures:
        code, n = _code(reason), int(n)
        parts.append(f"{status}:{code}={n}")
        if status == "expired":
            expired += n
        else:
            failed += n
        if code in SYSTEMIC_REASONS:
            systemic.append(code)
    level: Level = "ok"
    if systemic or expired >= thresholds.expired_crit or failed >= thresholds.failed_crit:
        level = "crit"
    elif expired >= thresholds.expired_warn or failed >= thresholds.failed_warn:
        level = "warn"
    window = thresholds.recent_window_seconds
    detail = f"last {window}s: " + (", ".join(sorted(parts)) if parts else "none")
    if systemic:
        detail += f" (systemic: {', '.join(sorted(set(systemic)))})"
    results.append(CheckResult("outbox-failures", level, detail))
    return results


# --- the worker process -----------------------------------------------------------


def check_worker(
    status_file: Path,
    *,
    now: float | None = None,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    heartbeat_file: Path | None = None,
) -> CheckResult:
    """Reads the files the worker writes. They live in the worker container,
    so this only means something when run there (`docker compose exec worker
    python -m app.cli ops-check --worker`)."""
    clock = time.time() if now is None else now
    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
        loop_at = float(data["loop_at"])
    except FileNotFoundError:
        # an older worker (or one just started) only has the heartbeat file
        if heartbeat_file is not None and heartbeat_file.exists():
            age = clock - heartbeat_file.stat().st_mtime
            if age > thresholds.worker_stale_seconds:
                return CheckResult("worker", "crit", f"heartbeat stale ({int(age)}s)")
            return CheckResult("worker", "ok", f"heartbeat {int(age)}s ago (no status file yet)")
        return CheckResult("worker", "crit", "no heartbeat found (worker not running here?)")
    except (OSError, ValueError, KeyError, TypeError):
        return CheckResult("worker", "crit", "status file unreadable")

    stale = clock - loop_at
    if stale > thresholds.worker_stale_seconds:
        return CheckResult("worker", "crit", f"loop stalled ({int(stale)}s since last turn)")
    if data.get("mode") == "idle":
        return CheckResult("worker", "ok", "idle (OTP outbox not configured)")

    notes = [f"loop {int(stale)}s ago"]
    level: Level = "ok"
    failures = int(data.get("consecutive_failures") or 0)
    if failures:
        notes.append(f"failed polls in a row={failures}")
        if failures >= thresholds.worker_failures_crit:
            level = "crit"
        elif failures >= thresholds.worker_failures_warn:
            level = "warn"
    last_ok = data.get("last_ok_at")
    if last_ok is not None and clock - float(last_ok) > thresholds.worker_no_progress_seconds:
        notes.append(f"no successful poll for {int(clock - float(last_ok))}s")
        level = "crit"
    paused = float(data.get("paused_for_seconds") or 0)
    if paused > 0:
        notes.append(f"paused by provider rate limit for {int(paused)}s")
        if level == "ok":
            level = "warn"
    return CheckResult("worker", level, ", ".join(notes))


# --- media ---------------------------------------------------------------------------


def check_media(
    store: MediaStore, thresholds: Thresholds = DEFAULT_THRESHOLDS
) -> list[CheckResult]:
    results: list[CheckResult] = []
    if store.check_writable():
        results.append(CheckResult("media-write", "ok", "writable"))
    else:
        results.append(
            CheckResult(
                "media-write",
                "crit",
                "MEDIA_ROOT is not writable (missing, read-only, full or wrong owner): "
                "photo uploads answer 503",
            )
        )
    try:
        usage = shutil.disk_usage(store.root)
    except OSError as exc:
        results.append(CheckResult("media-space", "warn", f"unknown ({type(exc).__name__})"))
        return results
    free = usage.free / usage.total if usage.total else 0.0
    level: Level = "ok"
    if free < thresholds.disk_crit_free_fraction:
        level = "crit"
    elif free < thresholds.disk_warn_free_fraction:
        level = "warn"
    results.append(
        CheckResult("media-space", level, f"{free:.0%} free ({usage.free // (1024 * 1024)} MiB)")
    )
    return results
