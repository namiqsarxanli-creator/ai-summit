"""CSV exports (app/modules/admin/exports.py): coverage, filter parity, the
sponsor rule, authority, escaping, the snapshot, streaming resources, audit.

Runs against the disposable reset-test database (a scratch database created by
this run only, see tests/suite_db.py).
"""

from __future__ import annotations

import csv
import gc
import io
import json
import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import ResourceClosedError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.ratelimit import RateLimit
from app.core.sessions import create_staff_session
from app.db.session import get_db
from app.main import app
from app.modules.admin import export_status, exports
from app.modules.admin.csv_export import encode_rows
from app.modules.admin.export_tickets import TTL_SECONDS, tickets
from app.modules.admin.schemas import AttendeeFilters, SponsorExportRequest
from app.modules.staff.models import StaffAccount, StaffRole
from tests.test_admin_lists import BASE, _insert_attendees, _insert_ledger, _walk

pytestmark = pytest.mark.usefixtures("clean_reset_db")

ATTENDEES = "/api/admin/attendees"
LEDGER = "/api/admin/points-log"
BOM_BYTES = b"\xef\xbb\xbf"


# --- fixtures and helpers -----------------------------------------------------


class Env:
    def __init__(self, client: TestClient, engine: Engine) -> None:
        self.client = client
        self.engine = engine
        self.sessions: list[Session] = []  # every session an export opened

    def factory(self) -> Session:
        session = sessionmaker(bind=self.engine, autoflush=False)()
        self.sessions.append(session)
        return session


@pytest.fixture
def env(reset_db_engine: Engine) -> Iterator[Env]:
    exports._active.clear()
    tickets.clear()
    export_status.store.clear()
    holder: dict[str, Env] = {}

    def isolated_db() -> Iterator[Session]:
        with Session(reset_db_engine) as db:
            yield db

    app.dependency_overrides[get_db] = isolated_db
    app.dependency_overrides[exports.export_session_factory] = lambda: holder["env"].factory
    try:
        with TestClient(app, headers={"Origin": get_settings().app_origin}) as client:
            holder["env"] = Env(client, reset_db_engine)
            _sign_in(client, reset_db_engine, StaffRole.ADMIN)
            yield holder["env"]
    finally:
        app.dependency_overrides.clear()
        exports._active.clear()
        tickets.clear()
        export_status.store.clear()


def _sign_in(client: TestClient, engine: Engine, role: StaffRole, nth: int = 0) -> int:
    with Session(engine) as db:
        rows = (
            db.execute(
                select(StaffAccount).where(StaffAccount.role == role).order_by(StaffAccount.id)
            )
            .scalars()
            .all()
        )
        staff = rows[nth]
        token = create_staff_session(db, staff_account_id=staff.id, now=datetime.now(UTC))
        staff_id = staff.id
        db.commit()
    client.cookies.clear()
    client.cookies.set("session", token)
    return staff_id


def _sql(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with Session(engine) as db:
        result = db.execute(text(sql), params)
        try:
            out = list(result.all())
        except ResourceClosedError:  # a statement that returns no rows
            out = []
        db.commit()
        return out


def _ids(engine: Engine, sql: str, **params: Any) -> list[int]:
    return [int(r[0]) for r in _sql(engine, sql, **params)]


def _prepare(env: Env, kind: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    response = env.client.post(f"/api/admin/exports/{kind}", json=body if body is not None else {})
    assert response.status_code == 200, response.text
    prepared: dict[str, Any] = response.json()
    return prepared


def _export(env: Env, kind: str, body: dict[str, Any] | None = None) -> Any:
    prepared = _prepare(env, kind, body)
    response = env.client.get(prepared["download_url"])
    assert response.status_code == 200, response.text
    return response


def _parse(response: Any) -> list[list[str]]:
    raw = response.content
    assert raw.startswith(BOM_BYTES)
    return list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))


def _audit(engine: Engine) -> list[dict[str, Any]]:
    rows = _sql(
        engine,
        "SELECT action, target_type, target_id, staff_account_id, before_value, after_value "
        "FROM admin_audit WHERE action LIKE 'export.%' ORDER BY id",
    )
    return [
        {
            "action": r[0],
            "target_type": r[1],
            "target_id": r[2],
            "actor": r[3],
            "before": r[4],
            "after": r[5],
        }
        for r in rows
    ]


def _idle_in_transaction(engine: Engine) -> int:
    return int(
        _sql(
            engine,
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND state LIKE 'idle in transaction%' AND pid <> pg_backend_pid()",
        )[0][0]
    )


def _insert_user(
    engine: Engine,
    first: str,
    last: str,
    email: str,
    *,
    phone: str = "+994501234567",
    profession: str = "Engineer",
    linkedin: str | None = None,
    status: str = "ACTIVE",
    consent: bool = False,
    created: datetime | None = None,
) -> int:
    return int(
        _sql(
            engine,
            "INSERT INTO users (first_name, last_name, email, phone, profession, linkedin_url, "
            "sponsor_consent, sponsor_consent_at, status, activated_at, created_at) "
            "VALUES (:f, :l, :e, :p, :pr, :li, :c, CASE WHEN :c THEN now() END, "
            "CAST(:s AS user_status), CASE WHEN :s = 'ACTIVE' THEN now() END, :at) RETURNING id",
            f=first,
            l=last,
            e=email,
            p=phone,
            pr=profession,
            li=linkedin,
            c=consent,
            s=status,
            at=created or datetime.now(UTC),
        )[0][0]
    )


# --- the whole result, not a page ----------------------------------------------


def test_the_attendee_export_contains_every_attendee_not_one_page(env: Env) -> None:
    _insert_attendees(env.engine)  # 620 rows: more than the 500 cap, more than two pages of 100
    expected = _ids(env.engine, "SELECT id FROM users ORDER BY created_at DESC, id DESC")
    assert len(expected) > 500

    response = _export(env, "attendees")
    rows = _parse(response)

    assert rows[0] == [
        "id",
        "first_name",
        "last_name",
        "email",
        "status",
        "sponsor_consent",
        "created_at",
    ]
    assert [int(r[0]) for r in rows[1:]] == expected  # all of them, in the screen's order
    assert response.headers["x-export-rows"] == str(len(expected))


def test_the_ledger_export_contains_every_row_not_one_page(env: Env) -> None:
    _insert_ledger(env.engine)
    expected = _ids(env.engine, "SELECT id FROM points_log ORDER BY created_at DESC, id DESC")
    assert len(expected) > 200

    rows = _parse(_export(env, "points-log"))

    assert rows[0][:3] == ["id", "created_at", "user_id"]
    assert [int(r[0]) for r in rows[1:]] == expected
    assert len({r[0] for r in rows[1:]}) == len(expected)  # none repeated


def test_an_empty_result_is_a_valid_file_with_only_the_header(env: Env) -> None:
    response = _export(env, "attendees", {"q": "nobody-has-this-name"})
    assert response.headers["x-export-rows"] == "0"
    assert _parse(response) == [
        ["id", "first_name", "last_name", "email", "status", "sponsor_consent", "created_at"]
    ]


# --- the screen and the file use one set of filters ------------------------------


