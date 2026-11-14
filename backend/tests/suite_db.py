"""Throw-away PostgreSQL databases for the test suite - one per run, owned by it.

The suite must never touch the developer's (or the event's) real data, so it
builds its own databases on the same server: created, migrated and seeded for
a run, dropped by that run. An earlier version used ONE fixed name and
`DROP DATABASE ... WITH (FORCE)` on every start and end, so two runs - or a
run and a leftover process of an earlier one - could drop each other's
database in the middle of a test. The rules now are:

* **A unique name per database**: `facemark_test_<purpose>_<12 hex>`, from a
  CSPRNG (`secrets`). Purpose is a short fixed word (`suite`, `reset`, ...);
  the whole name is at most 43 characters, well inside PostgreSQL's 63-byte
  identifier limit.
* **A database is dropped only by the process that created it, and only
  that one.** `create()` records the name AND the database's `oid`; `drop()`
  refuses anything else - an unregistered name, a name whose `oid` changed
  (someone dropped and recreated it), the application's own database, a name
  outside the test pattern. Nothing ever lists databases and drops what
  matches a prefix.
* **Nothing is dropped at start-up.** A leftover from a process that was
  killed is left alone; `python -m tests.db_inventory` lists them read-only
  and prints the commands for a human to run (see README).
* **Failure cleans up after itself**: if migrating or seeding fails, the
  database that attempt created is dropped before the error propagates. An
  `atexit` hook drops whatever this process still owns if it exits without
  the normal teardown.
* **URLs are parsed and built with SQLAlchemy's `make_url`**, never by
  string surgery, so query parameters and credentials with special
  characters survive; credentials never appear in an error or a log line.

`prepare()`/`provision_suite()` run from `conftest.py` BEFORE any `app` import
(settings are cached at import), so this module deliberately imports nothing
from `app`.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

TEST_DB_PREFIX = "facemark_test_"
_PURPOSE_RE = re.compile(r"^[a-z][a-z0-9]{0,15}$")
# The ONLY shape of name this module will ever create or drop.
NAME_PATTERN = r"^facemark_test_[a-z][a-z0-9]{0,15}_[0-9a-f]{12}$"
_NAME_RE = re.compile(NAME_PATTERN)
_NEVER_DROP = frozenset({"postgres", "template0", "template1"})
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_SUFFIX_BYTES = 6  # 12 hex characters


class UnsafeDropError(RuntimeError):
    """A destructive database command was refused."""


class SetupFailed(RuntimeError):
    """Migrating or seeding a scratch database failed (message is redacted)."""


@dataclass(frozen=True)
class ScratchDatabase:
    """The exact identity of a database this process created."""

    name: str
    oid: int
    # SQLAlchemy-style URL of this database. Contains the password.
    url: str = field(repr=False, compare=False)
    # The database the CREATE/DROP statements were issued through.
    maintenance_url: str = field(repr=False, compare=False)
    # Names that must never be dropped, whatever else is true.
    protected: frozenset[str] = field(default=frozenset(), repr=False, compare=False)

    @property
    def psycopg_url(self) -> str:
        return psycopg_url(self.url)

    def __str__(self) -> str:
        return self.name


# name -> the database this process created and has not yet dropped.
_OWNED: dict[str, ScratchDatabase] = {}
# The application's own database, recorded when the suite is provisioned.
_MAIN_DATABASE: str | None = None


# --- URLs ---------------------------------------------------------------


def with_database(url: str, name: str) -> str:
    """`url` pointing at database `name`; every other part (credentials,
    host, port, query parameters) is carried over unchanged."""
    return make_url(url).set(database=name).render_as_string(hide_password=False)


def psycopg_url(url: str) -> str:
    """The libpq URI for a SQLAlchemy URL. Built from the parsed parts with
    strict percent-encoding: SQLAlchemy writes a space in a query value as
    `+`, which libpq does NOT decode, so re-rendering with SQLAlchemy would
    silently change such a value."""
    parsed = make_url(url)
    userinfo = ""
    if parsed.username is not None:
        userinfo = quote(parsed.username, safe="")
        if parsed.password is not None:
            userinfo += ":" + quote(parsed.password, safe="")
        userinfo += "@"
    host = parsed.host or ""
    if ":" in host:  # an IPv6 literal
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    database = "/" + quote(parsed.database, safe="") if parsed.database else ""
    pairs: list[str] = []
    for key in sorted(parsed.query):
        value = parsed.query[key]
        for item in (value,) if isinstance(value, str) else value:
            pairs.append(f"{quote(key, safe='')}={quote(item, safe='')}")
    query = "?" + "&".join(pairs) if pairs else ""
    return f"postgresql://{userinfo}{host}{port}{database}{query}"


def database_of(url: str) -> str | None:
    return make_url(url).database


def _redact(text: str, *urls: str) -> str:
    for url in urls:
        parsed = make_url(url)
        for secret in (url, psycopg_url(url), parsed.password):
            if secret:
                text = text.replace(secret, "***")
    return text


# --- names --------------------------------------------------------------


def new_name(purpose: str) -> str:
    if not _PURPOSE_RE.fullmatch(purpose):
        raise ValueError("purpose must be 1-16 lowercase letters/digits, starting with a letter")
    return f"{TEST_DB_PREFIX}{purpose}_{secrets.token_hex(_SUFFIX_BYTES)}"


def _protected(maintenance_url: str, main_database: str | None) -> frozenset[str]:
    names = set(_NEVER_DROP)
    for candidate in (database_of(maintenance_url), main_database, _MAIN_DATABASE):
        if candidate:
            names.add(candidate)
    return frozenset(names)


# --- create / drop ------------------------------------------------------


def _admin(url: str) -> psycopg.Connection[tuple[object, ...]]:
    # DDL such as CREATE/DROP DATABASE cannot run in a transaction block.
    return psycopg.connect(psycopg_url(url), autocommit=True)


def create(
    purpose: str, *, maintenance_url: str, main_database: str | None = None
) -> ScratchDatabase:
    """Creates a new, empty, uniquely named database and registers it as
    owned by this process. Never drops anything."""
    protected = _protected(maintenance_url, main_database)
    for _attempt in range(5):
        name = new_name(purpose)
        if name in protected:
            continue
        try:
            with _admin(maintenance_url) as conn:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                row = conn.execute(
                    "SELECT oid FROM pg_database WHERE datname = %s", (name,)
                ).fetchone()
        except psycopg.errors.DuplicateDatabase:
            continue  # a 48-bit collision: draw another name, touch nothing
        if row is None:  # pragma: no cover - CREATE just succeeded
            raise RuntimeError("created database not found in pg_database")
        scratch = ScratchDatabase(
            name=name,
            oid=int(row[0]),  # type: ignore[call-overload]
            url=with_database(maintenance_url, name),
            maintenance_url=maintenance_url,
            protected=protected,
        )
        _OWNED[name] = scratch
        return scratch
    raise RuntimeError("could not find an unused test database name")  # pragma: no cover


def assert_may_drop(db: ScratchDatabase) -> None:
    """Every check that must pass before a destructive command is issued."""
    if not _NAME_RE.fullmatch(db.name):
        raise UnsafeDropError("refusing to drop a database whose name is not a test name")
    if db.name in db.protected or db.name in _NEVER_DROP or db.name == _MAIN_DATABASE:
        raise UnsafeDropError("refusing to drop the application's or a system database")
    owned = _OWNED.get(db.name)
    if owned is None or owned != db:
        raise UnsafeDropError("refusing to drop a database this run did not create")


def drop(db: ScratchDatabase) -> None:
    """Drops exactly the database `db` identifies, if it is still the one this
    process created. A second call is refused (it is no longer registered), so
    a stale handle can never drop a database that took the name afterwards."""
    assert_may_drop(db)
    with _admin(db.maintenance_url) as conn:
        row = conn.execute("SELECT oid FROM pg_database WHERE datname = %s", (db.name,)).fetchone()
        if row is not None:
            if int(row[0]) != db.oid:  # type: ignore[call-overload]
                # Same name, different database: not ours any more.
                _OWNED.pop(db.name, None)
                raise UnsafeDropError(
                    "refusing to drop: the database was replaced since it was created"
                )
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db.name))
            )
    _OWNED.pop(db.name, None)


def _drop_quietly(db: ScratchDatabase) -> None:
    # cleanup must not mask the failure that led here
    with contextlib.suppress(Exception):
        drop(db)


@atexit.register
def _drop_leftovers_of_this_process() -> None:
    """Backstop for an exit that skipped the normal teardown (an error during
    collection, an interrupted run). Touches only what THIS process created."""
    for db in list(_OWNED.values()):
        _drop_quietly(db)


# --- migrate / seed -----------------------------------------------------


def _run_step(args: list[str], env: dict[str, str], *urls: str) -> None:
    cmd = [sys.executable, "-m", *args]
    try:
        subprocess.run(  # noqa: S603 - fixed argv
            cmd, cwd=str(_BACKEND_ROOT), env=env, check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError as err:
        tail = _redact((err.stderr or err.stdout or "")[-1500:], *urls)
        raise SetupFailed(f"`{' '.join(args)}` exited {err.returncode}:\n{tail}") from None


def prepare(
    purpose: str,
    *,
    maintenance_url: str,
    migrate_to: str | None = "head",
    seed: bool = False,
    main_database: str | None = None,
) -> ScratchDatabase:
    """create() + migrate (+ seed). If any step fails, the database THIS call
    created is dropped before the error is re-raised."""
    scratch = create(purpose, maintenance_url=maintenance_url, main_database=main_database)
    try:
        if migrate_to is not None:
            env = dict(os.environ, DATABASE_URL=scratch.url)
            _run_step(["alembic", "upgrade", migrate_to], env, scratch.url, maintenance_url)
        if seed:
            env = dict(os.environ, DATABASE_URL=scratch.url, DEMO_MODE="true", RESEND_API_KEY="")
            _run_step(["app.cli", "seed-demo"], env, scratch.url, maintenance_url)
    except BaseException:
        _drop_quietly(scratch)
        raise
    return scratch


def provision_suite() -> ScratchDatabase:
    """The suite's own database: points `DATABASE_URL` at it and returns its
    identity. The ORIGINAL database is used only to issue CREATE/DROP."""
    global _MAIN_DATABASE
    original = os.environ["DATABASE_URL"]
    _MAIN_DATABASE = database_of(original)
    scratch = prepare("suite", maintenance_url=original, seed=True, main_database=_MAIN_DATABASE)
    os.environ["DATABASE_URL"] = scratch.url
    return scratch


@contextmanager
def scratch_database(purpose: str, *, migrate_to: str | None = "head") -> Iterator[ScratchDatabase]:
    """A further throw-away database (destructive reset tests, migration
    tests), created, migrated and dropped by the caller's own scope."""
    scratch = prepare(
        purpose,
        maintenance_url=os.environ["DATABASE_URL"],
        migrate_to=migrate_to,
    )
    try:
        yield scratch
    finally:
        _drop_quietly(scratch)
