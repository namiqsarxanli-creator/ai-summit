"""Real-event bootstrap vs the demo seed.

A real deployment must be able to get its initial settings and first
administrator WITHOUT any demo data, the demo seed must be impossible
in production, and none of it may touch what is already in the
database (real attendees above all).

Everything here runs against throw-away databases — the suite database
or the isolated reset-test database — never the developer's real one.
The commands under test are driven through ``app.cli.main`` with
``SessionLocal`` pointed at the isolated database.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app import cli
from app.core.config import Settings
from app.core.security import verify_secret
from app.db.session import SessionLocal
from app.modules.staff.models import StaffRole
from app.seed import inventory, seeder
from app.seed.seeder import DemoSeedRefusedError, seed_all
from tests.prod_settings import VALID_PRODUCTION
from tests.reset_support import ALL_RESET_RELEVANT_TABLES

_ADMIN_EMAIL = "director@summit.example.org"
_ADMIN_PASSWORD = "correct-horse-battery-staple"  # noqa: S105 — test value
_CONTENT_TABLES = (
    "booths",
    "tasks",
    "booth_interests",
    "interests",
    "staff_accounts",
    "users",
    "user_interests",
    "contact_cards",
    "points_log",
    "feedback",
)


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "development",
        "database_url": "postgresql+psycopg://u:p@localhost:5432/unused",
        "app_origin": "https://summit.example.org",
        "scan_ref_secret": "test-scan-ref-secret",
        "demo_mode": False,
        "resend_api_key": "re_test_key",
    }
    values.update(overrides)
    if values["environment"] == "production":
        values = {**VALID_PRODUCTION, **overrides}
    return Settings(_env_file=None, **values)


@pytest.fixture
def blank_db(reset_db_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    """An EMPTY (no seed, no event_config) isolated database, with the
    CLI's session factory pointed at it."""
    with reset_db_engine.connect() as conn:
        tables = ", ".join((*ALL_RESET_RELEVANT_TABLES, "event_config"))
        conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
        conn.commit()
    monkeypatch.setattr(cli, "SessionLocal", sessionmaker(bind=reset_db_engine))
    session = Session(bind=reset_db_engine)
    try:
        yield session
    finally:
        session.close()


def _count(db: Session, table: str) -> int:
    db.rollback()  # see rows committed by other sessions
    return int(db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def _snapshot(db: Session, tables: tuple[str, ...] = _CONTENT_TABLES) -> dict[str, list[Any]]:
    db.rollback()
    return {
        t: [tuple(r) for r in db.execute(text(f"SELECT * FROM {t} ORDER BY 1, 2")).all()]
        for t in tables
    }


def _insert_real_data(db: Session) -> None:
    """What a live event database holds: a real attendee, a real booth
    with its own staff member, a real interest."""
    db.execute(
        text(
            "INSERT INTO users (first_name, last_name, email, phone, profession, "
            "sponsor_consent, status, activated_at) VALUES "
            "('Real', 'Attendee', 'real.person@example.org', '+994500000000', 'Engineer', "
            "false, 'ACTIVE', now())"
        )
    )
    db.execute(text("INSERT INTO interests (slug, name) VALUES ('real-topic', 'Real Topic')"))
    booth_id: Any = db.execute(
        text("INSERT INTO booths (slug, name) VALUES ('real-booth', 'Real Booth') RETURNING id")
    ).scalar_one()
    db.execute(
        text(
            "INSERT INTO staff_accounts (email, password_hash, full_name, role, booth_id) "
            "VALUES ('ops@example.org', 'existing-hash', 'Ops Person', 'BOOTH_STAFF', :b)"
        ),
        {"b": booth_id},
    )
    db.commit()


def _answer_prompts(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
    """Makes the CLI believe it has a terminal and feeds `getpass`."""
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    queue = list(answers)
    asked: list[str] = []

    def fake_getpass(prompt: str = "") -> str:
        asked.append(prompt)
        return queue.pop(0)

    monkeypatch.setattr("getpass.getpass", fake_getpass)
    return asked


def _forbid_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))

    def never(prompt: str = "") -> str:
        raise AssertionError("a password must not be asked here")

    monkeypatch.setattr("getpass.getpass", never)