def test_every_attendee_filter_gives_the_export_the_same_rows_as_the_list(env: Env) -> None:
    _insert_attendees(env.engine)
    edge = BASE + timedelta(seconds=40)
    cases: list[dict[str, Any]] = [
        {"q": "zorbak"},
        {"q": "ZORBAK corp.test"},
        {"q": "O_Brien"},
        {"q": "%"},
        {"status": "PROVISIONAL"},
        {"status": "ACTIVE", "sponsor_consent": True},
        {"sponsor_consent": False, "q": "person1"},
        {"registered_from": edge.isoformat()},
        {"registered_to": edge.isoformat()},
        {
            "registered_from": edge.isoformat(),
            "registered_to": (edge + timedelta(seconds=9)).isoformat(),
        },
        {
            "q": "surname",
            "status": "ACTIVE",
            "registered_to": (edge + timedelta(seconds=30)).isoformat(),
        },
    ]
    for body in cases:
        params = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in body.items()}
        on_screen = [row["id"] for row in _walk(env.client, ATTENDEES, params, limit=100)[0]]
        in_file = [int(r[0]) for r in _parse(_export(env, "attendees", body))[1:]]
        assert in_file == on_screen, body
    assert on_screen  # the last combination matched something


def test_every_ledger_filter_gives_the_export_the_same_rows_as_the_list(env: Env) -> None:
    seed = _insert_ledger(env.engine)
    booth_a, booth_b = seed["booths"]
    task_a = seed["tasks"][booth_a.booth_id]
    edge = BASE + timedelta(seconds=7)
    cases: list[dict[str, Any]] = [
        {"user_id": seed["users"][3]},
        {"booth_id": booth_b.booth_id},
        {"task_id": task_a},
        {"staff_id": booth_a.staff_id},
        {"source": "FEEDBACK"},
        {"entry_type": "REVERSAL"},
        {"booth_id": booth_a.booth_id, "source": "TASK", "entry_type": "AWARD"},
        {"created_from": edge.isoformat()},
        {"created_to": edge.isoformat()},
        {"created_from": edge.isoformat(), "created_to": (edge + timedelta(seconds=5)).isoformat()},
    ]
    for body in cases:
        on_screen = [row["id"] for row in _walk(env.client, LEDGER, dict(body), limit=100)[0]]
        in_file = [int(r[0]) for r in _parse(_export(env, "points-log", body))[1:]]
        assert in_file == on_screen, body
        assert in_file, body


def test_an_export_cannot_be_limited_by_a_cursor_or_a_page_size(env: Env) -> None:
    for kind in ("attendees", "points-log"):
        for extra in ({"limit": 10}, {"cursor": "abc"}, {"colour": "red"}):
            response = env.client.post(f"/api/admin/exports/{kind}", json=extra)
            assert response.status_code == 422, (kind, extra)
            assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    for body in (
        {"registered_from": "2026-09-01T00:00:00"},  # no UTC offset
        {"registered_from": "2026-09-05T00:00:00Z", "registered_to": "2026-09-01T00:00:00Z"},
        {"status": "DELETED"},
        {"q": "x" * 101},
    ):
        assert env.client.post("/api/admin/exports/attendees", json=body).status_code == 422
    assert env.client.post("/api/admin/exports/points-log", json={"user_id": 0}).status_code == 422


# --- the sponsor export ---------------------------------------------------------


def _sponsor_fixture(env: Env) -> dict[str, int]:
    return {
        "yes": _insert_user(
            env.engine,
            "Ada",
            "Consenting",
            "ada@sponsor.test",
            consent=True,
            linkedin="https://www.linkedin.com/in/ada",
            profession="Engineer",
        ),
        "no": _insert_user(env.engine, "Bob", "Private", "bob@sponsor.test", consent=False),
        "provisional": _insert_user(
            env.engine, "Cem", "Unverified", "cem@sponsor.test", status="PROVISIONAL", consent=True
        ),
        "yes2": _insert_user(env.engine, "Əli", "Şirvanlı", "ali@sponsor.test", consent=True),
    }


def test_the_sponsor_export_holds_only_active_consenting_attendees_and_only_six_columns(
    env: Env,
) -> None:
    ids = _sponsor_fixture(env)
    _insert_attendees(env.engine, 40)  # a mix of consent/status from the generator

    rows = _parse(_export(env, "sponsor-contacts"))

    assert rows[0] == ["first_name", "last_name", "profession", "email", "phone", "linkedin_url"]
    emails = {r[3] for r in rows[1:]}
    assert {"ada@sponsor.test", "ali@sponsor.test"} <= emails
    assert "bob@sponsor.test" not in emails  # no consent
    assert "cem@sponsor.test" not in emails  # consent given, but never verified
    expected = {
        r[0]
        for r in _sql(
            env.engine,
            "SELECT email::text FROM users WHERE status = 'ACTIVE' AND sponsor_consent IS TRUE",
        )
    }
    assert emails == expected
    assert all(len(r) == 6 for r in rows)
    assert ids["yes"]


def test_the_sponsor_export_leaks_no_token_or_internal_field(env: Env) -> None:
    _sponsor_fixture(env)
    uid = _ids(env.engine, "SELECT id FROM users WHERE email::text = 'ada@sponsor.test'")[0]
    _sql(
        env.engine,
        "INSERT INTO contact_cards (user_id, public_token, issued_at) "
        "VALUES (:u, 'PLANTED-CARD-TOKEN-XYZ', now())",
        u=uid,
    )
    body = _export(env, "sponsor-contacts").text
    for secret in ("PLANTED-CARD-TOKEN-XYZ", "public_token", "token", "password", "status", "id,"):
        assert secret not in body, secret
    assert "ada@sponsor.test" in body


