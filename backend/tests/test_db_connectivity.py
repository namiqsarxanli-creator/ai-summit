"""M01 acceptance: the backend can reach the real PostgreSQL container.

This test requires a live database (the ``db`` Compose service) and is
run inside the backend container, where DATABASE_URL already points at
it. It is deliberately not mocked — a mocked connection would prove
nothing about M01's actual acceptance criterion ("backend connects to
PostgreSQL").
"""

from __future__ import annotations

from app.db.session import database_is_reachable


def test_database_is_reachable() -> None:
    assert database_is_reachable() is True
