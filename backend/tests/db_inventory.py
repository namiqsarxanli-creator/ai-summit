"""Read-only inventory of test databases a dead run may have left behind.

    docker compose exec backend python -m tests.db_inventory

This NEVER drops, creates or alters anything: the connection is opened with
`default_transaction_read_only=on` and only SELECTs catalog views. It prints
the commands a person can run by hand, after checking that nothing is using a
database. Deliberately no `--delete` flag and no automatic sweep exists: a
broad "drop everything that looks like a test database" is exactly what let
parallel runs destroy each other.

A database is listed only if its name is one the test tooling creates
(`facemark_test_<purpose>_<12 hex>`) or one of the three fixed names older
versions used. The application's own database is never listed.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import psycopg

from tests.suite_db import NAME_PATTERN, database_of, psycopg_url

# Used by earlier versions of the tooling, one fixed name each.
LEGACY_FIXED_NAMES = (
    "facemark_test_suite",
    "facemark_m15_reset_test",
    "facemark_m20_migration_test",
)

_INVENTORY_SQL = """
SELECT d.datname,
       pg_database_size(d.datname) AS size_bytes,
       (SELECT count(*) FROM pg_stat_activity a WHERE a.datname = d.datname) AS connections
  FROM pg_database d
 WHERE NOT d.datistemplate
   AND (d.datname ~ %(pattern)s OR d.datname = ANY(%(legacy)s))
   AND d.datname <> %(main)s
 ORDER BY d.datname
"""


@dataclass(frozen=True)
class Leftover:
    name: str
    size_bytes: int
    connections: int
    legacy: bool


def find_leftovers(
    conn: psycopg.Connection[tuple[object, ...]], main_database: str
) -> list[Leftover]:
    rows = conn.execute(
        _INVENTORY_SQL,
        {"pattern": NAME_PATTERN, "legacy": list(LEGACY_FIXED_NAMES), "main": main_database},
    ).fetchall()
    return [
        Leftover(
            name=str(name),
            size_bytes=int(size),  # type: ignore[call-overload]
            connections=int(connections),  # type: ignore[call-overload]
            legacy=str(name) in LEGACY_FIXED_NAMES,
        )
        for name, size, connections in rows
    ]


def render(leftovers: list[Leftover]) -> str:
    if not leftovers:
        return "No leftover test databases found.\n"
    lines = [f"{len(leftovers)} test database(s) found (nothing was changed):", ""]
    for item in leftovers:
        kind = "older fixed name" if item.legacy else "per-run name"
        state = (
            f"{item.connections} open connection(s) - probably a run is STILL USING IT"
            if item.connections
            else "no open connections"
        )
        lines.append(f"  {item.name}   [{kind}, {item.size_bytes // 1024} KiB, {state}]")
    lines += [
        "",
        "To remove one by hand (you decide; this tool never does):",
        "  1. Make sure no test run is in progress. A run that is still going owns its",
        "     database and would fail if it disappeared.",
        "  2. Make sure the line above shows 'no open connections'.",
        "  3. Connect to the 'postgres' database as a role that may drop it and run, with the",
        "     name copied EXACTLY from the list above (double quotes included):",
        "",
    ]
    lines += [f'       DROP DATABASE "{item.name}";' for item in leftovers if not item.connections]
    lines += [
        "",
        "     e.g.  docker compose exec db psql -U <database role> -d postgres",
        "     Never drop the application's own database.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 2
    main_database = database_of(url) or ""
    with psycopg.connect(
        psycopg_url(url), autocommit=True, options="-c default_transaction_read_only=on"
    ) as conn:
        print(render(find_leftovers(conn, main_database)), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
