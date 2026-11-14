"""CI safety guard: `python -m tests.ci_guard`.

The CI workflow gives the test run its OWN throw-away PostgreSQL service and
fake credentials, all written literally in the workflow file. This guard is the
first step of the backend job and fails the run - before anything connects
anywhere - if the environment looks like something else: a database that is
not the job's local service, a real e-mail key, an encryption key, or the
production environment. It exists so that a future edit (a secret wired in by
mistake, a copied production URL) cannot quietly turn CI into something that
touches real data or sends real mail.

It prints variable NAMES and the rule that failed, never a value.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

# The job's own service container is reachable as localhost (published port) or
# by its service name. Nothing else is a CI database.
ALLOWED_DB_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres"})
# The maintenance database of that service; the suite only uses it to CREATE and
# DROP its own uniquely named databases (tests/suite_db.py).
REQUIRED_DB_PREFIX = "ci_"
# Must be absent (or empty) in CI: a value means a real credential got in.
FORBIDDEN_NON_EMPTY = (
    "RESEND_API_KEY",
    "OTP_OUTBOX_ENCRYPTION_KEY",
    "OTP_OUTBOX_ENCRYPTION_KEY_PREVIOUS",
)


def check_environment(env: Mapping[str, str]) -> list[str]:
    """Every violated rule, as a sentence naming variables only."""
    problems: list[str] = []

    raw = env.get("DATABASE_URL", "")
    if not raw:
        problems.append("DATABASE_URL is not set")
    else:
        try:
            url = make_url(raw)
        except ArgumentError:
            problems.append("DATABASE_URL is not a valid database URL")
        else:
            if (url.host or "").lower() not in ALLOWED_DB_HOSTS:
                problems.append(
                    "DATABASE_URL must point at the job's own PostgreSQL service "
                    f"(host one of: {', '.join(sorted(ALLOWED_DB_HOSTS))})"
                )
            if not (url.database or "").startswith(REQUIRED_DB_PREFIX):
                problems.append(
                    f"DATABASE_URL's database name must start with {REQUIRED_DB_PREFIX!r}"
                )

    for name in FORBIDDEN_NON_EMPTY:
        if env.get(name, "").strip():
            problems.append(
                f"{name} must not be set in CI (no real credential may reach the tests)"
            )

    if env.get("ENVIRONMENT", "development").strip().lower() == "production":
        problems.append("ENVIRONMENT must not be production in CI")
    return problems


def main() -> int:
    problems = check_environment(os.environ)
    if problems:
        print("CI environment guard FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("CI environment guard passed (local throw-away database, no real credentials).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
