"""The paginated, filterable admin lists: attendees and the points ledger.

Runs against the disposable reset-test database (a scratch database created by
this run only, see tests/suite_db.py) so the bulk data these tests insert can
never reach any other database.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.sessions import create_staff_session
from app.db.session import get_db
from app.main import app
from app.modules.admin.pagination import decode_cursor, encode_cursor, filter_fingerprint
from app.modules.staff.models import StaffAccount, StaffRole

pytestmark = pytest.mark.usefixtures("clean_reset_db")

ATTENDEES = "/api/admin/attendees"
LEDGER = "/api/admin/points-log"
BASE = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)
ATTENDEE_COUNT = 620  # more than the old 500-row cap
AWARD_COUNT = 170
FEEDBACK_COUNT = 40
REVERSAL_COUNT = 12  # 222 ledger rows from these tests: more than the old 200-row cap


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def admin_client(reset_db_engine: Engine) -> Iterator[tuple[TestClient, Engine]]:
    def isolated_db() -> Iterator[Session]:
        with Session(reset_db_engine) as db:
            yield db

    app.dependency_overrides[get_db] = isolated_db
    try:
        with TestClient(app, headers={"Origin": get_settings().app_origin}) as client:
            yield client, reset_db_engine
    finally:
        app.dependency_overrides.clear()


def _sign_in(client: TestClient, engine: Engine, role: StaffRole) -> None:
    with Session(engine) as db:
        staff = db.execute(select(StaffAccount).where(StaffAccount.role == role)).scalars().first()
        assert staff is not None
        token = create_staff_session(db, staff_account_id=staff.id, now=datetime.now(UTC))
        db.commit()
    client.cookies.clear()
    client.cookies.set("session", token)


@pytest.fixture
def admin(admin_client: tuple[TestClient, Engine]) -> tuple[TestClient, Engine]:
    client, engine = admin_client
    _sign_in(client, engine, StaffRole.ADMIN)
    return client, engine


def _insert_attendees(engine: Engine, count: int = ATTENDEE_COUNT) -> None:
    """`count` attendees; created_at moves one second per THREE rows, so the
    list is full of timestamp ties. Every 5th is PROVISIONAL, every 2nd has
    given sponsor consent, every 10th is a 'Zorbak', every 7th has the e-mail
    domain 'corp.test'."""
    with Session(engine) as db:
        db.execute(
            text(
                """
                INSERT INTO users (first_name, last_name, email, phone, profession,
                                   sponsor_consent, sponsor_consent_at, status, activated_at,
                                   created_at)
                SELECT CASE WHEN g % 10 = 0 THEN 'Zorbak' ELSE 'Person' || g END,
                       CASE WHEN g % 11 = 0 THEN 'O_Brien' ELSE 'Surname' || g END,
                       'list' || g || CASE WHEN g % 7 = 0 THEN '@corp.test' ELSE '@mail.test' END,
                       '+10000000' || g, 'Engineer',
                       g % 2 = 0,
                       CASE WHEN g % 2 = 0 THEN :base ELSE NULL END,
                       CASE WHEN g % 5 = 0 THEN 'PROVISIONAL'::user_status
                            ELSE 'ACTIVE'::user_status END,
                       CASE WHEN g % 5 = 0 THEN NULL ELSE :base END,
                       :base + (g / 3) * interval '1 second'
                  FROM generate_series(1, :n) AS g
                """
            ),
            {"base": BASE, "n": count},
        )
        db.commit()


def _insert_ledger(engine: Engine) -> dict[str, Any]:
    """AWARD_COUNT task awards (a quarter of them legacy: no task_id, two
    booths, two staff accounts), FEEDBACK_COUNT feedback rewards and
    REVERSAL_COUNT reversals, in timestamp-tie groups of four."""
    _insert_attendees(engine, 80)
    with Session(engine) as db:
        booths = db.execute(
            text(
                "SELECT b.id AS booth_id, s.id AS staff_id FROM booths b "
                "JOIN staff_accounts s ON s.booth_id = b.id AND s.role = 'BOOTH_STAFF' "
                "ORDER BY b.id LIMIT 2"
            )
        ).all()
        assert len(booths) == 2
        users = [
            int(r[0])
            for r in db.execute(
                text("SELECT id FROM users WHERE email LIKE 'list%' ORDER BY id LIMIT 80")
            )
        ]
        task_of = {
            b.booth_id: int(
                db.execute(
                    text("SELECT id FROM tasks WHERE booth_id = :b ORDER BY id LIMIT 1"),
                    {"b": b.booth_id},
                ).scalar_one()
            )
            for b in booths
        }
        rows = []
        for n in range(AWARD_COUNT):
            booth = booths[n % 2]
            rows.append(
                {
                    "u": users[n % 60],
                    "b": booth.booth_id,
                    "pts": 5 + n % 3,
                    "t": None if n % 4 == 0 else task_of[booth.booth_id],
                    "s": booth.staff_id,
                    "at": BASE + timedelta(seconds=n // 4),
                }
            )
        db.execute(
            text(
                "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, task_id, "
                "entered_by_staff_id, entered_by_system, created_at) "
                "VALUES (:u, :b, 'TASK', 'AWARD', :pts, :t, :s, false, :at)"
            ),
            rows,
        )
        db.execute(
            text(
                "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                "entered_by_system, created_at) "
                "VALUES (:u, :b, 'FEEDBACK', 'AWARD', 5, true, :at)"
            ),
            [
                {
                    "u": users[40 + n],
                    "b": booths[0].booth_id,
                    "at": BASE + timedelta(seconds=n // 4),
                }
                for n in range(FEEDBACK_COUNT)
            ],
        )
        award_ids = [
            int(r[0])
            for r in db.execute(
                text(
                    "SELECT id FROM points_log WHERE source = 'TASK' AND user_id = ANY(:us) "
                    "ORDER BY id LIMIT :n"
                ),
                {"us": users, "n": REVERSAL_COUNT},
            )
        ]
        for n, award_id in enumerate(award_ids):
            db.execute(
                text(
                    "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                    "entered_by_staff_id, entered_by_system, reverses_points_log_id, created_at) "
                    "SELECT user_id, booth_id, 'TASK', 'REVERSAL', -points, entered_by_staff_id, "
                    "false, id, :at FROM points_log WHERE id = :id"
                ),
                {"id": award_id, "at": BASE + timedelta(seconds=n // 4)},
            )
        db.commit()
    return {"booths": booths, "tasks": task_of, "users": users}


def _ids(engine: Engine, sql: str, **params: Any) -> list[int]:
    with Session(engine) as db:
        return [int(r[0]) for r in db.execute(text(sql), params)]


def _walk(
    client: TestClient, path: str, params: dict[str, Any] | None = None, limit: int | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Follows next_cursor to the end; returns (all items, number of pages)."""
    items: list[dict[str, Any]] = []
    pages = 0
    cursor: str | None = None
    while True:
        query = dict(params or {})
        if limit is not None:
            query["limit"] = limit
        if cursor is not None:
            query["cursor"] = cursor
        response = client.get(path, params=query)
        assert response.status_code == 200, response.text
        page = response.json()
        pages += 1
        assert pages < 5_000, "pagination does not terminate"
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return items, pages