@pytest.mark.parametrize(
    "body",
    [
        {"sponsor_consent": False},
        {"status": "PROVISIONAL"},
        {"include_unconsented": True},
        {"q": "bob"},
        {"limit": 5},
    ],
)
def test_the_client_cannot_switch_off_the_sponsor_conditions(
    env: Env, body: dict[str, Any]
) -> None:
    _sponsor_fixture(env)
    response = env.client.post("/api/admin/exports/sponsor-contacts", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_the_sponsor_export_takes_no_body_at_all(env: Env) -> None:
    _sponsor_fixture(env)
    response = env.client.post("/api/admin/exports/sponsor-contacts")  # no JSON at all
    assert response.status_code == 200
    rows = _parse(env.client.get(response.json()["download_url"]))
    assert "bob@sponsor.test" not in {r[3] for r in rows[1:]}


def test_sponsor_consent_is_judged_at_the_snapshot_instant(env: Env) -> None:
    ids = _sponsor_fixture(env)
    stream = exports.start_export(
        env.factory,
        actor_id=_sign_in(env.client, env.engine, StaffRole.ADMIN),
        kind="sponsor-contacts",
        filters=SponsorExportRequest(),
        limit=exports.ExportLimits(max_rows=1000, max_seconds=60, batch_rows=1),
    )
    first = next(stream.chunks())
    # after the snapshot: Ada withdraws, Bob consents, a new consenting attendee registers
    _sql(
        env.engine,
        "UPDATE users SET sponsor_consent = false, sponsor_consent_at = NULL WHERE id = :i",
        i=ids["yes"],
    )
    _sql(
        env.engine,
        "UPDATE users SET sponsor_consent = true, sponsor_consent_at = now() WHERE id = :i",
        i=ids["no"],
    )
    _insert_user(env.engine, "Late", "Registrant", "late@sponsor.test", consent=True)

    body = first + b"".join(stream.rest)
    emails = {r[3] for r in list(csv.reader(io.StringIO(body.decode("utf-8-sig"), newline="")))[1:]}

    assert "ada@sponsor.test" in emails  # consented AT the snapshot, withdrew after
    assert "bob@sponsor.test" not in emails  # consented only after it
    assert "late@sponsor.test" not in emails
    # a new export reflects the new state
    later = {r[3] for r in _parse(_export(env, "sponsor-contacts"))[1:]}
    assert "ada@sponsor.test" not in later and "bob@sponsor.test" in later


# --- authority and the ticket ----------------------------------------------------


@pytest.mark.parametrize("kind", ["attendees", "points-log", "sponsor-contacts"])
def test_only_an_administrator_can_prepare_or_download(env: Env, kind: str) -> None:
    prepared = _prepare(env, kind)  # a real ticket exists ...
    env.client.cookies.clear()
    anonymous = env.client.post(f"/api/admin/exports/{kind}", json={})
    assert anonymous.status_code == 401
    assert (
        env.client.get(prepared["download_url"]).status_code == 401
    )  # ... but nobody may redeem it

    _sign_in(env.client, env.engine, StaffRole.BOOTH_STAFF)
    assert env.client.post(f"/api/admin/exports/{kind}", json={}).status_code == 403
    staff_try = env.client.get(prepared["download_url"])
    assert staff_try.status_code == 403
    assert not staff_try.content.startswith(BOM_BYTES)
    assert _audit(env.engine) == []  # nothing started, nothing audited


def test_authority_is_refused_before_any_streaming_or_audit(env: Env) -> None:
    prepared = _prepare(env, "attendees")
    env.client.cookies.clear()
    response = env.client.get(prepared["download_url"])
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert "content-disposition" not in response.headers
    assert _audit(env.engine) == []
    # the refused attempt did not spend the ticket
    _sign_in(env.client, env.engine, StaffRole.ADMIN)
    assert env.client.get(prepared["download_url"]).status_code == 200


def test_a_ticket_is_single_use_short_lived_and_bound_to_its_administrator(env: Env) -> None:
    _sql(
        env.engine,
        "INSERT INTO staff_accounts (email, password_hash, full_name, role) "
        "VALUES ('second-admin@test.example', 'x', 'Second Admin', 'ADMIN')",
    )
    first_admin = _sign_in(env.client, env.engine, StaffRole.ADMIN, nth=0)
    prepared = _prepare(env, "attendees")

    _sign_in(env.client, env.engine, StaffRole.ADMIN, nth=1)  # another admin finds the link
    other = env.client.get(prepared["download_url"])
    assert other.status_code == 410
    assert other.json()["error"]["code"] == "EXPORT_TICKET_INVALID"

    _sign_in(env.client, env.engine, StaffRole.ADMIN, nth=0)
    assert env.client.get(prepared["download_url"]).status_code == 200
    again = env.client.get(prepared["download_url"])  # replay / double click
    assert again.status_code == 410
    assert first_admin
    assert len([a for a in _audit(env.engine) if a["action"] == "export.started"]) == 1

    # expiry
    stale = _prepare(env, "attendees")
    real_clock = tickets._clock
    tickets._clock = lambda: real_clock() + TTL_SECONDS + 1
    try:
        assert env.client.get(stale["download_url"]).status_code == 410
    finally:
        tickets._clock = real_clock


@pytest.mark.parametrize("token", ["x" * 30, "short", "a" * 300])
def test_garbage_tickets_are_refused(env: Env, token: str) -> None:
    response = env.client.get("/api/admin/exports/download", params={"ticket": token})
    assert response.status_code in (410, 422)


def test_a_second_download_while_one_runs_is_refused_without_spending_its_ticket(env: Env) -> None:
    _insert_attendees(env.engine, 60)
    actor = _sign_in(env.client, env.engine, StaffRole.ADMIN)
    running = exports.start_export(
        env.factory,
        actor_id=actor,
        kind="attendees",
        filters=AttendeeFilters(),
        limit=exports.ExportLimits(max_rows=1000, max_seconds=60, batch_rows=5),
    )
    prepared = _prepare(env, "attendees")
    busy = env.client.get(prepared["download_url"])
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "EXPORT_BUSY"

    running.run.close_resources()  # the first export ends ...
    assert (
        env.client.get(prepared["download_url"]).status_code == 200
    )  # ... the same link now works


def test_preparing_exports_is_rate_limited(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exports, "EXPORT_PER_ACCOUNT", RateLimit("admin:export:test", 2, 60))
    assert env.client.post("/api/admin/exports/attendees", json={}).status_code == 200
    assert env.client.post("/api/admin/exports/attendees", json={}).status_code == 200
    assert env.client.post("/api/admin/exports/attendees", json={}).status_code == 429


# --- response headers -------------------------------------------------------------


def test_the_download_is_never_cached_and_has_a_fixed_filename_without_personal_data(
    env: Env,
) -> None:
    _insert_attendees(env.engine, 30)
    response = _export(env, "attendees", {"q": "Zorbak"})
    headers = response.headers

    assert headers["cache-control"] == "no-store"
    assert headers["content-type"] == "text/csv; charset=utf-8"
    assert headers["x-content-type-options"] == "nosniff"
    assert re.fullmatch(
        r'attachment; filename="facemark-attendees-\d{8}-\d{6}Z\.csv"',
        headers["content-disposition"],
    )
    assert "zorbak" not in headers["content-disposition"].lower()
    snapshot = datetime.fromisoformat(headers["x-export-snapshot-at"].replace("Z", "+00:00"))
    assert snapshot.tzinfo is not None
    assert abs((snapshot - datetime.now(UTC)).total_seconds()) < 120
    # the three exports have three different fixed names
    names = {
        kind: re.search(r'filename="(.+?)"', _export(env, kind).headers["content-disposition"])
        for kind in ("points-log", "sponsor-contacts")
    }
    assert all(m and m.group(1).startswith("facemark-") for m in names.values())


def test_preparing_returns_json_that_is_not_cached_and_carries_no_filter_text(env: Env) -> None:
    response = env.client.post("/api/admin/exports/attendees", json={"q": "Secret Name"})
    assert response.headers["cache-control"] == "no-store"
    assert "Secret" not in response.text
    assert "q=" not in response.json()["download_url"]
    assert set(response.json()) == {
        "download_url",
        "ref",
        "status_url",
        "kind",
        "rows",
        "max_rows",
        "expires_in_seconds",
    }


# --- CSV content ------------------------------------------------------------------

HOSTILE = [
    '=HYPERLINK("http://evil.test","x")',
    "+1+2",
    "-2+3",
    "@SUM(A1)",
    "\t=1+1",
    "  =1+1",
    " =1+1",
    "​+x",
    "﻿@x",
    "\n=1",
]


def test_hostile_user_text_is_neutralised_and_everything_else_is_untouched(env: Env) -> None:
    created = datetime.now(UTC)
    for n, nasty in enumerate(HOSTILE):
        _insert_user(
            env.engine,
            nasty,
            f"Last{n}",
            f"hostile{n}@x.test",
            created=created - timedelta(seconds=n),
        )
    tricky = _insert_user(
        env.engine,
        'Əli, "Məmməd"\nİsmayıl',
        "Şirvanlı-Ğ",
        "unicode@x.test",
        created=created + timedelta(seconds=5),
    )

    rows = _parse(_export(env, "attendees", {"q": "x.test"}))[1:]
    by_email = {r[3]: r for r in rows}

    for n, nasty in enumerate(HOSTILE):
        first_name = by_email[f"hostile{n}@x.test"][1]
        assert first_name == "'" + nasty, repr(nasty)
    unicode_row = by_email["unicode@x.test"]
    assert unicode_row[0] == str(tricky)
    assert unicode_row[1] == 'Əli, "Məmməd"\nİsmayıl'  # commas, quotes, newline: one cell
    assert unicode_row[2] == "Şirvanlı-Ğ"


def test_server_numbers_in_the_ledger_stay_numbers_even_when_negative(env: Env) -> None:
    _insert_ledger(env.engine)
    rows = _parse(_export(env, "points-log"))
    header, data = rows[0], rows[1:]
    points = header.index("points")
    reversals = [r for r in data if r[header.index("entry_type")] == "REVERSAL"]
    assert reversals
    for row in reversals:
        assert re.fullmatch(r"-\d+", row[points]), row[points]  # "-6", never "'-6"
    for row in data:
        assert not any(cell.startswith("'") for cell in row), row


def test_activity_and_attendee_names_in_the_ledger_are_neutralised(env: Env) -> None:
    seed = _insert_ledger(env.engine)
    task = seed["tasks"][seed["booths"][0].booth_id]
    _sql(env.engine, "UPDATE tasks SET title = '=1+1 evil activity' WHERE id = :t", t=task)
    _sql(env.engine, "UPDATE users SET first_name = '@evil' WHERE id = :u", u=seed["users"][3])

    rows = _parse(_export(env, "points-log"))
    header, data = rows[0], rows[1:]
    activities = {r[header.index("activity")] for r in data}
    assert "'=1+1 evil activity" in activities
    assert "=1+1 evil activity" not in activities
    names = {r[header.index("attendee_name")] for r in data}
    assert any(n.startswith("'@evil") for n in names)


def test_sponsor_phone_numbers_survive_and_hostile_contact_fields_are_neutralised(env: Env) -> None:
    _insert_user(
        env.engine, "Pat", "Phone", "pat@sponsor.test", consent=True, phone="+994 50 123 45 67"
    )
    _insert_user(
        env.engine,
        "Eve",
        "Evil",
        "+eve@sponsor.test",
        consent=True,
        phone="+1+cmd|' /C calc'!A0",
        profession="=1+1",
        linkedin="@evil",
    )
    rows = {r[3]: r for r in _parse(_export(env, "sponsor-contacts"))[1:]}

    assert rows["pat@sponsor.test"][4] == "+994 50 123 45 67"
    eve = rows["'+eve@sponsor.test"]
    assert eve[4] == "'+1+cmd|' /C calc'!A0"
    assert eve[2] == "'=1+1"
    assert eve[5] == "'@evil"


# --- the snapshot ------------------------------------------------------------------


def test_changes_during_the_stream_neither_duplicate_nor_drop_rows(env: Env) -> None:
    _insert_attendees(env.engine, 200)
    actor = _sign_in(env.client, env.engine, StaffRole.ADMIN)
    expected = _ids(env.engine, "SELECT id FROM users ORDER BY created_at DESC, id DESC")
    stream = exports.start_export(
        env.factory,
        actor_id=actor,
        kind="attendees",
        filters=AttendeeFilters(),
        limit=exports.ExportLimits(max_rows=10_000, max_seconds=60, batch_rows=7),
    )
    chunks = [stream.first]
    chunks.append(next(stream.rest))

    # a registration at the head, one in the middle of the order, a rename, a delete
    _insert_user(
        env.engine, "Head", "Newest", "head@x.test", created=datetime.now(UTC) + timedelta(days=1)
    )
    _insert_user(env.engine, "Mid", "Inserted", "mid@x.test", created=BASE + timedelta(seconds=30))
    _sql(env.engine, "UPDATE users SET first_name = 'RENAMED' WHERE id = :i", i=expected[50])
    victim = expected[120]
    _sql(env.engine, "DELETE FROM users WHERE id = :i", i=victim)

    chunks.extend(stream.rest)
    rows = list(csv.reader(io.StringIO(b"".join(chunks).decode("utf-8-sig"), newline="")))[1:]

    assert [int(r[0]) for r in rows] == expected  # exactly the state at the snapshot
    assert victim in [int(r[0]) for r in rows]  # still there: it was there at the snapshot
    assert "RENAMED" not in {r[1] for r in rows}
    assert len({r[0] for r in rows}) == len(rows)
    assert stream.run.rows == stream.run.total == len(expected)


def test_the_export_runs_in_one_read_only_repeatable_read_transaction(env: Env) -> None:
    actor = _sign_in(env.client, env.engine, StaffRole.ADMIN)
    stream = exports.start_export(
        env.factory,
        actor_id=actor,
        kind="attendees",
        filters=AttendeeFilters(),
        limit=exports.ExportLimits(max_rows=1000, max_seconds=60, batch_rows=5),
    )
    session = stream.run.session
    assert session is not None
    assert session.execute(text("SHOW transaction_isolation")).scalar_one() == "repeatable read"
    assert session.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
    with pytest.raises(Exception, match="read-only"):
        session.execute(text("UPDATE users SET first_name = first_name"))
    session.rollback()
    stream.run.close_resources()
    # the pooled connection is not left in that mode
    with Session(env.engine) as other:
        assert other.execute(text("SHOW transaction_read_only")).scalar_one() == "off"


# --- limits and failure ---------------------------------------------------------------


def test_an_export_over_the_row_limit_is_refused_before_it_starts(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_attendees(env.engine, 150)
    monkeypatch.setattr(
        exports, "limits", lambda: exports.ExportLimits(max_rows=100, max_seconds=60)
    )

    refused = env.client.post("/api/admin/exports/attendees", json={})
    assert refused.status_code == 413
    error = refused.json()["error"]
    assert error["code"] == "EXPORT_TOO_LARGE"
    assert error["details"]["max_rows"] == 100 and error["details"]["rows"] > 100

    # narrower filters fit
    narrow = env.client.post("/api/admin/exports/attendees", json={"q": "Zorbak"})
    assert narrow.status_code == 200


def test_a_set_that_grew_past_the_limit_after_preparing_is_refused_and_audited(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_attendees(env.engine, 60)
    prepared = _prepare(env, "attendees")
    monkeypatch.setattr(
        exports, "limits", lambda: exports.ExportLimits(max_rows=20, max_seconds=60)
    )

    response = env.client.get(prepared["download_url"])

    assert response.status_code == 413
    assert not response.content.startswith(BOM_BYTES)
    actions = [a["action"] for a in _audit(env.engine)]
    assert actions == ["export.rejected"]
    assert exports.active_exports() == 0
    assert _idle_in_transaction(env.engine) == 0


def test_a_failure_after_the_response_started_aborts_the_stream_instead_of_ending_it(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_attendees(env.engine, 80)
    monkeypatch.setattr(
        exports,
        "limits",
        lambda: exports.ExportLimits(max_rows=1000, max_seconds=60, batch_rows=10),
    )
    calls = {"n": 0}
    real = encode_rows

    def flaky(rows: Any, kinds: Any) -> bytes:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("disk on fire: secret detail")
        return real(rows, kinds)

    monkeypatch.setattr("app.modules.admin.exports.encode_rows", flaky)
    prepared = _prepare(env, "attendees")

    with pytest.raises(RuntimeError, match="disk on fire"):
        env.client.get(
            prepared["download_url"]
        )  # the transfer errors out: it is NOT a complete 200

    audit = _audit(env.engine)
    assert [a["action"] for a in audit] == ["export.started", "export.finished"]
    finished = audit[1]["after"]
    assert finished["outcome"] == "failed"
    assert finished["error_class"] == "RuntimeError"
    assert finished["rows_written"] < finished["rows_expected"]
    assert "secret detail" not in json.dumps(audit)  # the error text is never recorded
    assert exports.active_exports() == 0
    assert _idle_in_transaction(env.engine) == 0


def test_running_past_the_time_limit_aborts_the_stream(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_attendees(env.engine, 80)
    monkeypatch.setattr(
        exports,
        "limits",
        lambda: exports.ExportLimits(max_rows=1000, max_seconds=60, batch_rows=10),
    )
    real = exports.ExportRun._check_budget
    calls = {"n": 0}

    def tick(self: exports.ExportRun) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            self._started -= 10_000  # as if the export had been running for hours
        real(self)

    monkeypatch.setattr(exports.ExportRun, "_check_budget", tick)
    prepared = _prepare(env, "attendees")
    with pytest.raises(exports.ExportTimeLimit):
        env.client.get(prepared["download_url"])

    finished = _audit(env.engine)[-1]["after"]
    assert finished["outcome"] == "failed" and finished["reason"] == "time_limit"
    assert exports.active_exports() == 0


def test_a_failure_before_the_first_byte_is_an_ordinary_error_response(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_attendees(env.engine, 20)
    prepared = _prepare(env, "attendees")
    monkeypatch.setattr(
        exports.ExportRun,
        "_check_budget",
        lambda self: (_ for _ in ()).throw(exports.ExportTimeLimit("slow")),
    )

    response = env.client.get(prepared["download_url"])

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "EXPORT_FAILED"
    assert not response.content.startswith(BOM_BYTES)
    assert "content-disposition" not in response.headers
    assert exports.active_exports() == 0
    assert _idle_in_transaction(env.engine) == 0


def test_a_database_failure_while_opening_is_a_server_error_and_leaks_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(env, "attendees")

    def boom(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("database exploded")

    monkeypatch.setattr(exports, "_count", boom)
    with pytest.raises(RuntimeError):
        env.client.get(prepared["download_url"])
    assert exports.active_exports() == 0
    assert _idle_in_transaction(env.engine) == 0
    assert all(not s.in_transaction() for s in env.sessions)


# --- streaming resources ---------------------------------------------------------------


def test_a_large_result_is_streamed_in_batches_not_loaded_whole(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_attendees(env.engine, 400)
    actor = _sign_in(env.client, env.engine, StaffRole.ADMIN)
    sizes: list[int] = []

    def spy(rows: Any, kinds: Any) -> bytes:
        batch = list(rows)
        sizes.append(len(batch))
        return encode_rows(batch, kinds)

    monkeypatch.setattr("app.modules.admin.exports.encode_rows", spy)
    stream = exports.start_export(
        env.factory,
        actor_id=actor,
        kind="attendees",
        filters=AttendeeFilters(),
        limit=exports.ExportLimits(max_rows=10_000, max_seconds=60, batch_rows=25),
    )
    chunks = list(stream.chunks())

    total = stream.run.total
    assert total >= 400
    assert max(sizes) <= 25  # rows were read and encoded 25 at a time ...
    assert len(sizes) == -(-total // 25)  # ... in as many batches as that takes
    assert len(chunks) == len(sizes)  # and each batch went out as its own chunk
    assert sum(len(c) for c in chunks) == stream.run.bytes
    assert max(len(c) for c in chunks) < stream.run.bytes / 10  # no chunk holds the file


def test_resources_are_released_when_the_stream_finishes(env: Env) -> None:
    _insert_attendees(env.engine, 50)
    _export(env, "attendees")
    assert exports.active_exports() == 0
    assert _idle_in_transaction(env.engine) == 0
    assert env.sessions and all(not s.in_transaction() for s in env.sessions)
    audit = _audit(env.engine)
    assert [a["action"] for a in audit] == ["export.started", "export.finished"]
    assert audit[0]["target_id"] == audit[1]["target_id"]
    assert audit[1]["after"]["outcome"] == "stream_finished"


def test_closing_a_half_read_stream_releases_everything_and_is_recorded_as_interrupted(
    env: Env,
) -> None:
    _insert_attendees(env.engine, 100)
    actor = _sign_in(env.client, env.engine, StaffRole.ADMIN)
    stream = exports.start_export(
        env.factory,
        actor_id=actor,
        kind="attendees",
        filters=AttendeeFilters(),
        limit=exports.ExportLimits(max_rows=1000, max_seconds=60, batch_rows=10),
    )
    chunks = stream.chunks()
    next(chunks)
    next(chunks)
    assert exports.active_exports() == 1  # the slot and the snapshot are held while it streams
    assert _idle_in_transaction(env.engine) == 1

    chunks.close()  # the client went away

    assert exports.active_exports() == 0
    assert _idle_in_transaction(env.engine) == 0
    finished = _audit(env.engine)[-1]["after"]
    assert finished["outcome"] == "interrupted"
    assert finished["rows_written"] < finished["rows_expected"]


def test_a_stream_that_is_simply_dropped_is_cleaned_up(env: Env) -> None:
    _insert_attendees(env.engine, 100)
    actor = _sign_in(env.client, env.engine, StaffRole.ADMIN)
    stream = exports.start_export(
        env.factory,
        actor_id=actor,
        kind="attendees",
        filters=AttendeeFilters(),
        limit=exports.ExportLimits(max_rows=1000, max_seconds=60, batch_rows=10),
    )
    del stream
    gc.collect()

    assert exports.active_exports() == 0
    assert _idle_in_transaction(env.engine) == 0
    assert _audit(env.engine)[-1]["after"]["outcome"] == "interrupted"


def test_a_client_disconnecting_mid_download_releases_everything(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drives the real ASGI app by hand: the client reads the first chunk and then
    hangs up."""
    _insert_attendees(env.engine, 400)
    monkeypatch.setattr(
        exports,
        "limits",
        lambda: exports.ExportLimits(max_rows=10_000, max_seconds=60, batch_rows=20),
    )
    prepared = _prepare(env, "attendees")
    token = env.client.cookies.get("session")
    url, _, query = prepared["download_url"].partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": url,
        "raw_path": url.encode(),
        "query_string": query.encode(),
        "headers": [(b"host", b"testserver"), (b"cookie", f"session={token}".encode())],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }
    seen: list[dict[str, Any]] = []

    async def drive() -> None:
        async def receive() -> dict[str, Any]:
            await anyio.sleep_forever()
            raise AssertionError("unreachable")

        async def send(message: dict[str, Any]) -> None:
            seen.append(message)
            bodies = [m for m in seen if m["type"] == "http.response.body" and m.get("body")]
            if len(bodies) >= 2:
                # what the server does when the peer has gone away
                raise OSError("client disconnected")

        try:
            await app(scope, receive, send)  # type: ignore[arg-type]
        except Exception as err:  # noqa: BLE001 - Starlette reports the hang-up as an error
            seen.append({"type": "app.error", "error": type(err).__name__})

    anyio.run(drive)
    gc.collect()
    deadline = time.monotonic() + 5
    while exports.active_exports() and time.monotonic() < deadline:
        time.sleep(0.05)

    assert any(m["type"] == "http.response.start" and m["status"] == 200 for m in seen)
    bodies = [m for m in seen if m["type"] == "http.response.body" and m.get("body")]
    assert len(bodies) == 2  # the client hung up after two of ~20 chunks
    assert exports.active_exports() == 0
    assert _idle_in_transaction(env.engine) == 0
    assert _audit(env.engine)[-1]["after"]["outcome"] == "interrupted"


def test_a_stalled_client_is_cut_off_by_the_watchdog(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_attendees(env.engine, 100)
    actor = _sign_in(env.client, env.engine, StaffRole.ADMIN)
    monkeypatch.setattr(exports, "WATCHDOG_GRACE_SECONDS", 0.3)
    stream = exports.start_export(
        env.factory,
        actor_id=actor,
        kind="attendees",
        filters=AttendeeFilters(),
        limit=exports.ExportLimits(max_rows=1000, max_seconds=0.3, batch_rows=10),
    )
    # the client takes the first chunk and then neither reads nor hangs up
    deadline = time.monotonic() + 8
    while exports.active_exports() and time.monotonic() < deadline:
        time.sleep(0.1)

    assert exports.active_exports() == 0
    assert _idle_in_transaction(env.engine) == 0
    finished = _audit(env.engine)[-1]["after"]
    assert finished["outcome"] == "failed" and finished["reason"] == "stalled"
    del stream


# --- the audit trail ---------------------------------------------------------------------


def test_the_audit_records_who_what_when_and_how_many_but_no_search_text_or_personal_data(
    env: Env,
) -> None:
    _insert_attendees(env.engine, 60)
    _insert_user(env.engine, "Zorbak", "Secretson", "secretson@private.test", consent=True)
    actor = _sign_in(env.client, env.engine, StaffRole.ADMIN)

    _export(env, "attendees", {"q": "Secretson", "status": "ACTIVE"})
    _export(env, "sponsor-contacts")
    _export(env, "points-log")

    audit = _audit(env.engine)
    assert [a["action"] for a in audit] == ["export.started", "export.finished"] * 3
    assert {a["actor"] for a in audit} == {actor}
    assert {a["target_type"] for a in audit} == {"export"}
    started, finished = audit[0]["after"], audit[1]["after"]
    assert started["kind"] == "attendees"
    assert started["filters_used"] == ["q", "status"]  # names only
    assert started["rows_expected"] == 1
    assert datetime.fromisoformat(started["snapshot_at"].replace("Z", "+00:00")).tzinfo
    assert finished["rows_written"] == 1 and finished["outcome"] == "stream_finished"
    assert finished["bytes_written"] > 0 and finished["elapsed_ms"] >= 0
    assert audit[2]["after"]["kind"] == "sponsor-contacts"
    assert audit[2]["after"]["filters_used"] == []

    blob = json.dumps(audit, default=str)
    for private in ("Secretson", "secretson", "private.test", "Zorbak", "+994", "token"):
        assert private not in blob, private
    assert all(a["before"] is None for a in audit)
    assert set(finished) == {
        "kind",
        "outcome",
        "reason",
        "error_class",
        "rows_written",
        "rows_expected",
        "bytes_written",
        "elapsed_ms",
    }


def test_preparing_an_export_alone_is_not_audited_because_nothing_left_the_server(env: Env) -> None:
    _prepare(env, "attendees")
    _prepare(env, "sponsor-contacts")
    assert _audit(env.engine) == []


def test_the_outcome_names_never_claim_that_the_user_has_the_file(env: Env) -> None:
    allowed = {"stream_finished", "interrupted", "failed"}
    _insert_attendees(env.engine, 20)
    _export(env, "attendees")
    outcomes = {
        a["after"]["outcome"] for a in _audit(env.engine) if a["action"] == "export.finished"
    }
    assert outcomes <= allowed
    assert not ({"downloaded", "saved", "completed", "success"} & outcomes)


# --- opening exports leaves the lists alone --------------------------------------------------


def test_exporting_changes_no_data(env: Env) -> None:
    _insert_ledger(env.engine)
    snapshot = (
        "SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM points_log), "
        "(SELECT coalesce(sum(points), 0) FROM points_log)"
    )
    before = tuple(_sql(env.engine, snapshot)[0])
    for kind in ("attendees", "points-log", "sponsor-contacts"):
        _export(env, kind)
    assert tuple(_sql(env.engine, snapshot)[0]) == before


def test_the_export_routes_are_hidden_from_the_production_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as app_main

    production = get_settings().model_copy(update={"environment": "production", "demo_mode": False})
    monkeypatch.setattr(app_main, "get_settings", lambda: production)
    paths = app_main.create_app().openapi()["paths"]
    assert not any("/exports/" in path for path in paths)


Factory = Callable[[], Session]


# --- what became of a download: the status the screen reads ------------------------


def _status(env: Env, prepared: dict[str, Any]) -> dict[str, Any]:
    response = env.client.get(prepared["status_url"])
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _token(prepared: dict[str, Any]) -> str:
    return str(prepared["download_url"]).partition("ticket=")[2]


def _staff(env: Env, nth: int = 0) -> StaffAccount:
    with Session(env.engine) as db:
        return (
            db.execute(
                select(StaffAccount)
                .where(StaffAccount.role == StaffRole.ADMIN)
                .order_by(StaffAccount.id)
            )
            .scalars()
            .all()[nth]
        )


def test_the_prepare_answer_points_at_a_status_that_is_not_the_ticket(env: Env) -> None:
    prepared = _prepare(env, "attendees")
    assert set(prepared) == {
        "download_url",
        "ref",
        "status_url",
        "kind",
        "rows",
        "max_rows",
        "expires_in_seconds",
    }
    assert prepared["ref"] != _token(prepared)
    assert _token(prepared) not in prepared["status_url"]  # the status link grants no file
    assert prepared["status_url"] == f"/api/admin/exports/status/{prepared['ref']}"
    first = _status(env, prepared)
    assert first == {
        "ref": prepared["ref"],
        "kind": "attendees",
        "state": "ready",
        "code": None,
        "retry": "none",
        "rows_expected": None,
        "rows_written": None,
    }


def test_a_stream_the_server_finished_is_reported_as_finished_with_exact_counts(env: Env) -> None:
    _insert_attendees(env.engine, 80)
    prepared = _prepare(env, "attendees")
    response = env.client.get(prepared["download_url"])
    assert response.status_code == 200

    status = _status(env, prepared)

    assert status["state"] == "finished"
    assert (
        status["rows_written"] == status["rows_expected"] == int(response.headers["x-export-rows"])
    )
    assert (status["code"], status["retry"]) == (None, "none")


def test_status_follows_a_stream_while_it_runs_and_reports_an_early_close_as_interrupted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        exports,
        "limits",
        lambda: exports.ExportLimits(max_rows=10_000, max_seconds=60, batch_rows=10),
    )
    _insert_attendees(env.engine, 100)
    prepared = _prepare(env, "attendees")
    with Session(env.engine) as db:
        stream = exports.open_download(db, env.factory, actor=_staff(env), token=_token(prepared))
    assert _status(env, prepared)["state"] == "streaming"
    assert _status(env, prepared)["rows_expected"] == stream.run.total

    chunks = stream.chunks()
    next(chunks)
    next(chunks)
    mid = _status(env, prepared)
    assert mid["state"] == "streaming" and 0 < mid["rows_written"] < mid["rows_expected"]

    chunks.close()  # the client went away

    end = _status(env, prepared)
    assert end["state"] == "interrupted"
    assert end["rows_written"] < end["rows_expected"]
    assert (end["code"], end["retry"]) == ("EXPORT_ABORTED", "new_export")


def test_a_break_after_the_response_started_is_reported_as_failed_not_finished(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_attendees(env.engine, 80)
    monkeypatch.setattr(
        exports,
        "limits",
        lambda: exports.ExportLimits(max_rows=1000, max_seconds=60, batch_rows=10),
    )
    calls = {"n": 0}

    def flaky(rows: Any, kinds: Any) -> bytes:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("boom")
        return encode_rows(rows, kinds)

    monkeypatch.setattr("app.modules.admin.exports.encode_rows", flaky)
    prepared = _prepare(env, "attendees")
    with pytest.raises(RuntimeError):
        env.client.get(prepared["download_url"])

    status = _status(env, prepared)
    assert status["state"] == "failed"
    assert status["rows_written"] < status["rows_expected"]
    assert (status["code"], status["retry"]) == ("EXPORT_ABORTED", "new_export")


def test_a_used_ticket_is_refused_and_does_not_change_the_finished_status(env: Env) -> None:
    _insert_attendees(env.engine, 40)
    prepared = _prepare(env, "attendees")
    assert env.client.get(prepared["download_url"]).status_code == 200
    again = env.client.get(prepared["download_url"])  # a double click / replay

    assert again.status_code == 410
    status = _status(env, prepared)
    assert status["state"] == "finished"  # the successful stream is not overwritten
    assert status["rows_written"] == status["rows_expected"]


def test_an_expired_ticket_is_reported_as_expired_and_needs_a_new_export(env: Env) -> None:
    prepared = _prepare(env, "attendees")
    real_clock = tickets._clock
    status_clock = export_status.store._clock
    tickets._clock = lambda: real_clock() + TTL_SECONDS + 1
    export_status.store._clock = lambda: status_clock() + TTL_SECONDS + 1
    try:
        assert _status(env, prepared)["state"] == "expired"  # never requested, now too late
        refused = env.client.get(prepared["download_url"])
        assert refused.status_code == 410
        assert refused.json()["error"]["code"] == "EXPORT_TICKET_INVALID"
        status = _status(env, prepared)
        assert (status["code"], status["retry"]) == ("EXPORT_TICKET_INVALID", "new_export")
    finally:
        tickets._clock = real_clock
        export_status.store._clock = status_clock


def test_a_busy_refusal_keeps_the_ticket_and_the_same_link_works_afterwards(env: Env) -> None:
    _insert_attendees(env.engine, 60)
    actor = _staff(env)
    running = exports.start_export(
        env.factory,
        actor_id=actor.id,
        kind="attendees",
        filters=AttendeeFilters(),
        limit=exports.ExportLimits(max_rows=1000, max_seconds=60, batch_rows=5),
    )
    prepared = _prepare(env, "attendees")

    busy = env.client.get(prepared["download_url"])

    assert busy.status_code == 409
    status = _status(env, prepared)
    assert (status["state"], status["code"], status["retry"]) == (
        "rejected",
        "EXPORT_BUSY",
        "same_link",
    )
    running.run.close_resources()  # the first export ends ...
    retry = env.client.get(prepared["download_url"])  # ... and the SAME link is tried again
    assert retry.status_code == 200
    assert _status(env, prepared)["state"] == "finished"


def test_a_set_that_grew_past_the_limit_is_reported_as_too_large_and_needs_a_new_export(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_attendees(env.engine, 60)
    prepared = _prepare(env, "attendees")
    monkeypatch.setattr(
        exports, "limits", lambda: exports.ExportLimits(max_rows=20, max_seconds=60)
    )

    assert env.client.get(prepared["download_url"]).status_code == 413

    status = _status(env, prepared)
    assert (status["state"], status["code"], status["retry"]) == (
        "rejected",
        "EXPORT_TOO_LARGE",
        "new_export",
    )


def test_a_failure_before_the_first_byte_is_reported_as_rejected_not_as_a_broken_stream(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(env, "attendees")
    monkeypatch.setattr(
        exports.ExportRun,
        "_check_budget",
        lambda self: (_ for _ in ()).throw(exports.ExportTimeLimit("slow")),
    )
    assert env.client.get(prepared["download_url"]).status_code == 503

    status = _status(env, prepared)
    assert (status["state"], status["code"], status["retry"]) == (
        "rejected",
        "EXPORT_FAILED",
        "new_export",
    )
    assert status["rows_written"] is None  # nothing was ever sent


def test_an_unexpected_server_error_while_starting_is_reported_as_an_internal_error(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(env, "attendees")

    def boom(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("database exploded")

    monkeypatch.setattr(exports, "_count", boom)
    with pytest.raises(RuntimeError):
        env.client.get(prepared["download_url"])

    status = _status(env, prepared)
    assert (status["state"], status["code"], status["retry"]) == (
        "rejected",
        "INTERNAL_ERROR",
        "new_export",
    )


def test_maintenance_refuses_the_download_but_keeps_the_ticket(env: Env) -> None:
    prepared = _prepare(env, "attendees")
    _sql(env.engine, "UPDATE event_config SET maintenance_mode = true WHERE id = 1")
    try:
        refused = env.client.get(prepared["download_url"])
        assert refused.status_code == 503
        assert refused.json()["error"]["code"] == "MAINTENANCE"
        status = _status(env, prepared)
        assert (status["state"], status["code"], status["retry"]) == (
            "rejected",
            "MAINTENANCE",
            "same_link",
        )
    finally:
        _sql(env.engine, "UPDATE event_config SET maintenance_mode = false WHERE id = 1")
    assert env.client.get(prepared["download_url"]).status_code == 200  # same link, now fine


def test_a_session_that_ended_is_a_401_on_the_status_too(env: Env) -> None:
    prepared = _prepare(env, "attendees")
    env.client.cookies.clear()  # the session is gone
    download = env.client.get(prepared["download_url"])
    status = env.client.get(prepared["status_url"])
    assert download.status_code == 401 and status.status_code == 401
    assert status.json()["error"]["code"] == "UNAUTHENTICATED"
    _sign_in(env.client, env.engine, StaffRole.ADMIN)  # signing in again: the ticket was not spent
    assert _status(env, prepared)["state"] == "ready"
    assert env.client.get(prepared["download_url"]).status_code == 200


def test_another_administrator_cannot_see_or_disturb_an_exports_status(env: Env) -> None:
    _sql(
        env.engine,
        "INSERT INTO staff_accounts (email, password_hash, full_name, role) "
        "VALUES ('second-admin@test.example', 'x', 'Second Admin', 'ADMIN')",
    )
    _sign_in(env.client, env.engine, StaffRole.ADMIN, nth=0)
    prepared = _prepare(env, "attendees")

    _sign_in(env.client, env.engine, StaffRole.ADMIN, nth=1)  # another admin, same ref
    peek = env.client.get(prepared["status_url"])
    assert peek.status_code == 404
    assert peek.json()["error"]["code"] == "ADMIN_NOT_FOUND"
    # holding the ticket does not let them touch the status either
    assert env.client.get(prepared["download_url"]).status_code == 410

    _sign_in(env.client, env.engine, StaffRole.ADMIN, nth=0)
    assert _status(env, prepared)["state"] == "ready"  # untouched


@pytest.mark.parametrize("role", [None, StaffRole.BOOTH_STAFF])
def test_only_an_administrator_can_ask_for_a_status(env: Env, role: StaffRole | None) -> None:
    prepared = _prepare(env, "attendees")
    if role is None:
        env.client.cookies.clear()
    else:
        _sign_in(env.client, env.engine, role)
    response = env.client.get(prepared["status_url"])
    assert response.status_code == (401 if role is None else 403)
    assert "state" not in response.text


def test_unknown_and_malformed_refs_are_not_found_or_invalid(env: Env) -> None:
    assert env.client.get("/api/admin/exports/status/abcdefghijklmnop").status_code == 404
    assert env.client.get("/api/admin/exports/status/short").status_code == 422
    assert env.client.get("/api/admin/exports/status/has%20space%20inside").status_code == 422


def test_the_status_never_carries_csv_content_filters_or_the_ticket(env: Env) -> None:
    _insert_user(env.engine, "Zorbak", "Secretson", "secretson@private.test", consent=True)
    prepared = _prepare(env, "attendees", {"q": "Secretson"})
    env.client.get(prepared["download_url"])
    response = env.client.get(prepared["status_url"])

    blob = response.text
    for private in ("Secretson", "secretson", "private.test", "Zorbak", _token(prepared), "q="):
        assert private not in blob, private
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {
        "ref",
        "kind",
        "state",
        "code",
        "retry",
        "rows_expected",
        "rows_written",
    }


def test_status_requests_are_rate_limited_separately(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(env, "attendees")
    monkeypatch.setattr(
        exports, "EXPORT_STATUS_PER_ACCOUNT", RateLimit("admin:export-status:t", 3, 60)
    )
    assert [env.client.get(prepared["status_url"]).status_code for _ in range(4)] == [
        200,
        200,
        200,
        429,
    ]


def test_the_status_store_is_bounded_and_forgets_old_entries() -> None:
    clock = {"now": 0.0}
    store = export_status.StatusStore(clock=lambda: clock["now"])
    refs = [
        store.create(actor_id=1, kind="attendees", ticket=f"ticket-{n:040d}", ticket_ttl=60)
        for n in range(export_status.MAX_PER_ADMIN + 5)
    ]
    assert store.view(refs[0], actor_id=1) is None  # displaced by newer ones of the same admin
    assert store.view(refs[-1], actor_id=1) is not None
    assert store.view(refs[-1], actor_id=2) is None  # and never visible to anyone else

    clock["now"] = export_status.RETENTION_SECONDS + 1  # long after
    assert store.view(refs[-1], actor_id=1) is None
    assert store.ref_for_ticket(f"ticket-{0:040d}") is None  # the ticket digest went with it


# --- the audit view shows what the exports wrote ----------------------------------


def _audit_events(env: Env, **params: Any) -> tuple[list[dict[str, Any]], str]:
    response = env.client.get("/api/admin/audit", params={"target_type": "export", **params})
    assert response.status_code == 200, response.text
    return response.json()["items"], response.text


def test_a_finished_export_shows_in_the_audit_view_with_its_outcome_and_counts(
    env: Env,
) -> None:
    _insert_attendees(env.engine, 25)
    response = _export(env, "attendees", {"q": "Zorbak", "status": "PROVISIONAL"})
    written = len(_parse(response)) - 1  # without the header row
    assert written > 0

    events, raw = _audit_events(env)

    assert [e["action"] for e in events] == ["export.finished", "export.started"]
    finished, started = events
    assert started["operation_id"] == finished["operation_id"] == started["target_id"]
    assert len(started["operation_id"]) == 16
    assert started["result"]["outcome"] == "started"
    assert finished["result"]["outcome"] == "stream_finished"
    assert finished["result"]["rows_written"] == finished["result"]["rows_expected"] == written
    assert finished["result"]["kind"] == "attendees" and finished["result"]["bytes_written"] > 0
    assert started["result"]["filters_used"] == ["q", "status"]  # names only
    assert "Zorbak" not in raw  # the search text is personal data: never recorded, never shown
    # one operation's events are found together by its id
    same, _ = _audit_events(env, target_id=started["operation_id"])
    assert [e["id"] for e in same] == [finished["id"], started["id"]]


def test_a_refused_export_shows_as_refused_with_the_numbers(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_attendees(env.engine, 60)
    prepared = _prepare(env, "attendees")
    monkeypatch.setattr(
        exports, "limits", lambda: exports.ExportLimits(max_rows=20, max_seconds=60)
    )
    assert env.client.get(prepared["download_url"]).status_code == 413

    events, _ = _audit_events(env)

    assert [e["action"] for e in events] == ["export.rejected"]
    result = events[0]["result"]
    assert result["outcome"] == "rejected" and result["reason"] == "row_limit"
    assert result["rows_matched"] > 20 and result["max_rows"] == 20
    assert result["rows_written"] is None


def test_a_failed_stream_shows_as_failed_without_the_error_text(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_attendees(env.engine, 80)
    monkeypatch.setattr(
        exports,
        "limits",
        lambda: exports.ExportLimits(max_rows=1000, max_seconds=60, batch_rows=10),
    )
    calls = {"n": 0}
    real = encode_rows

    def flaky(rows: Any, kinds: Any) -> bytes:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("disk on fire: secret detail")
        return real(rows, kinds)

    monkeypatch.setattr("app.modules.admin.exports.encode_rows", flaky)
    prepared = _prepare(env, "attendees")
    with pytest.raises(RuntimeError, match="disk on fire"):
        env.client.get(prepared["download_url"])

    events, raw = _audit_events(env)

    finished = next(e for e in events if e["action"] == "export.finished")
    result = finished["result"]
    assert result["outcome"] == "failed" and result["error_class"] == "RuntimeError"
    assert result["rows_written"] < result["rows_expected"]
    assert "secret detail" not in raw and "disk on fire" not in raw


def test_viewing_the_audit_after_exports_writes_no_audit_rows_of_its_own(env: Env) -> None:
    _insert_attendees(env.engine, 10)
    _export(env, "attendees")
    before = _sql(env.engine, "SELECT count(*) FROM admin_audit")[0][0]
    for _ in range(3):
        _audit_events(env)
        env.client.get("/api/admin/audit")
    assert _sql(env.engine, "SELECT count(*) FROM admin_audit")[0][0] == before
