"""The test-database lifecycle itself (tests/suite_db.py, tests/db_inventory.py).

These tests create their own small, uniquely named scratch databases and
only ever drop databases they created. They cover what once went wrong: runs
sharing one fixed name and dropping each other's database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy.engine import make_url

from tests import db_inventory, suite_db
from tests.suite_db import (
    ScratchDatabase,
    SetupFailed,
    UnsafeDropError,
)

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _maintenance() -> str:
    # After conftest's provisioning DATABASE_URL is THIS run's own suite database.
    return os.environ["DATABASE_URL"]


def _exists(name: str) -> bool:
    with psycopg.connect(suite_db.psycopg_url(_maintenance()), autocommit=True) as conn:
        return (
            conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
            is not None
        )


def _raw_create(name: str) -> int:
    """A database this run did NOT create through suite_db (a bystander)."""
    with psycopg.connect(suite_db.psycopg_url(_maintenance()), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
        row = conn.execute("SELECT oid FROM pg_database WHERE datname = %s", (name,)).fetchone()
        assert row is not None
        return int(row[0])


def _raw_drop(name: str) -> None:
    with psycopg.connect(suite_db.psycopg_url(_maintenance()), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def made() -> Iterator[list[ScratchDatabase]]:
    """Databases a test created through suite_db; dropped afterwards."""
    items: list[ScratchDatabase] = []
    yield items
    for db in items:
        suite_db._drop_quietly(db)


def _forged(name: str, oid: int = 1, protected: frozenset[str] = frozenset()) -> ScratchDatabase:
    return ScratchDatabase(
        name=name, oid=oid, url=_maintenance(), maintenance_url=_maintenance(), protected=protected
    )


# --- this run's own wiring -------------------------------------------------


def test_the_suite_runs_in_a_uniquely_named_database_it_owns() -> None:
    name = suite_db.database_of(os.environ["DATABASE_URL"])
    assert name is not None
    assert suite_db._NAME_RE.fullmatch(name), name
    assert name not in {"facemark", "facemark_test_suite"}  # not the app's, not the old fixed one
    assert name in suite_db._OWNED
    assert suite_db._OWNED[name].name == name


# --- names -----------------------------------------------------------------


def test_names_are_unique_well_formed_and_inside_the_identifier_limit() -> None:
    names = {suite_db.new_name("suite") for _ in range(500)}
    assert len(names) == 500
    for name in names:
        assert suite_db._NAME_RE.fullmatch(name)
        assert len(name.encode()) <= 63  # PostgreSQL's identifier limit
    assert len(suite_db.new_name("a" * 16).encode()) <= 63  # the longest purpose allowed


@pytest.mark.parametrize(
    "purpose", ["", "Suite", "a-b", "9abc", "a" * 17, 'x"; DROP DATABASE facemark; --', "a b"]
)
def test_a_purpose_that_could_break_an_identifier_is_refused(purpose: str) -> None:
    with pytest.raises(ValueError):
        suite_db.new_name(purpose)


# --- parallel runs ----------------------------------------------------------


def test_two_runs_creating_at_the_same_moment_get_different_databases(
    made: list[ScratchDatabase],
) -> None:
    barrier = threading.Barrier(2)
    results: list[ScratchDatabase] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(suite_db.create("par", maintenance_url=_maintenance()))
        except BaseException as err:  # noqa: BLE001 - reported below
            errors.append(err)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    made.extend(results)

    assert not errors, errors
    assert len(results) == 2
    assert results[0].name != results[1].name
    assert results[0].oid != results[1].oid
    assert _exists(results[0].name) and _exists(results[1].name)


_CHILD = """
import os, sys
from tests import suite_db
db = suite_db.create("proc", maintenance_url=os.environ["DATABASE_URL"])
print(db.name, flush=True)
if sys.argv[1] == "wait":
    sys.stdin.readline()
    suite_db.drop(db)
    print("dropped", flush=True)