def _run(argv: list[str]) -> None:
    cli.main(argv)


# --------------------------------------------------------------------------
# The demo seed is refused outside demo mode — before any database access
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"environment": "production", "demo_mode": False},
        {"environment": "development", "demo_mode": False},
    ],
    ids=["production", "demo-mode-off"],
)
def test_demo_seed_cli_is_refused_and_writes_nothing(
    blank_db: Session,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, Any],
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(**overrides))
    monkeypatch.setenv("SEED_ADMIN_PASSWORD", "x" * 14)
    monkeypatch.setenv("SEED_STAFF_PASSWORD", "y" * 14)

    with pytest.raises(SystemExit) as exc:
        _run(["seed-demo"])

    assert exc.value.code == 1
    assert "refusing to load demo data" in capsys.readouterr().err
    for table in (*_CONTENT_TABLES, "event_config", "task_preset_points"):
        assert _count(blank_db, table) == 0, table


def test_demo_seed_guard_runs_before_the_session_is_even_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_database(*args: object, **kwargs: object) -> None:
        raise AssertionError("the database was touched before the guard")

    monkeypatch.setattr(cli, "SessionLocal", no_database)
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(environment="production"))
    monkeypatch.setenv("SEED_ADMIN_PASSWORD", "x" * 14)
    monkeypatch.setenv("SEED_STAFF_PASSWORD", "y" * 14)

    with pytest.raises(SystemExit) as exc:
        _run(["seed-demo"])
    assert exc.value.code == 1


def test_seed_all_itself_refuses_in_production_before_any_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is inside `seed_all`, so no caller can route around the
    CLI. A session that explodes on any use proves it ran first."""

    class ExplodingSession:
        def __getattr__(self, name: str) -> None:
            raise AssertionError(f"session.{name} used before the guard")

    monkeypatch.setattr(seeder, "get_settings", lambda: _settings(environment="production"))
    with pytest.raises(DemoSeedRefusedError, match="production"):
        seed_all(ExplodingSession(), admin_password="a" * 14, staff_password="b" * 14)  # type: ignore[arg-type]


def test_the_old_seed_command_no_longer_loads_anything(
    blank_db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(["seed"])
    assert exc.value.code == 1
    assert "bootstrap" in capsys.readouterr().err
    assert _count(blank_db, "booths") == 0


# --------------------------------------------------------------------------
# Real bootstrap: settings only
# --------------------------------------------------------------------------


def test_bootstrap_creates_settings_and_no_demo_content(blank_db: Session) -> None:
    _run(["bootstrap"])

    for table in _CONTENT_TABLES:
        assert _count(blank_db, table) == 0, f"bootstrap created rows in {table}"
    assert _count(blank_db, "event_config") == 1
    presets = blank_db.execute(
        text("SELECT points FROM task_preset_points WHERE booth_id IS NULL ORDER BY points")
    ).all()
    assert [p[0] for p in presets] == [5, 10, 15]
    config = blank_db.execute(text("SELECT dataset_generation, maintenance_mode FROM event_config"))
    assert tuple(config.one()) == (1, False)


def test_bootstrap_needs_no_demo_mode_and_no_seed_passwords(
    blank_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SEED_STAFF_PASSWORD", raising=False)
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(environment="production"))

    _run(["bootstrap"])

    assert _count(blank_db, "event_config") == 1


def test_bootstrap_twice_duplicates_nothing_and_keeps_operator_changes(
    blank_db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(["bootstrap"])
    blank_db.execute(text("UPDATE event_config SET feedback_points = 9, leaderboard_top_n = 25"))
    blank_db.execute(text("UPDATE task_preset_points SET label = 'Renamed' WHERE points = 10"))
    blank_db.commit()
    before = _snapshot(blank_db, ("event_config", "task_preset_points"))
    capsys.readouterr()

    _run(["bootstrap"])

    assert _snapshot(blank_db, ("event_config", "task_preset_points")) == before
    assert _count(blank_db, "task_preset_points") == 3
    assert "already present" in capsys.readouterr().out


def test_bootstrap_and_create_admin_leave_existing_data_untouched(
    blank_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live-database case: a real attendee, booth and staff exist."""
    _insert_real_data(blank_db)
    before = _snapshot(blank_db)

    _run(["bootstrap"])
    _answer_prompts(monkeypatch, _ADMIN_PASSWORD, _ADMIN_PASSWORD)
    _run(["create-admin", "--email", _ADMIN_EMAIL, "--name", "Director"])
    _run(["bootstrap"])

    after = _snapshot(blank_db)
    # the attendee, booth, interest and booth-staff rows are byte-identical...
    for table in ("users", "booths", "interests", "contact_cards", "user_interests", "tasks"):
        assert after[table] == before[table], table
    # ...and the only new row anywhere is the administrator.
    assert set(before["staff_accounts"]) <= set(after["staff_accounts"])
    assert len(after["staff_accounts"]) == len(before["staff_accounts"]) + 1


