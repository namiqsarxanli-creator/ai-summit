"""The operator's status page: is each part of the system working, and where did
the server fail (`GET /api/admin/system-status`, administrators only).

For the system's operator (AIWorks), not the event organiser. It joins what
`python -m app.cli ops-check` reports from a shell - database, schema revision,
sign-in code queue, photo storage - with this process's admission state and its
error log (app/core/error_log.py). Counts, ages, fixed reason codes and masked
paths only: nothing personal, no address, code, token or connection string.

If the whole server is down this page cannot load either; the cron monitor
(deploy/monitoring/monitor.sh) covers that from outside the application.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core import admission, ops_status
from app.core.config import get_settings
from app.core.error_log import ERRORS
from app.core.media import get_store


def checks() -> list[ops_status.CheckResult]:
    # Its own short-lived engine with hard timeouts (as `ops-check`): a status
    # page that hangs on a dead database would say nothing.
    engine = create_engine(
        get_settings().database_url,
        poolclass=NullPool,
        connect_args={"connect_timeout": 5, "options": "-c statement_timeout=5000"},
    )
    results = [ops_status.check_database(engine)]
    try:
        if results[0].level == "ok":
            with Session(engine) as db:
                results.append(ops_status.check_migrations(db))
                results.extend(ops_status.check_outbox(db))
    finally:
        engine.dispose()
    from app.modules.auth.email_providers import configured

    names = [name for name, _ in configured(get_settings())]
    results.append(
        ops_status.CheckResult(
            "email-providers",
            "ok" if names else "crit",
            " -> ".join(names) if names else "none configured",
        )
    )
    try:
        results.extend(ops_status.check_media(get_store()))
    except Exception as exc:  # noqa: BLE001 - the class name is the diagnosis
        results.append(
            ops_status.CheckResult("media", "crit", f"unreadable ({type(exc).__name__})")
        )
    return results


def report() -> dict[str, Any]:
    results = checks()
    worst = ops_status.exit_code(results)
    errors = ERRORS.snapshot()
    busy = dict(admission.STATE)
    # Server errors in the last 15 minutes make the overall state at least a warning.
    recent = sum(s["last_15_min"] for s in errors["sections"].values())
    if recent and worst == 0:
        worst = 1
    return {
        "status": ("ok", "warn", "crit")[worst],
        "checks": [r.to_dict() for r in results],
        "admission": busy,
        "errors": errors,
    }
