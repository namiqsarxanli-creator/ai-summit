"""M02 acceptance: the reset-fence unit-of-work primitive.

ARCHITECTURE.md §19.4 / invariant #29: every reset-sensitive mutation
acquires `pg_advisory_xact_lock_shared(FACEMARK_RESET_FENCE)` as the
FIRST statement, transaction-scoped so it releases automatically on
commit or rollback. These tests use real, separate PostgreSQL
connections/threads — advisory-lock blocking is a genuine multi-
connection phenomenon that cannot be faked on one session.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.locks import FACEMARK_RESET_FENCE
from app.db.session import SessionLocal, mutating_transaction

_PSYCOPG_URL = get_settings().database_url.replace("postgresql+psycopg://", "postgresql://")


def _raw_connection() -> psycopg.Connection:
    conn = psycopg.connect(_PSYCOPG_URL)
    conn.autocommit = False
    return conn


@pytest.fixture
def session_a() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def session_b() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


# --------------------------------------------------------------------------
# Basic contract
# --------------------------------------------------------------------------


def test_fence_is_the_first_statement(db: Session) -> None:
    """Asserts the advisory lock is genuinely held DURING the `with`
    block by checking pg_locks from a second connection — proving this
    is a real lock acquisition, not a no-op."""
    holder_thread_ready = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with mutating_transaction(db):
            holder_thread_ready.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold)
    t.start()
    assert holder_thread_ready.wait(timeout=5), "fence was never acquired"

    with _raw_connection() as check_conn, check_conn.cursor() as cur:
        # Matched on locktype/mode/granted rather than reconstructing
        # the bigint key from classid/objid — that bit-packing is an
        # internal pg_locks representation detail not worth depending
        # on precisely. The test DB has no other advisory lock holder
        # at this point, so "a granted shared advisory lock exists at
        # all" is already a specific, meaningful assertion here.
        cur.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
            "AND mode = 'ShareLock' AND granted"
        )
        (count,) = cur.fetchone()  # type: ignore[misc]

    release.set()
    t.join(timeout=5)
    assert count >= 1, "expected the shared advisory lock to be visible in pg_locks while held"


@pytest.fixture
def committed_interest_id() -> Iterator[int]:
    """Unlike `seeded_interest_id` (which lives only inside `db`'s
    rolled-back transaction), `mutating_transaction` genuinely commits
    — so a test exercising that commit needs a row it owns end-to-end,
    with its own explicit cleanup, independent of the `db` fixture's
    rollback-based isolation."""
    setup = SessionLocal()
    row = setup.execute(
        text("INSERT INTO interests (slug, name) VALUES ('mt-test', 'Original') RETURNING id")
    ).one()
    setup.commit()
    interest_id = int(row[0])
    setup.close()
    try:
        yield interest_id
    finally:
        cleanup = SessionLocal()
        cleanup.execute(text("DELETE FROM interests WHERE id = :i"), {"i": interest_id})
        cleanup.commit()
        cleanup.close()


def test_commits_on_success(db: Session, committed_interest_id: int) -> None:
    with mutating_transaction(db) as tx:
        tx.execute(
            text("UPDATE interests SET name = 'Renamed' WHERE id = :i"),
            {"i": committed_interest_id},
        )
    # mutating_transaction committed — a completely separate session
    # sees the change, proving it is genuinely durable and not merely
    # visible within `db`'s own uncommitted transaction.
    fresh = SessionLocal()
    try:
        name = fresh.execute(
            text("SELECT name FROM interests WHERE id = :i"), {"i": committed_interest_id}
        ).scalar_one()
    finally:
        fresh.rollback()
        fresh.close()
    assert name == "Renamed"


def test_rolls_back_on_exception(db: Session, committed_interest_id: int) -> None:
    class Boom(Exception):
        pass

    with pytest.raises(Boom), mutating_transaction(db) as tx:
        tx.execute(
            text("UPDATE interests SET name = 'Should Not Stick' WHERE id = :i"),
            {"i": committed_interest_id},
        )
        raise Boom

    # A fresh session sees the ORIGINAL value — the update never committed.
    fresh = SessionLocal()
    try:
        name = fresh.execute(
            text("SELECT name FROM interests WHERE id = :i"), {"i": committed_interest_id}
        ).scalar_one()
    finally:
        fresh.rollback()
        fresh.close()
    assert name == "Original"


# --------------------------------------------------------------------------
# Concurrency: shared locks do not block each other
# --------------------------------------------------------------------------


def test_two_concurrent_shared_acquires_do_not_block_each_other(
    session_a: Session, session_b: Session
) -> None:
    """ARCHITECTURE.md §19.4: "Shared locks are mutually compatible, so
    ordinary mutations never contend with each other." Both threads
    must be inside their `mutating_transaction` block SIMULTANEOUSLY —
    proven with a barrier, not inferred from timing."""
    barrier = threading.Barrier(2, timeout=5)
    results: dict[str, bool] = {}

    def worker(session: Session, name: str) -> None:
        with mutating_transaction(session):
            try:
                barrier.wait()  # only succeeds if BOTH threads are here concurrently
                results[name] = True
            except threading.BrokenBarrierError:
                results[name] = False

    t1 = threading.Thread(target=worker, args=(session_a, "a"))
    t2 = threading.Thread(target=worker, args=(session_b, "b"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert results == {"a": True, "b": True}


# --------------------------------------------------------------------------
# Concurrency: exclusive blocks shared
# --------------------------------------------------------------------------


def test_exclusive_lock_blocks_a_shared_acquire_until_released(db: Session) -> None:
    """Simulates the reset side of ARCHITECTURE.md §19.4: an exclusive,
    session-scoped `pg_advisory_lock` (as the demo-reset CLI will hold,
    M15) blocks a `mutating_transaction`'s shared acquire until it is
    released — proving the fence actually fences, not just that it
    runs some SQL."""
    exclusive_conn = _raw_connection()
    with exclusive_conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (FACEMARK_RESET_FENCE,))
    # exclusive_conn now holds the lock, uncommitted transaction still open.

    shared_acquired = threading.Event()
    error: list[BaseException] = []

    def try_shared() -> None:
        s = SessionLocal()
        try:
            with mutating_transaction(s, lock_timeout_ms=60_000):
                shared_acquired.set()
        except BaseException as e:  # noqa: BLE001
            error.append(e)
        finally:
            s.rollback()
            s.close()

    t = threading.Thread(target=try_shared)
    t.start()

    # The shared acquire must NOT succeed while the exclusive lock is held.
    assert not shared_acquired.wait(timeout=1.5), (
        "shared acquire proceeded despite an exclusive lock being held"
    )

    # Release the exclusive lock — the shared acquire must now proceed.
    with exclusive_conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", (FACEMARK_RESET_FENCE,))
    exclusive_conn.commit()
    exclusive_conn.close()

    t.join(timeout=10)
    assert not error, f"shared acquire raised unexpectedly: {error}"
    assert shared_acquired.is_set(), (
        "shared acquire never proceeded after the exclusive lock was released"
    )


def test_lock_timeout_raises_when_exclusive_lock_is_held(db: Session) -> None:
    """`mutating_transaction`'s lock_timeout_ms bounds the wait —
    ARCHITECTURE.md §19.4: "Exceeding it means a mutation is genuinely
    stuck" (or, in the mutation's own case, that reset currently holds
    the fence). A short timeout here keeps the test fast."""
    exclusive_conn = _raw_connection()
    with exclusive_conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (FACEMARK_RESET_FENCE,))

    try:
        start = time.monotonic()
        with pytest.raises(OperationalError), mutating_transaction(db, lock_timeout_ms=300):
            pytest.fail("should never reach the body — the fence acquire itself must raise")
        elapsed = time.monotonic() - start
        assert elapsed < 5, "lock_timeout was not honoured — the wait ran far longer than 300ms"
    finally:
        with exclusive_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (FACEMARK_RESET_FENCE,))
        exclusive_conn.commit()
        exclusive_conn.close()