# --------------------------------------------------------------------------
# create-admin
# --------------------------------------------------------------------------


def test_create_admin_stores_only_an_argon2id_hash_and_never_echoes_the_password(
    blank_db: Session,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(0)
    asked = _answer_prompts(monkeypatch, _ADMIN_PASSWORD, _ADMIN_PASSWORD)

    _run(["create-admin", "--email", "Director@Summit.Example.org ", "--name", "  Director  "])

    row: Any = blank_db.execute(
        text(
            "SELECT email::text, password_hash, full_name, role::text, booth_id, is_active "
            "FROM staff_accounts"
        )
    ).one()
    email, password_hash, full_name, role, booth_id, is_active = row
    assert (email, full_name, role, booth_id, is_active) == (
        _ADMIN_EMAIL,
        "Director",
        StaffRole.ADMIN.value,
        None,
        True,
    )
    assert password_hash.startswith("$argon2id$")
    assert _ADMIN_PASSWORD not in password_hash
    assert verify_secret(password_hash, _ADMIN_PASSWORD)
    assert not verify_secret(password_hash, _ADMIN_PASSWORD + "x")

    captured = capsys.readouterr()
    assert _ADMIN_PASSWORD not in captured.out + captured.err + caplog.text
    assert len(asked) == 2  # entered and confirmed through the hidden prompt


def test_create_admin_accepts_no_password_argument_or_environment_variable(
    blank_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    for flag in ("--password", "--pass"):
        with pytest.raises(SystemExit) as exc:
            _run(["create-admin", "--email", _ADMIN_EMAIL, "--name", "D", flag, _ADMIN_PASSWORD])
        assert exc.value.code == 2  # argparse: unrecognised argument

    monkeypatch.setenv("ADMIN_PASSWORD", _ADMIN_PASSWORD)
    monkeypatch.setenv("SEED_ADMIN_PASSWORD", _ADMIN_PASSWORD)
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(SystemExit) as exc:
        _run(["create-admin", "--email", _ADMIN_EMAIL, "--name", "D"])
    assert exc.value.code == 1  # no terminal -> refuses rather than reading env/stdin
    assert _count(blank_db, "staff_accounts") == 0


def test_create_admin_twice_does_not_duplicate_or_touch_the_password(
    blank_db: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _answer_prompts(monkeypatch, _ADMIN_PASSWORD, _ADMIN_PASSWORD)
    _run(["create-admin", "--email", _ADMIN_EMAIL, "--name", "Director"])
    original = _snapshot(blank_db, ("staff_accounts",))
    capsys.readouterr()

    _forbid_prompts(monkeypatch)  # an existing admin must not even be asked for a password
    _run(["create-admin", "--email", _ADMIN_EMAIL.upper(), "--name", "Somebody Else"])

    assert _snapshot(blank_db, ("staff_accounts",)) == original
    assert "already exists" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("first", "second", "why"),
    [
        ("long-enough-password", "different-password!", "mismatch"),
        ("short", "short", "too short"),
    ],
)
def test_create_admin_rejects_bad_passwords_without_writing(
    blank_db: Session,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    first: str,
    second: str,
    why: str,
) -> None:
    _answer_prompts(monkeypatch, first, second)

    with pytest.raises(SystemExit) as exc:
        _run(["create-admin", "--email", _ADMIN_EMAIL, "--name", "Director"])

    assert exc.value.code == 1, why
    assert _count(blank_db, "staff_accounts") == 0
    err = capsys.readouterr().err
    assert first not in err and second not in err


def test_create_admin_never_hijacks_an_existing_non_admin_account(
    blank_db: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _insert_real_data(blank_db)
    before = _snapshot(blank_db, ("staff_accounts",))
    _forbid_prompts(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        _run(["create-admin", "--email", "ops@example.org", "--name", "Ops Person"])

    assert exc.value.code == 1
    assert "BOOTH_STAFF" in capsys.readouterr().err
    assert _snapshot(blank_db, ("staff_accounts",)) == before


def test_create_admin_rejects_an_invalid_email(blank_db: Session) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(["create-admin", "--email", "not-an-email", "--name", "Director"])
    assert exc.value.code == 1
    assert _count(blank_db, "staff_accounts") == 0


# --------------------------------------------------------------------------
# demo-inventory: read-only
# --------------------------------------------------------------------------


def _seed_demo_into(db: Session) -> None:
    seed_all(db, admin_password="seed-admin-password", staff_password="seed-staff-password")


def test_inventory_finds_the_demo_records_and_changes_nothing(
    blank_db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_demo_into(blank_db)
    _insert_real_data(blank_db)
    blank_db.execute(text("UPDATE booths SET name = 'AIWorks Azerbaijan' WHERE slug = 'aiworks'"))
    blank_db.commit()
    before = _snapshot(blank_db, (*_CONTENT_TABLES, "event_config", "task_preset_points"))

    report = inventory.build_inventory(session_for(blank_db)())

    assert len(report.demo_staff) == 7
    assert len(report.demo_attendees) == 4
    assert {b.slug for b in report.demo_booths} == {
        "aiworks",
        "quantumleap-robotics",
        "securestack",
        "finpulse",
        "medisync-ai",
        "greengrid-systems",
    }
    edited = {b.slug: b.identical_to_fixture for b in report.demo_booths}
    assert edited["aiworks"] is False  # renamed since seeding: flagged, not assumed demo
    assert edited["finpulse"] is True
    # real data is reported as "everything else", never as demo
    assert report.other_attendee_count == 1
    assert report.other_booth_count == 1
    assert [s.email for s in report.other_staff] == ["ops@example.org"]

    assert _snapshot(blank_db, (*_CONTENT_TABLES, "event_config", "task_preset_points")) == before


def session_for(db: Session) -> Callable[[], Session]:
    return sessionmaker(bind=db.get_bind())


def test_inventory_cli_json_is_read_only_and_hides_real_attendees(
    blank_db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_demo_into(blank_db)
    _insert_real_data(blank_db)
    before = _snapshot(blank_db)

    _run(["demo-inventory", "--json"])

    out = capsys.readouterr().out
    data = json.loads(out)
    assert len(data["demo_booths"]) == 6
    assert "real.person@example.org" not in out  # real attendee data is never printed
    assert "Real" not in out.replace("Real Booth", "")
    assert _snapshot(blank_db) == before

    _run(["demo-inventory"])
    assert "nothing was changed" in capsys.readouterr().out
    assert _snapshot(blank_db) == before


def test_inventory_runs_in_a_read_only_transaction(
    blank_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the report code ever tried to write, PostgreSQL itself would
    refuse — proven by making it try."""
    _seed_demo_into(blank_db)

    def sneaky_write(db: Session) -> None:
        db.execute(text("UPDATE booths SET is_active = false"))

    monkeypatch.setattr(inventory, "_collect", sneaky_write)
    with pytest.raises(Exception, match="read-only transaction"):
        inventory.build_inventory(session_for(blank_db)())
    assert _count(blank_db, "booths") == 6
    assert blank_db.execute(text("SELECT count(*) FROM booths WHERE is_active")).scalar_one() == 6


def test_inventory_on_the_suite_database_runs_without_changes() -> None:
    """Same code path, ordinary suite database (seeded with the demo)."""
    db = SessionLocal()
    try:
        before = _snapshot(db)
        report = inventory.build_inventory(SessionLocal())
        assert len(report.demo_booths) == 6
        assert _snapshot(db) == before
    finally:
        db.close()
