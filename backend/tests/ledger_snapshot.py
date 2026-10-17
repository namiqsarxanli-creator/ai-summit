"""Ledger snapshots that survive schema growth, for the migration tests.

A `SELECT *` taken before an upgrade and after it is not comparable once a later
revision adds a column. These helpers record the column names that existed
BEFORE the upgrade and read exactly those columns afterwards - by name, safely
quoted - so every historical value is still compared one by one; the columns
added since are reported separately, so a test can check what they hold for
old rows (they must not invent evidence).
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg import sql


def columns(conn: psycopg.Connection[Any], table: str) -> list[str]:
    return [
        str(r[0])
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
            (table,),
        )
    ]


def rows(
    conn: psycopg.Connection[Any], table: str, names: list[str], *, order_by: str = "id"
) -> list[tuple[Any, ...]]:
    query = sql.SQL("SELECT {cols} FROM {table} ORDER BY {order}").format(
        cols=sql.SQL(", ").join(sql.Identifier(n) for n in names),
        table=sql.Identifier(table),
        order=sql.Identifier(order_by),
    )
    return list(conn.execute(query).fetchall())


def added_values(
    conn: psycopg.Connection[Any], table: str, before: list[str], ids: list[int]
) -> dict[str, list[Any]]:
    """What the columns added since `before` hold for the rows `ids`."""
    added = [c for c in columns(conn, table) if c not in before]
    if not added:
        return {}
    query = sql.SQL("SELECT {cols} FROM {table} WHERE id = ANY(%s) ORDER BY id").format(
        cols=sql.SQL(", ").join(sql.Identifier(n) for n in added),
        table=sql.Identifier(table),
    )
    got = conn.execute(query, (ids,)).fetchall()
    return {name: [r[i] for r in got] for i, name in enumerate(added)}