def _error(response: Any) -> tuple[int, str]:
    return int(response.status_code), str(response.json()["error"]["code"])


# --- attendees: walking every page ------------------------------------------


@pytest.mark.parametrize("limit", [None, 100, 7, 1])
def test_every_attendee_is_returned_exactly_once_in_a_stable_order(
    admin: tuple[TestClient, Engine], limit: int | None
) -> None:
    client, engine = admin
    _insert_attendees(engine)
    expected = _ids(engine, "SELECT id FROM users ORDER BY created_at DESC, id DESC")
    assert len(expected) > 500  # more than the old cap

    if limit == 1:  # one-row pages over 600+ rows: a smaller sample is enough
        items, pages = _walk(client, ATTENDEES, {"q": "Zorbak"}, limit=1)
        expected = _ids(
            engine,
            "SELECT id FROM users WHERE first_name ILIKE '%Zorbak%' "
            "ORDER BY created_at DESC, id DESC",
        )
        assert pages == len(expected)
    else:
        items, pages = _walk(client, ATTENDEES, limit=limit)
        per_page = limit or 50
        assert pages == -(-len(expected) // per_page)

    assert [row["id"] for row in items] == expected  # nothing lost, nothing repeated
    assert len({row["id"] for row in items}) == len(items)


def test_ties_on_the_timestamp_never_repeat_or_drop_a_row_across_a_page_boundary(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _insert_attendees(engine, 90)  # rows share a timestamp in threes
    expected = _ids(engine, "SELECT id FROM users ORDER BY created_at DESC, id DESC")
    shared = _ids(
        engine,
        "SELECT min(id) FROM users GROUP BY created_at HAVING count(*) > 1",
    )
    assert shared, "the fixture must contain ties"
    for limit in (2, 4, 5):  # boundaries fall inside tie groups
        items, _ = _walk(client, ATTENDEES, limit=limit)
        assert [row["id"] for row in items] == expected, limit


def test_rows_added_while_paging_do_not_shift_the_pages_already_being_read(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _insert_attendees(engine, 120)
    original = _ids(engine, "SELECT id FROM users ORDER BY created_at DESC, id DESC")

    first = client.get(ATTENDEES, params={"limit": 50}).json()
    with Session(engine) as db:  # a newer registration arrives mid-walk
        db.execute(
            text(
                "INSERT INTO users (first_name, last_name, email, phone, profession, "
                "sponsor_consent, status, activated_at, created_at) VALUES "
                "('Late','Comer','late@mail.test','+1','Eng',false,'ACTIVE',now(),now())"
            )
        )
        db.commit()
    cursor = first["next_cursor"]
    seen = [row["id"] for row in first["items"]]
    while cursor:
        page = client.get(ATTENDEES, params={"limit": 50, "cursor": cursor}).json()
        seen.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
    assert seen == original  # the late row is at the head, not inside the pages being read


# --- attendees: search and filters -------------------------------------------


def test_search_matches_first_name_last_name_and_email_case_insensitively(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _insert_attendees(engine)

    def found(**params: Any) -> list[int]:
        return [row["id"] for row in _walk(client, ATTENDEES, params)[0]]

    def expect(where: str, **bind: Any) -> list[int]:
        return _ids(
            engine, f"SELECT id FROM users WHERE {where} ORDER BY created_at DESC, id DESC", **bind
        )

    assert found(q="zorbak") == expect("first_name = 'Zorbak'")  # first name, any case
    assert found(q="SURNAME12") == expect("last_name ILIKE '%surname12%'")  # last name
    assert found(q="CORP.TEST") == expect("email::text ILIKE '%corp.test%'")  # e-mail
    assert found(q="  list1  ") == expect("email::text ILIKE '%list1%'")  # padded input
    # every word must match somewhere: first name AND e-mail domain
    assert found(q="zorbak corp.test") == expect(
        "first_name = 'Zorbak' AND email::text ILIKE '%corp.test%'"
    )
    assert found(q="nobody-has-this-name") == []


def test_wildcard_characters_in_the_search_text_are_literal(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _insert_attendees(engine)
    underscore = [row["last_name"] for row in _walk(client, ATTENDEES, {"q": "O_Brien"})[0]]
    assert underscore and set(underscore) == {"O_Brien"}  # '_' is not "any character"
    assert _walk(client, ATTENDEES, {"q": "O.Brien"})[0] == []
    assert _walk(client, ATTENDEES, {"q": "%"})[0] == []  # '%' is not "everything"
    assert _walk(client, ATTENDEES, {"q": "\\"})[0] == []


def test_status_and_sponsor_consent_filters_and_their_combinations(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _insert_attendees(engine)
    cases = [
        ({"status": "PROVISIONAL"}, "status = 'PROVISIONAL'"),
        ({"status": "ACTIVE"}, "status = 'ACTIVE'"),
        ({"sponsor_consent": "true"}, "sponsor_consent"),
        ({"sponsor_consent": "false"}, "NOT sponsor_consent"),
        (
            {"status": "ACTIVE", "sponsor_consent": "true", "q": "corp.test"},
            "status = 'ACTIVE' AND sponsor_consent AND email::text ILIKE '%corp.test%'",
        ),
    ]
    for params, where in cases:
        got = [row["id"] for row in _walk(client, ATTENDEES, params, limit=60)[0]]
        want = _ids(engine, f"SELECT id FROM users WHERE {where} ORDER BY created_at DESC, id DESC")
        assert got == want, params
        assert want, params


def test_the_registration_range_includes_its_start_and_excludes_its_end(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _insert_attendees(engine, 30)
    edge = BASE + timedelta(seconds=4)  # rows 12..14 share it

    def ids(**params: Any) -> set[int]:
        return {row["id"] for row in _walk(client, ATTENDEES, params)[0]}

    at_edge = set(_ids(engine, "SELECT id FROM users WHERE created_at = :t", t=edge))
    assert len(at_edge) == 3
    inclusive_start = ids(registered_from=edge.isoformat())
    assert at_edge <= inclusive_start  # `from` is inclusive
    exclusive_end = ids(registered_to=edge.isoformat())
    assert not (at_edge & exclusive_end)  # `to` is exclusive
    # consecutive ranges tile exactly: nothing lost, nothing counted twice
    middle = BASE + timedelta(seconds=6)
    low = ids(registered_to=middle.isoformat())
    high = ids(registered_from=middle.isoformat())
    assert not (low & high)
    assert low | high == {row["id"] for row in _walk(client, ATTENDEES)[0]}
    # the same instant written in another offset selects the same rows
    other_offset = edge.astimezone(timezone_plus(4)).isoformat()
    assert ids(registered_from=other_offset) == inclusive_start
    # a window around exactly one second
    window = ids(
        registered_from=edge.isoformat(), registered_to=(edge + timedelta(seconds=1)).isoformat()
    )
    assert window == at_edge


def timezone_plus(hours: int) -> Any:
    from datetime import timezone

    return timezone(timedelta(hours=hours))


# --- invalid input ----------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": -5},
        {"limit": "many"},
        {"status": "DELETED"},
        {"sponsor_consent": "maybe"},
        {"q": "x" * 101},
        {"cursor": "x" * 513},
        {"registered_from": "yesterday"},
        {"registered_from": "2026-09-01T10:00:00"},  # no UTC offset: ambiguous, refused
        {"registered_to": "2026-09-01"},
        {"registered_from": "2026-09-02T00:00:00Z", "registered_to": "2026-09-01T00:00:00Z"},
        {"registered_from": "2026-09-01T00:00:00Z", "registered_to": "2026-09-01T00:00:00Z"},
        {"colour": "red"},  # unknown parameter: never silently ignored
        {"user_id": 1},  # a points-ledger filter on the attendee list
    ],
)
def test_attendee_list_rejects_bad_parameters(
    admin: tuple[TestClient, Engine], params: dict[str, Any]
) -> None:
    client, _ = admin
    response = client.get(ATTENDEES, params=params)
    assert _error(response) == (422, "VALIDATION_ERROR")


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"user_id": 0},
        {"booth_id": "x"},
        {"task_id": -1},
        {"staff_id": 0},
        {"source": "MANUAL"},
        {"entry_type": "REFUND"},
        {"created_from": "2026-09-01T10:00:00"},
        {"created_from": "2026-09-02T00:00:00Z", "created_to": "2026-09-01T00:00:00Z"},
        {"q": "ada"},  # an attendee-list parameter on the ledger
        {"status": "ACTIVE"},
    ],
)
def test_ledger_list_rejects_bad_parameters(
    admin: tuple[TestClient, Engine], params: dict[str, Any]
) -> None:
    client, _ = admin
    assert _error(client.get(LEDGER, params=params)) == (422, "VALIDATION_ERROR")


def test_a_plus_sign_offset_works_when_encoded_and_a_bare_plus_is_refused(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _insert_attendees(engine, 12)
    instant = "2026-09-01T14:00:03+04:00"  # = 10:00:03Z
    encoded = client.get(f"{ATTENDEES}?{urlencode({'registered_from': instant})}")
    assert encoded.status_code == 200
    bare = client.get(f"{ATTENDEES}?registered_from={instant}")  # '+' decodes to a space
    assert _error(bare) == (422, "VALIDATION_ERROR")


# --- cursors ----------------------------------------------------------------


def test_cursors_are_bound_to_their_list_and_their_filters(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _insert_ledger(engine)  # also inserts 80 attendees

    people = client.get(ATTENDEES, params={"limit": 5}).json()["next_cursor"]
    ledger = client.get(LEDGER, params={"limit": 5}).json()["next_cursor"]
    assert people and ledger and people != ledger

    # a cursor works with the filters it was issued for - and a different page size
    assert client.get(ATTENDEES, params={"limit": 9, "cursor": people}).status_code == 200
    # ... but not another list's, nor another filter set's
    assert _error(client.get(ATTENDEES, params={"cursor": ledger})) == (422, "VALIDATION_ERROR")
    assert _error(client.get(LEDGER, params={"cursor": people})) == (422, "VALIDATION_ERROR")
    assert _error(client.get(ATTENDEES, params={"cursor": people, "status": "ACTIVE"})) == (
        422,
        "VALIDATION_ERROR",
    )
    assert _error(client.get(ATTENDEES, params={"cursor": people, "q": "x"})) == (
        422,
        "VALIDATION_ERROR",
    )
    assert _error(client.get(LEDGER, params={"cursor": ledger, "source": "TASK"})) == (
        422,
        "VALIDATION_ERROR",
    )


@pytest.mark.parametrize(
    "mangle",
    [
        lambda c: c[:-1] + ("A" if c[-1] != "A" else "B"),  # one signature character changed
        lambda c: c.split(".")[0],  # signature removed
        lambda c: "." + c.split(".")[1],  # body removed
        lambda c: c + "x",
        lambda c: "not-a-cursor",
        lambda c: "e30.abc",  # '{}' with a made-up signature
        lambda c: "ü" + c,
    ],
)
def test_forged_or_damaged_cursors_are_refused(
    admin: tuple[TestClient, Engine], mangle: Any
) -> None:
    client, engine = admin
    _insert_attendees(engine)
    cursor = client.get(ATTENDEES, params={"limit": 5}).json()["next_cursor"]
    response = client.get(ATTENDEES, params={"cursor": mangle(cursor)})
    assert _error(response) == (422, "VALIDATION_ERROR")


def test_a_hand_made_cursor_with_a_valid_shape_but_no_signature_is_refused(
    admin: tuple[TestClient, Engine],
) -> None:
    import base64

    client, _ = admin
    body = base64.urlsafe_b64encode(
        json.dumps({"k": "attendees", "t": BASE.isoformat(), "i": 5, "f": "x"}).encode()
    ).decode()
    assert _error(client.get(ATTENDEES, params={"cursor": body + ".AAAAAAAAAAAAAAAAAAAAAA"})) == (
        422,
        "VALIDATION_ERROR",
    )


def test_cursor_helpers_roundtrip_and_ignore_irrelevant_differences() -> None:
    fp = filter_fingerprint("attendees", {"q": "ada", "status": None, "sponsor_consent": None})
    assert fp == filter_fingerprint("attendees", {"q": "ada"})  # unset == absent
    assert fp != filter_fingerprint("attendees", {"q": "ada", "status": "ACTIVE"})
    assert fp != filter_fingerprint("points-log", {"q": "ada"})
    a = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    b = a.astimezone(timezone_plus(4))
    assert filter_fingerprint("x", {"f": a}) == filter_fingerprint("x", {"f": b})  # same instant

    cursor = encode_cursor("attendees", created_at=b, row_id=77, fingerprint=fp)
    at, row_id = decode_cursor("attendees", cursor, fingerprint=fp)
    assert (at, row_id) == (a, 77)


# --- the ledger -------------------------------------------------------------


def test_every_ledger_row_is_returned_exactly_once_across_pages(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _insert_ledger(engine)
    expected = _ids(engine, "SELECT id FROM points_log ORDER BY created_at DESC, id DESC")
    assert len(expected) > 200  # more than the old cap
    for limit in (None, 100, 7, 4):
        items, pages = _walk(client, LEDGER, limit=limit)
        assert [row["id"] for row in items] == expected, limit
        assert pages == -(-len(expected) // (limit or 50))


def test_ledger_filters_and_their_combinations_match_the_database(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    seed = _insert_ledger(engine)
    booth_a, booth_b = seed["booths"]
    task_a = seed["tasks"][booth_a.booth_id]
    user = seed["users"][3]
    cases: list[tuple[dict[str, Any], str, dict[str, Any]]] = [
        ({"user_id": user}, "user_id = :u", {"u": user}),
        ({"booth_id": booth_b.booth_id}, "booth_id = :b", {"b": booth_b.booth_id}),
        ({"task_id": task_a}, "task_id = :t", {"t": task_a}),
        ({"staff_id": booth_a.staff_id}, "entered_by_staff_id = :s", {"s": booth_a.staff_id}),
        ({"source": "FEEDBACK"}, "source = 'FEEDBACK'", {}),
        ({"source": "TASK"}, "source = 'TASK'", {}),
        ({"entry_type": "REVERSAL"}, "entry_type = 'REVERSAL'", {}),
        (
            {"booth_id": booth_a.booth_id, "source": "TASK", "entry_type": "AWARD"},
            "booth_id = :b AND source = 'TASK' AND entry_type = 'AWARD'",
            {"b": booth_a.booth_id},
        ),
        (
            {"task_id": task_a, "staff_id": booth_a.staff_id, "entry_type": "AWARD"},
            "task_id = :t AND entered_by_staff_id = :s AND entry_type = 'AWARD'",
            {"t": task_a, "s": booth_a.staff_id},
        ),
        (
            {"created_from": (BASE + timedelta(seconds=5)).isoformat()},
            "created_at >= :f",
            {"f": BASE + timedelta(seconds=5)},
        ),
        (
            {
                "created_from": (BASE + timedelta(seconds=5)).isoformat(),
                "created_to": (BASE + timedelta(seconds=9)).isoformat(),
                "source": "TASK",
            },
            "created_at >= :f AND created_at < :t AND source = 'TASK'",
            {"f": BASE + timedelta(seconds=5), "t": BASE + timedelta(seconds=9)},
        ),
    ]
    for params, where, bind in cases:
        got = [row["id"] for row in _walk(client, LEDGER, params, limit=13)[0]]
        want = _ids(
            engine,
            f"SELECT id FROM points_log WHERE {where} ORDER BY created_at DESC, id DESC",
            **bind,
        )
        assert got == want, params
        assert want, params  # every case is non-trivial


def test_ledger_date_range_is_inclusive_at_the_start_and_exclusive_at_the_end(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _insert_ledger(engine)
    edge = BASE + timedelta(seconds=7)
    at_edge = set(_ids(engine, "SELECT id FROM points_log WHERE created_at = :t", t=edge))
    assert len(at_edge) >= 4  # a tie group

    def ids(**params: Any) -> set[int]:
        return {row["id"] for row in _walk(client, LEDGER, params)[0]}

    assert at_edge <= ids(created_from=edge.isoformat())
    assert not (at_edge & ids(created_to=edge.isoformat()))
    assert ids(created_to=edge.isoformat()) | ids(created_from=edge.isoformat()) == ids()
    assert not (ids(created_to=edge.isoformat()) & ids(created_from=edge.isoformat()))


def test_ledger_rows_name_the_activity_and_fall_back_for_older_rows(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    seed = _insert_ledger(engine)
    items, _ = _walk(client, LEDGER, limit=100)
    by_kind: dict[str, dict[str, Any]] = {}
    for row in items:
        by_kind.setdefault(row["activity"], row)
    with Session(engine) as db:
        titles = {
            int(r[0]): str(r[1])
            for r in db.execute(
                text("SELECT id, title FROM tasks WHERE id = ANY(:ids)"),
                {"ids": list(seed["tasks"].values())},
            )
        }
    for row in items:
        if row["task_id"] is not None:
            assert row["activity"] == titles[row["task_id"]]
        elif row["source"] == "FEEDBACK":
            assert row["activity"] == "Feedback reward"
        else:
            assert row["activity"] == "Booth-level award (before activities)"
    assert {"Feedback reward", "Booth-level award (before activities)"} <= set(by_kind)
    one = items[0]
    assert one["attendee_name"] and one["booth_name"]
    feedback = next(r for r in items if r["source"] == "FEEDBACK")
    assert feedback["staff_name"] is None and feedback["entered_by_system"] is True
    award = next(r for r in items if r["source"] == "TASK" and r["entry_type"] == "AWARD")
    assert award["staff_name"] and award["entered_by_staff_id"]


def test_reading_the_ledger_changes_nothing(admin: tuple[TestClient, Engine]) -> None:
    client, engine = admin
    _insert_ledger(engine)
    snapshot = "SELECT count(*), coalesce(sum(points), 0), coalesce(sum(id), 0) FROM points_log"
    with Session(engine) as db:
        before = tuple(db.execute(text(snapshot)).one())
    _walk(client, LEDGER, limit=10)
    _walk(client, LEDGER, {"source": "TASK"}, limit=33)
    with Session(engine) as db:
        assert tuple(db.execute(text(snapshot)).one()) == before


# --- who may read, and what they get ----------------------------------------


@pytest.mark.parametrize("path", [ATTENDEES, LEDGER])
def test_only_administrators_may_read_the_lists(
    admin_client: tuple[TestClient, Engine], path: str
) -> None:
    client, engine = admin_client
    client.cookies.clear()
    anonymous = client.get(path)
    assert _error(anonymous) == (401, "UNAUTHENTICATED")
    # authority is decided before the query is even looked at: a bad parameter
    # must not turn into a hint for an anonymous caller
    assert _error(client.get(path, params={"limit": 0})) == (401, "UNAUTHENTICATED")

    _sign_in(client, engine, StaffRole.BOOTH_STAFF)
    staff = client.get(path)
    assert staff.status_code == 403
    assert _error(client.get(path, params={"limit": 0}))[0] == 403
    assert "items" not in staff.text

    _sign_in(client, engine, StaffRole.ADMIN)
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", [ATTENDEES, LEDGER])
def test_personal_data_is_never_cached(admin: tuple[TestClient, Engine], path: str) -> None:
    client, engine = admin
    _insert_ledger(engine)
    assert client.get(path).headers["cache-control"] == "no-store"
    assert (
        client.get(path, params={"limit": 0}).headers["cache-control"] == "no-store"
    )  # even a 422
    client.cookies.clear()
    assert client.get(path).headers["cache-control"] == "no-store"  # and a 401


def test_responses_carry_only_the_fields_an_administrator_needs(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _insert_ledger(engine)
    people = client.get(ATTENDEES).json()
    assert set(people) == {"items", "next_cursor", "limit"}
    assert set(people["items"][0]) == {
        "id",
        "first_name",
        "last_name",
        "email",
        "status",
        "sponsor_consent",
        "created_at",
    }
    ledger = client.get(LEDGER).json()
    assert set(ledger) == {"items", "next_cursor", "limit"}
    assert set(ledger["items"][0]) == {
        "id",
        "user_id",
        "attendee_name",
        "booth_id",
        "booth_name",
        "source",
        "entry_type",
        "points",
        "task_id",
        "activity",
        "entered_by_staff_id",
        "staff_name",
        "entered_by_system",
        "created_at",
        "reverses_entry_id",
        "reversed_by_entry_id",
        "reason",
    }
    blob = (
        client.get(ATTENDEES, params={"limit": 100}).text
        + client.get(LEDGER, params={"limit": 100}).text
    )
    for forbidden in ("+10000000", "phone", "linkedin", "password", "token_hash", "card_token"):
        assert forbidden not in blob, forbidden


def test_timestamps_are_timezone_aware_and_the_page_size_defaults_are_enforced(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _insert_attendees(engine, 130)
    page = client.get(ATTENDEES).json()
    assert page["limit"] == 50 and len(page["items"]) == 50
    big = client.get(ATTENDEES, params={"limit": 100}).json()
    assert big["limit"] == 100 and len(big["items"]) == 100
    for row in page["items"]:
        stamp = datetime.fromisoformat(row["created_at"])
        assert stamp.tzinfo is not None and stamp.utcoffset() is not None


# --- indexes ----------------------------------------------------------------


def test_the_list_queries_have_indexes_to_use(admin: tuple[TestClient, Engine]) -> None:
    _, engine = admin
    with Session(engine) as db:
        names = {
            str(r[0])
            for r in db.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            )
        }
        assert {
            "ix_users_created_at_id",
            "ix_points_log_created_at_id",
            "ix_points_log_task_id_created_at_id",
            "ix_points_log_staff_id_created_at_id",
        } <= names

        db.execute(text("SET LOCAL enable_seqscan = off"))
        db.execute(text("SET LOCAL enable_sort = off"))  # an index must provide the order

        def plan(sql: str) -> str:
            return "\n".join(str(r[0]) for r in db.execute(text("EXPLAIN " + sql)))

        assert "ix_users_created_at_id" in plan(
            "SELECT id FROM users ORDER BY created_at DESC, id DESC LIMIT 51"
        )
        assert "ix_points_log_created_at_id" in plan(
            "SELECT id FROM points_log ORDER BY created_at DESC, id DESC LIMIT 51"
        )
        assert "ix_points_log_task_id_created_at_id" in plan(
            "SELECT id FROM points_log WHERE task_id = 1 ORDER BY created_at DESC, id DESC LIMIT 51"
        )
        assert "ix_points_log_staff_id_created_at_id" in plan(
            "SELECT id FROM points_log WHERE entered_by_staff_id = 1 "
            "ORDER BY created_at DESC, id DESC LIMIT 51"
        )