# "leak": exit WITHOUT the normal teardown - the atexit backstop must clean up
"""


def _child(mode: str) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-c", _CHILD, mode],
        cwd=str(BACKEND_ROOT),
        env=dict(os.environ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_one_runs_cleanup_cannot_touch_another_runs_database(
    made: list[ScratchDatabase],
) -> None:
    """A second OS process (a second run) owns a database; this process
    creates and drops its own. The other one must survive and then clean up
    after itself."""
    other = _child("wait")
    try:
        assert other.stdout is not None and other.stdin is not None
        other_name = other.stdout.readline().strip()
        assert suite_db._NAME_RE.fullmatch(other_name), other.stderr and other.stderr.read()

        mine = suite_db.create("proc", maintenance_url=_maintenance())
        made.append(mine)
        assert mine.name != other_name

        suite_db.drop(mine)  # this run's cleanup ...
        assert not _exists(mine.name)
        assert _exists(other_name)  # ... leaves the other run's database alone

        # ... and this process cannot even be told to drop it
        with pytest.raises(UnsafeDropError):
            suite_db.drop(_forged(other_name, oid=0))
        assert _exists(other_name)

        other.stdin.write("\n")
        other.stdin.flush()
        assert other.stdout.readline().strip() == "dropped"
        assert other.wait(timeout=30) == 0
        assert not _exists(other_name)
    finally:
        if other.poll() is None:
            other.kill()
        other.wait(timeout=30)
        for stream in (other.stdin, other.stdout, other.stderr):
            if stream is not None:
                stream.close()


def test_a_process_that_exits_without_teardown_still_drops_only_its_own_database() -> None:
    bystander = suite_db.new_name("bystander")
    _raw_create(bystander)
    try:
        done = _child("leak")
        out, err = done.communicate(timeout=60)
        assert done.returncode == 0, err
        leaked = out.strip()
        assert suite_db._NAME_RE.fullmatch(leaked)
        assert not _exists(leaked)  # its atexit backstop dropped it
        assert _exists(bystander)  # and nothing else with a similar name
    finally:
        _raw_drop(bystander)


# --- what may be dropped ---------------------------------------------------


def test_a_database_with_a_test_like_name_this_run_did_not_create_is_never_dropped() -> None:
    name = suite_db.new_name("stranger")
    oid = _raw_create(name)
    try:
        with pytest.raises(UnsafeDropError, match="did not create"):
            suite_db.drop(_forged(name, oid=oid))
        assert _exists(name)
    finally:
        _raw_drop(name)


def test_a_database_replaced_under_the_same_name_is_not_dropped(
    made: list[ScratchDatabase],
) -> None:
    mine = suite_db.create("swap", maintenance_url=_maintenance())
    made.append(mine)
    _raw_drop(mine.name)
    _raw_create(mine.name)  # someone else's database now answers to the name
    try:
        with pytest.raises(UnsafeDropError, match="replaced"):
            suite_db.drop(mine)
        assert _exists(mine.name)
        assert mine.name not in suite_db._OWNED  # no longer treated as ours
    finally:
        _raw_drop(mine.name)


def test_a_stale_handle_cannot_drop_again(made: list[ScratchDatabase]) -> None:
    mine = suite_db.create("twice", maintenance_url=_maintenance())
    made.append(mine)
    suite_db.drop(mine)
    assert not _exists(mine.name)
    with pytest.raises(UnsafeDropError):  # no longer registered: nothing is run
        suite_db.drop(mine)


@pytest.mark.parametrize(
    "name",
    [
        "facemark",  # the application's database
        "postgres",
        "template0",
        "template1",
        "facemark_test_suite",  # the old fixed name: the new code cannot drop it
        "facemark_m15_reset_test",
        "facemark_test_suite_zz",  # right prefix, wrong shape
        "facemark_test_suite_ABCDEF012345",  # uppercase
        "facemark_test_suite_0123456789abcdef",  # suffix too long
        'facemark_test_x_0123456789ab"; DROP DATABASE facemark; --',
        "FACEMARK_TEST_suite_0123456789ab",
    ],
)
def test_destructive_commands_are_refused_for_anything_but_a_test_name(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_connection(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a connection was opened for a refused drop")

    monkeypatch.setattr(suite_db, "_admin", no_connection)
    with pytest.raises(UnsafeDropError):
        suite_db.drop(_forged(name))


def test_the_application_database_is_refused_even_if_its_name_looks_like_a_test_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_db = "facemark_test_main_0123456789ab"
    monkeypatch.setattr(suite_db, "_MAIN_DATABASE", app_db)
    forged = _forged(app_db)
    monkeypatch.setitem(suite_db._OWNED, app_db, forged)  # even if it were registered
    monkeypatch.setattr(
        suite_db, "_admin", lambda url: pytest.fail("a connection was opened for a refused drop")
    )
    with pytest.raises(UnsafeDropError, match="application"):
        suite_db.drop(forged)


def test_create_never_reuses_a_protected_name(monkeypatch: pytest.MonkeyPatch) -> None:
    taken = suite_db.new_name("clash")
    monkeypatch.setattr(suite_db, "new_name", lambda purpose: taken)
    with pytest.raises(RuntimeError, match="unused"):
        suite_db.create("clash", maintenance_url=_maintenance(), main_database=taken)


# --- failure during setup ---------------------------------------------------


@pytest.mark.parametrize(
    ("fail_on", "error"),
    [
        ("alembic", SetupFailed("migration failed")),
        ("app.cli", SetupFailed("seed failed")),
        ("alembic", KeyboardInterrupt()),
    ],
)
def test_a_failed_migration_or_seed_drops_the_database_that_attempt_created_and_nothing_else(
    fail_on: str,
    error: BaseException,
    made: list[ScratchDatabase],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    survivor = suite_db.create("survivor", maintenance_url=_maintenance())
    made.append(survivor)

    created: list[ScratchDatabase] = []
    real_create = suite_db.create

    def spy_create(*args: Any, **kwargs: Any) -> ScratchDatabase:
        db = real_create(*args, **kwargs)
        created.append(db)
        return db

    def step(args: list[str], env: dict[str, str], *urls: str) -> None:
        if args[0] == fail_on:
            raise error

    monkeypatch.setattr(suite_db, "create", spy_create)
    monkeypatch.setattr(suite_db, "_run_step", step)

    with pytest.raises(type(error)):
        suite_db.prepare("failing", maintenance_url=_maintenance(), seed=True)

    assert len(created) == 1
    assert not _exists(created[0].name)  # its own database is gone ...
    assert created[0].name not in suite_db._OWNED
    assert _exists(survivor.name)  # ... and a sibling is untouched
    assert _exists(suite_db.database_of(_maintenance()) or "")  # as is this run's suite database


def test_a_successful_setup_keeps_its_database_until_dropped(
    made: list[ScratchDatabase], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(suite_db, "_run_step", lambda *a, **k: None)
    db = suite_db.prepare("ok", maintenance_url=_maintenance(), seed=True)
    made.append(db)
    assert _exists(db.name)


def test_a_setup_error_message_carries_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql+psycopg://us%40er:s3cr%40t%2Fpw@db.example:5432/facemark?sslmode=prefer"

    def boom(*args: Any, **kwargs: Any) -> None:
        raise subprocess.CalledProcessError(
            1,
            ["alembic"],
            stderr=f"could not connect: {url} / {suite_db.psycopg_url(url)} / password s3cr@t/pw",
        )

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(SetupFailed) as caught:
        suite_db._run_step(["alembic", "upgrade", "head"], {}, url)
    message = str(caught.value)
    assert "s3cr" not in message and "us%40er" not in message
    assert "***" in message


# --- URLs -------------------------------------------------------------------

AWKWARD = "postgresql+psycopg://us%40er:p%40ss%3Aw%2Ford%25%23%3F@db.example:5433/facemark?sslmode=prefer&application_name=fm%20test"


def test_the_database_is_swapped_without_disturbing_credentials_or_query() -> None:
    new = suite_db.with_database(AWKWARD, "facemark_test_suite_0123456789ab")
    parsed = make_url(new)
    assert parsed.drivername == "postgresql+psycopg"
    assert parsed.username == "us@er"
    assert parsed.password == "p@ss:w/ord%#?"
    assert (parsed.host, parsed.port) == ("db.example", 5433)
    assert parsed.database == "facemark_test_suite_0123456789ab"
    assert dict(parsed.query) == {"sslmode": "prefer", "application_name": "fm test"}
    assert make_url(AWKWARD).database == "facemark"  # the input is not modified


def test_the_libpq_url_decodes_to_the_same_connection_parameters() -> None:
    url = suite_db.psycopg_url(suite_db.with_database(AWKWARD, "facemark_test_x_0123456789ab"))
    assert url.startswith("postgresql://")
    info = psycopg.conninfo.conninfo_to_dict(url)
    assert info["user"] == "us@er"
    assert info["password"] == "p@ss:w/ord%#?"
    assert (info["host"], info["port"]) == ("db.example", "5433")
    assert info["dbname"] == "facemark_test_x_0123456789ab"
    assert info["sslmode"] == "prefer"
    assert info["application_name"] == "fm test"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://u:p@h/db",
        "postgresql+psycopg://u@h:6543/db",
        "postgresql+psycopg://u:p@h/db?host=/var/run/postgresql",
        "postgresql+psycopg://u:p@[::1]:5432/db",
    ],
)
def test_other_url_shapes_round_trip(url: str) -> None:
    swapped = make_url(suite_db.with_database(url, "facemark_test_a_0123456789ab"))
    original = make_url(url)
    assert swapped.database == "facemark_test_a_0123456789ab"
    for attribute in ("username", "password", "host", "port", "query"):
        assert getattr(swapped, attribute) == getattr(original, attribute), attribute


def test_a_unix_socket_url_keeps_its_host_parameter_for_libpq() -> None:
    url = suite_db.psycopg_url(
        suite_db.with_database(
            "postgresql+psycopg://u:p@/db?host=/var/run/postgresql", "facemark_test_a_0123456789ab"
        )
    )
    info = psycopg.conninfo.conninfo_to_dict(url)
    assert info["host"] == "/var/run/postgresql"
    assert info["dbname"] == "facemark_test_a_0123456789ab"


def test_a_scratch_database_never_shows_its_credentials_in_repr_or_str() -> None:
    db = ScratchDatabase(
        name="facemark_test_x_0123456789ab",
        oid=7,
        url=suite_db.with_database(AWKWARD, "facemark_test_x_0123456789ab"),
        maintenance_url=AWKWARD,
    )
    for text in (repr(db), str(db), f"{db}"):
        assert "p%40ss" not in text and "p@ss" not in text and "db.example" not in text
    assert str(db) == "facemark_test_x_0123456789ab"


# --- the read-only inventory ------------------------------------------------


def test_the_inventory_lists_test_databases_without_changing_anything(
    made: list[ScratchDatabase],
) -> None:
    mine = suite_db.create("inv", maintenance_url=_maintenance())
    made.append(mine)
    with psycopg.connect(
        suite_db.psycopg_url(_maintenance()),
        autocommit=True,
        options="-c default_transaction_read_only=on",
    ) as conn:
        assert conn.execute("SHOW default_transaction_read_only").fetchone() == ("on",)
        found = {item.name: item for item in db_inventory.find_leftovers(conn, main_database="x")}
        assert mine.name in found
        assert not found[mine.name].legacy
        # the application's own database is never listed, whatever it is called
        hidden = db_inventory.find_leftovers(conn, main_database=mine.name)
        assert mine.name not in {item.name for item in hidden}
    assert _exists(mine.name)  # looking changed nothing


def test_the_inventory_only_selects_and_only_matches_test_names() -> None:
    assert db_inventory._INVENTORY_SQL.lstrip().upper().startswith("SELECT")
    for word in ("DROP", "DELETE", "ALTER", "CREATE", "TRUNCATE", "UPDATE", "INSERT"):
        assert word not in db_inventory._INVENTORY_SQL.upper()
    assert (
        "%(pattern)s" in db_inventory._INVENTORY_SQL
    )  # the shape comes from suite_db, bound as a parameter


def test_the_inventory_connects_read_only_and_prints_instructions(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    made: list[ScratchDatabase],
) -> None:
    mine = suite_db.create("inv2", maintenance_url=_maintenance())
    made.append(mine)
    seen: dict[str, Any] = {}
    real_connect: Callable[..., Any] = psycopg.connect

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(psycopg, "connect", spy)
    assert db_inventory.main() == 0
    out = capsys.readouterr().out

    assert "default_transaction_read_only=on" in seen["options"]
    assert mine.name in out
    assert "never does" in out and "by hand" in out
    assert "p@ss" not in out and os.environ["DATABASE_URL"] not in out  # no credentials


def test_the_rendered_instructions_offer_commands_only_for_idle_databases() -> None:
    idle = db_inventory.Leftover("facemark_test_a_0123456789ab", 8192, 0, False)
    busy = db_inventory.Leftover("facemark_test_b_ba9876543210", 8192, 3, False)
    old = db_inventory.Leftover("facemark_test_suite", 8192, 0, True)
    text = db_inventory.render([idle, busy, old])
    assert 'DROP DATABASE "facemark_test_a_0123456789ab";' in text
    assert 'DROP DATABASE "facemark_test_suite";' in text
    assert 'DROP DATABASE "facemark_test_b_ba9876543210";' not in text
    assert "STILL USING IT" in text
    assert "older fixed name" in text
    assert "No leftover" in db_inventory.render([])
