"""The admin audit view (`GET /api/admin/audit`, app/modules/admin/audit.py).

Runs against the disposable reset-test database (a scratch database created by
this run only, see tests/suite_db.py). Rows are planted with SQL where a test
needs volume, ties or an old/odd shape, and written by the REAL admin
operations where a test needs the exact payload the writers produce.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import ResourceClosedError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.sessions import create_attendee_session, create_staff_session
from app.db.session import get_db
from app.main import app
from app.modules.admin import audit as audit_view
from app.modules.admin import service as admin_service
from app.modules.admin.models import AdminAudit
from app.modules.admin.pagination import filter_fingerprint
from app.modules.attendees.models import User
from app.modules.staff.models import StaffAccount, StaffRole

pytestmark = pytest.mark.usefixtures("clean_reset_db")

AUDIT = "/api/admin/audit"
BASE = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)
ROWS = 230  # four pages of 50 and one of 30; seven rows share each timestamp


# --- fixtures and helpers -----------------------------------------------------


@pytest.fixture
def audit_client(reset_db_engine: Engine) -> Iterator[tuple[TestClient, Engine]]:
    def isolated_db() -> Iterator[Session]:
        with Session(reset_db_engine) as db:
            yield db

    app.dependency_overrides[get_db] = isolated_db
    try:
        with TestClient(app, headers={"Origin": get_settings().app_origin}) as client:
            yield client, reset_db_engine
    finally:
        app.dependency_overrides.clear()


def _sign_in(client: TestClient, engine: Engine, role: StaffRole) -> int:
    with Session(engine) as db:
        staff = (
            db.execute(
                select(StaffAccount).where(StaffAccount.role == role).order_by(StaffAccount.id)
            )
            .scalars()
            .first()
        )
        assert staff is not None
        token = create_staff_session(db, staff_account_id=staff.id, now=datetime.now(UTC))
        staff_id = staff.id
        db.commit()
    client.cookies.clear()
    client.cookies.set("session", token)
    return staff_id


@pytest.fixture
def admin(audit_client: tuple[TestClient, Engine]) -> tuple[TestClient, Engine]:
    client, engine = audit_client
    _sign_in(client, engine, StaffRole.ADMIN)
    return client, engine


def _sql(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with Session(engine) as db:
        result = db.execute(text(sql), params)
        try:
            rows = list(result.all())
        except ResourceClosedError:  # a statement that returns no rows
            rows = []
        db.commit()
        return rows


def _staff_ids(engine: Engine) -> tuple[int, int]:
    """(an admin account id, a booth-staff account id) of the seeded fixtures."""
    admin_id = _sql(engine, "SELECT min(id) FROM staff_accounts WHERE role = 'ADMIN'")[0][0]
    booth_id = _sql(engine, "SELECT min(id) FROM staff_accounts WHERE role = 'BOOTH_STAFF'")[0][0]
    return int(admin_id), int(booth_id)


def _plant(
    engine: Engine,
    *,
    action: str = "booth.update",
    actor: int | None = None,
    target_type: str = "booth",
    target_id: str | None = "1",
    before: object = None,
    after: object = None,
    at: datetime = BASE,
) -> int:
    actor = actor if actor is not None else _staff_ids(engine)[0]
    return int(
        _sql(
            engine,
            "INSERT INTO admin_audit (staff_account_id, action, target_type, target_id, "
            "before_value, after_value, created_at) VALUES (:a, :ac, :tt, :ti, "
            "CAST(:b AS jsonb), CAST(:af AS jsonb), :at) RETURNING id",
            a=actor,
            ac=action,
            tt=target_type,
            ti=target_id,
            b=json.dumps(before) if before is not None else None,
            af=json.dumps(after) if after is not None else None,
            at=at,
        )[0][0]
    )


def _plant_many(engine: Engine, count: int = ROWS) -> None:
    """`count` rows; created_at moves one second per SEVEN rows (a tie group is
    larger than a page boundary can ever hide), three actions, two actors."""
    admin_id, staff_id = _staff_ids(engine)
    _sql(
        engine,
        "INSERT INTO admin_audit (staff_account_id, action, target_type, target_id, "
        "before_value, after_value, created_at) "
        "SELECT CASE WHEN g % 2 = 0 THEN :admin ELSE :staff END, "
        "       (ARRAY['booth.update', 'task.update', 'interest.update'])[1 + g % 3], "
        "       (ARRAY['booth', 'task', 'interest'])[1 + g % 3], "
        "       (g % 5)::text, "
        "       jsonb_build_object('id', g % 5, 'name', 'before ' || g), "
        "       jsonb_build_object('id', g % 5, 'name', 'after ' || g), "
        "       :base + (g / 7) * interval '1 second' "
        "FROM generate_series(1, :n) AS g",
        admin=admin_id,
        staff=staff_id,
        base=BASE,
        n=count,
    )


def _get(client: TestClient, **params: Any) -> Any:
    return client.get(AUDIT, params={k: v for k, v in params.items() if v is not None})


def _error(response: Any) -> tuple[int, str]:
    return int(response.status_code), str(response.json()["error"]["code"])


def _walk(client: TestClient, **params: Any) -> tuple[list[dict[str, Any]], list[int]]:
    """Follows next_cursor to the end: (all items, the size of each page)."""
    items: list[dict[str, Any]] = []
    sizes: list[int] = []
    cursor: str | None = None
    for _ in range(100):
        response = _get(client, cursor=cursor, **params)
        assert response.status_code == 200, response.text
        page = response.json()
        items += page["items"]
        sizes.append(len(page["items"]))
        cursor = page["next_cursor"]
        if cursor is None:
            return items, sizes
    raise AssertionError("pagination did not end")


# --- authority, caching, read-only ---------------------------------------------


def test_only_an_administrator_may_read_the_audit(
    audit_client: tuple[TestClient, Engine],
) -> None:
    client, engine = audit_client
    _plant(engine)
    assert client.get(AUDIT).status_code == 401  # anonymous

    with Session(engine) as db:
        user = db.execute(select(User)).scalars().first()
        if user is None:  # the fixtures hold no attendee: make one
            db.execute(
                text(
                    "INSERT INTO users (first_name, last_name, email, phone, profession, "
                    "sponsor_consent, status, activated_at) VALUES "
                    "('A','B','audit-auth@x.test','+1','Eng', false, 'ACTIVE', now())"
                )
            )
            db.commit()
            user = db.execute(select(User)).scalars().first()
        assert user is not None
        token = create_attendee_session(db, user_id=user.id, now=datetime.now(UTC))
        db.commit()
    client.cookies.set("session", token)
    assert client.get(AUDIT).status_code == 401  # an attendee session is not staff

    _sign_in(client, engine, StaffRole.BOOTH_STAFF)
    forbidden = client.get(AUDIT)
    assert _error(forbidden) == (403, "STAFF_NOT_AUTHORIZED")

    _sign_in(client, engine, StaffRole.ADMIN)
    assert client.get(AUDIT).status_code == 200


def test_the_audit_is_never_cached_and_has_the_documented_shape(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _plant(engine, after={"id": 1, "name": "Hall"})
    response = client.get(AUDIT)
    assert response.headers["Cache-Control"] == "no-store"
    page = response.json()
    assert set(page) == {"items", "next_cursor", "limit"}
    assert page["limit"] == 50
    event = page["items"][0]
    assert set(event) == {
        "id",
        "created_at",
        "actor_id",
        "actor_name",
        "action",
        "target_type",
        "target_id",
        "operation_id",
        "before",
        "after",
        "changed_fields",
        "result",
        "hidden_field_count",
        "details",
    }
    assert event["created_at"].endswith(("Z", "+00:00"))  # timezone-aware
    with Session(engine) as db:
        name = db.execute(
            select(StaffAccount.full_name).where(StaffAccount.id == event["actor_id"])
        ).scalar_one()
    assert event["actor_name"] == name


def test_reading_the_audit_creates_no_audit_rows(admin: tuple[TestClient, Engine]) -> None:
    client, engine = admin
    _plant_many(engine, 30)
    before = _sql(engine, "SELECT count(*), max(id) FROM admin_audit")[0]
    for params in (
        {},
        {"limit": 5},
        {"action": "booth.update"},
        {"target_type": "booth", "target_id": "1"},
        {"created_from": BASE.isoformat()},
        {"limit": 0},  # a refused request writes nothing either
        {"bogus": "1"},
    ):
        client.get(AUDIT, params=params)
    first = client.get(AUDIT, params={"limit": 5}).json()
    client.get(AUDIT, params={"limit": 5, "cursor": first["next_cursor"]})
    assert _sql(engine, "SELECT count(*), max(id) FROM admin_audit")[0] == before


# --- pagination ----------------------------------------------------------------


def test_walking_every_page_yields_each_row_once_in_a_stable_total_order(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _plant_many(engine)

    items, sizes = _walk(client)  # default page size

    assert sizes == [50, 50, 50, 50, 30]
    ids = [e["id"] for e in items]
    assert len(ids) == len(set(ids)) == ROWS
    expected = [
        int(r[0])
        for r in _sql(engine, "SELECT id FROM admin_audit ORDER BY created_at DESC, id DESC")
    ]
    assert ids == expected
    stamps = [(e["created_at"], e["id"]) for e in items]
    assert stamps == sorted(stamps, reverse=True)
    # ties exist and a page boundary fell inside one: that is the case under test
    assert len({e["created_at"] for e in items}) < ROWS / 5


def test_the_page_size_has_a_default_a_maximum_and_may_change_between_pages(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _plant_many(engine, 130)
    assert len(_get(client).json()["items"]) == 50
    assert len(_get(client, limit=100).json()["items"]) == 100
    for bad in (0, 101, -1, "x", ""):
        assert _get(client, limit=bad).status_code == 422
    first = _get(client, limit=10).json()
    second = _get(client, limit=25, cursor=first["next_cursor"]).json()
    assert len(second["items"]) == 25
    assert {e["id"] for e in first["items"]}.isdisjoint(e["id"] for e in second["items"])


def test_a_row_written_while_paging_does_not_shift_the_pages_already_read(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _plant_many(engine, 60)
    first = _get(client, limit=20).json()
    newest = _plant(engine, at=BASE + timedelta(days=1))
    rest = []
    cursor = first["next_cursor"]
    while cursor:
        page = _get(client, limit=20, cursor=cursor).json()
        rest += page["items"]
        cursor = page["next_cursor"]
    seen = [e["id"] for e in first["items"] + rest]
    assert len(seen) == len(set(seen)) == 60
    assert newest not in seen  # it is at the head; the cursor points into the middle
    assert _get(client, limit=1).json()["items"][0]["id"] == newest


def test_an_empty_trail_is_an_empty_page(admin: tuple[TestClient, Engine]) -> None:
    client, _ = admin
    page = client.get(AUDIT).json()
    assert page["items"] == [] and page["next_cursor"] is None


# --- filters -------------------------------------------------------------------


def test_filters_by_actor_action_and_target(admin: tuple[TestClient, Engine]) -> None:
    client, engine = admin
    _plant_many(engine, 90)
    admin_id, staff_id = _staff_ids(engine)

    by_actor, _ = _walk(client, actor_id=staff_id)
    assert by_actor and {e["actor_id"] for e in by_actor} == {staff_id}
    assert len(by_actor) == int(
        _sql(engine, "SELECT count(*) FROM admin_audit WHERE staff_account_id = :a", a=staff_id)[0][
            0
        ]
    )

    by_action, _ = _walk(client, action="task.update")
    assert by_action and {e["action"] for e in by_action} == {"task.update"}

    by_type, _ = _walk(client, target_type="interest")
    assert by_type and {e["target_type"] for e in by_type} == {"interest"}

    one, _ = _walk(client, target_type="task", target_id="3")
    assert one and {(e["target_type"], e["target_id"]) for e in one} == {("task", "3")}

    both, _ = _walk(client, actor_id=admin_id, action="booth.update", target_type="booth")
    assert both and all(
        e["actor_id"] == admin_id and e["action"] == "booth.update" for e in both
    )  # filters combine with AND

    nothing = _get(client, action="no.such_thing").json()
    assert nothing["items"] == [] and nothing["next_cursor"] is None


def test_the_date_range_includes_its_start_and_excludes_its_end(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    hour = timedelta(hours=1)
    ids = {
        "t0": _plant(engine, at=BASE),
        "t1": _plant(engine, at=BASE + hour),
        "t2a": _plant(engine, at=BASE + 2 * hour),
        "t2b": _plant(engine, at=BASE + 2 * hour),
        "t3": _plant(engine, at=BASE + 3 * hour),
    }
    inside, _ = _walk(
        client, created_from=(BASE + hour).isoformat(), created_to=(BASE + 3 * hour).isoformat()
    )
    assert {e["id"] for e in inside} == {ids["t1"], ids["t2a"], ids["t2b"]}

    # the same instants written in another offset mean the same thing
    plus4 = timezone_of(4)
    shifted, _ = _walk(
        client,
        created_from=(BASE + hour).astimezone(plus4).isoformat(),
        created_to=(BASE + 3 * hour).astimezone(plus4).isoformat(),
    )
    assert {e["id"] for e in shifted} == {ids["t1"], ids["t2a"], ids["t2b"]}

    # consecutive ranges tile: no gap, no overlap
    left, _ = _walk(client, created_to=(BASE + 2 * hour).isoformat())
    right, _ = _walk(client, created_from=(BASE + 2 * hour).isoformat())
    assert {e["id"] for e in left} | {e["id"] for e in right} == set(ids.values())
    assert {e["id"] for e in left}.isdisjoint(e["id"] for e in right)

    only_from, _ = _walk(client, created_from=(BASE + 3 * hour).isoformat())
    assert [e["id"] for e in only_from] == [ids["t3"]]


def timezone_of(hours: int) -> Any:
    from datetime import timezone

    return timezone(timedelta(hours=hours))


@pytest.mark.parametrize(
    "params",
    [
        {"unknown": "1"},
        {"actor_id": "0"},
        {"actor_id": "-3"},
        {"actor_id": "abc"},
        {"actor_id": "1.5"},
        {"action": "Booth.Update"},
        {"action": "booth update"},
        {"action": "booth.update; DROP TABLE admin_audit"},
        {"action": "x" * 65},
        {"action": ".update"},
        {"target_type": "Booth"},
        {"target_type": "a/b"},
        {"target_id": "5"},  # an id means nothing without its type
        {"target_type": "booth", "target_id": "5 OR 1=1"},
        {"target_type": "booth", "target_id": "x" * 65},
        {"created_from": "2026-09-01T10:00:00"},  # no timezone
        {"created_to": "2026-09-01"},
        {"created_from": "yesterday"},
        {"created_from": "2026-09-02T00:00:00Z", "created_to": "2026-09-01T00:00:00Z"},
        {"created_from": "2026-09-02T00:00:00Z", "created_to": "2026-09-02T00:00:00Z"},
        {"cursor": "x" * 513},
        {"cursor": "not-a-cursor"},
        {"limit": "50", "page": "2"},
    ],
)
def test_invalid_and_unknown_parameters_are_refused(
    admin: tuple[TestClient, Engine], params: dict[str, str]
) -> None:
    client, engine = admin
    _plant(engine, target_type="booth", target_id="5")
    response = client.get(AUDIT, params=params)
    assert _error(response) == (422, "VALIDATION_ERROR"), params


def test_a_blank_cursor_means_the_first_page(admin: tuple[TestClient, Engine]) -> None:
    client, engine = admin
    _plant(engine)
    assert client.get(AUDIT, params={"cursor": ""}).status_code == 200


# --- cursors -------------------------------------------------------------------


def test_cursors_are_bound_to_the_audit_list_and_to_its_filters(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _plant_many(engine, 60)
    admin_id, staff_id = _staff_ids(engine)
    plain = _get(client, limit=2).json()["next_cursor"]
    by_actor = _get(client, limit=2, actor_id=admin_id).json()["next_cursor"]
    by_action = _get(client, limit=2, action="booth.update").json()["next_cursor"]
    by_target = _get(client, limit=2, target_type="booth", target_id="1").json()["next_cursor"]
    by_date = _get(client, limit=2, created_from=BASE.isoformat()).json()["next_cursor"]
    assert all([plain, by_actor, by_action, by_target, by_date])

    # works with exactly the filters it was issued for
    assert _get(client, cursor=plain).status_code == 200
    assert _get(client, cursor=by_actor, actor_id=admin_id).status_code == 200
    assert _get(client, cursor=by_date, created_from=BASE.isoformat()).status_code == 200
    # ...and the same instant in another offset is the same filter
    assert (
        _get(
            client, cursor=by_date, created_from=BASE.astimezone(timezone_of(3)).isoformat()
        ).status_code
        == 200
    )

    invalid = (422, "VALIDATION_ERROR")
    assert _error(_get(client, cursor=plain, actor_id=admin_id)) == invalid
    assert _error(_get(client, cursor=by_actor)) == invalid
    assert _error(_get(client, cursor=by_actor, actor_id=staff_id)) == invalid
    assert _error(_get(client, cursor=by_action, action="task.update")) == invalid
    assert _error(_get(client, cursor=by_target, target_type="booth", target_id="2")) == invalid
    assert _error(_get(client, cursor=by_target, target_type="booth")) == invalid
    assert (
        _error(_get(client, cursor=by_date, created_from=(BASE + timedelta(1)).isoformat()))
        == invalid
    )
    assert (
        _error(_get(client, cursor=by_date, created_to=(BASE + timedelta(9)).isoformat()))
        == invalid
    )


def test_a_cursor_of_another_list_or_a_damaged_one_is_refused(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _plant_many(engine, 30)
    audit_cursor = _get(client, limit=5).json()["next_cursor"]
    # a cursor of the same shape issued for another list (same secret, other kind)
    from app.modules.admin.pagination import encode_cursor

    foreign = encode_cursor(
        "points-log", created_at=BASE, row_id=5, fingerprint=filter_fingerprint("points-log", {})
    )
    invalid = (422, "VALIDATION_ERROR")
    assert _error(_get(client, cursor=foreign)) == invalid
    body = audit_cursor.split(".")[0]
    assert _error(_get(client, cursor=body + ".AAAAAAAAAAAAAAAAAAAAAA")) == invalid
    assert _error(_get(client, cursor=audit_cursor[:-3] + "AAA")) == invalid
    assert _error(_get(client, cursor=audit_cursor.replace(".", ""))) == invalid
    # and the audit cursor does not open another list
    assert client.get("/api/admin/points-log", params={"cursor": audit_cursor}).status_code == 422
    assert client.get("/api/admin/attendees", params={"cursor": audit_cursor}).status_code == 422


# --- what is shown: every kind of event ----------------------------------------


def _run_every_admin_operation(client: TestClient) -> str:
    """Drives the real operations so the audit holds the payloads the writers
    produce. Returns the staff password used (it must never be shown)."""
    password = "Correct-horse-battery-staple-9"
    booths = client.get("/api/admin/booths").json()
    interests = client.get("/api/admin/interests").json()
    booth, interest = booths[0]["id"], interests[0]["id"]

    def ok(response: Any) -> Any:
        assert response.status_code < 300, response.text
        return response.json() if response.content else None

    made = ok(client.post("/api/admin/booths", json={"slug": "audit-view", "name": "Audit view"}))
    ok(client.patch(f"/api/admin/booths/{made['id']}", json={"name": "Audit view 2"}))
    ok(client.put(f"/api/admin/booths/{booth}/weights/{interest}", json={"weight": 0.5}))
    ok(client.put(f"/api/admin/booths/{booth}/weights/{interest}", json={"weight": 0.75}))
    ok(client.delete(f"/api/admin/booths/{booth}/weights/{interest}"))
    new_interest = ok(
        client.post("/api/admin/interests", json={"slug": "audit-i", "name": "Audit"})
    )
    ok(client.patch(f"/api/admin/interests/{new_interest['id']}", json={"name": "Audit 2"}))
    task = ok(
        client.post(
            "/api/admin/tasks",
            json={
                "booth_id": booth,
                "title": "Audit task",
                "points": 7,
                "allow_repeat_points": None,
            },
        )
    )
    ok(client.patch(f"/api/admin/tasks/{task['id']}", json={"points": 9}))
    preset = ok(client.post("/api/admin/presets", json={"booth_id": booth, "points": 3}))
    ok(client.patch(f"/api/admin/presets/{preset['id']}", json={"points": 4}))
    ok(client.patch("/api/admin/event-config", json={"feedback_points": 6}))
    staff = ok(
        client.post(
            "/api/admin/staff",
            json={
                "email": "audit-person@example.com",
                "password": password,
                "full_name": "Hidden Person Name",
                "role": "BOOTH_STAFF",
                "booth_id": booth,
            },
        )
    )
    ok(client.post(f"/api/admin/staff/{staff['id']}/deactivate"))
    return password


def test_every_admin_operation_is_shown_through_its_allowlist(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    password = _run_every_admin_operation(client)

    response = client.get(AUDIT, params={"limit": 100})
    seen: dict[str, list[dict[str, Any]]] = {}
    for event in response.json()["items"]:
        seen.setdefault(event["action"], []).append(event)

    # every operation of the admin service, nothing missing and nothing extra (the points
    # reversal and the password events are written by their own modules and tested there)
    other_modules = {
        "points.reverse",
        "staff.password_change",
        "staff.password_reset",
        "staff.password_admin_set",
        "staff.password_reset_requested",
        # the masterclass programme (masterclasses/admin.py, tests/test_masterclasses.py)
        *(a for a in audit_view.FIELD_ALLOWLISTS if a.startswith("masterclass")),
        # the competition (competition/, people/, mentors/ and their own tests)
        *(
            a
            for a in audit_view.FIELD_ALLOWLISTS
            if a.startswith(
                (
                    "panel.",
                    "staff_scope.",
                    "competition.",
                    "match_table.",
                    "mentor_session.",
                    "final.",
                    "pair_photo.",
                    "offline_scan.",
                    "booth.leads",
                )
            )
        ),
    }
    assert set(seen) == set(audit_view.FIELD_ALLOWLISTS) - other_modules
    for action, events in seen.items():
        assert all(e["details"] == "known" for e in events), action
        assert all(e["result"] is None and e["operation_id"] is None for e in events), action

    # a create has no "before"; an update shows exactly what changed
    created = seen["booth.create"][0]
    assert created["before"] is None and created["after"]["slug"] == "audit-view"
    updated = seen["booth.update"][0]
    assert updated["changed_fields"] == ["name"]
    assert updated["before"]["name"] == "Audit view" and updated["after"]["name"] == "Audit view 2"
    task = seen["task.update"][0]
    assert task["changed_fields"] == ["points"]
    assert (task["before"]["points"], task["after"]["points"]) == (7, 9)
    config = seen["event_config.update"][0]
    assert (
        config["changed_fields"] == ["feedback_points"] and config["after"]["feedback_points"] == 6
    )

    # a weight: set (the seeded row had 1.00), change again, remove. The writer
    # records only the weight as "before" and the ids as well as "after": the ids
    # are context, not a change.
    weights = sorted(seen["booth.weight"], key=lambda e: e["id"])
    assert [w["changed_fields"] for w in weights] == [["weight"], ["weight"]]
    assert [(Decimal(w["before"]["weight"]), Decimal(w["after"]["weight"])) for w in weights] == [
        (Decimal("1.00"), Decimal("0.5")),
        (Decimal("0.5"), Decimal("0.75")),
    ]
    assert {"id", "booth_id", "interest_id"} <= set(weights[0]["after"])
    removed = seen["booth.weight.remove"][0]
    assert removed["after"] is None and Decimal(removed["before"]["weight"]) == Decimal("0.75")
    assert set(removed["changed_fields"]) == {"id", "booth_id", "interest_id", "weight"}

    # staff: the account's e-mail address and name are personal data and not needed here
    raw = response.text
    assert "audit-person@example.com" not in raw
    assert "Hidden Person Name" not in raw
    assert password not in raw
    for event in seen["staff.create"] + seen["staff.deactivate"]:
        for side in ("before", "after"):
            assert event[side] is None or set(event[side]) <= {
                "id",
                "role",
                "booth_id",
                "is_active",
            }
        assert event["hidden_field_count"] == 2  # exactly: email, full_name
    deactivated = seen["staff.deactivate"][0]
    assert deactivated["changed_fields"] == ["is_active"]
    assert deactivated["before"]["is_active"] is True and deactivated["after"]["is_active"] is False


def test_the_allowlists_cover_every_action_the_admin_service_can_write() -> None:
    """A new admin operation that is not allowlisted would be shown as metadata
    only: fail loudly instead, so the writer's author decides what to show."""
    source = Path(admin_service.__file__).read_text(encoding="utf-8")
    written = set(re.findall(r'return\s+"([a-z_]+(?:\.[a-z_]+)+)",\s*"[a-z_]+"', source))
    assert written, "the pattern found no audited operation: update this test"
    assert written <= set(audit_view.FIELD_ALLOWLISTS), written - set(audit_view.FIELD_ALLOWLISTS)
    # audit actions written by other modules are module constants: AUDIT_ACTION /
    # AUDIT_<NAME> = "area.action". Every one of them must be allowlisted too.
    app_root = Path(admin_service.__file__).resolve().parents[2]
    constants: set[str] = set()
    for module in app_root.rglob("*.py"):
        constants |= set(
            re.findall(
                r'^AUDIT_[A-Z_]*\s*=\s*"([a-z_]+(?:\.[a-z_]+)+)"',
                module.read_text(encoding="utf-8"),
                flags=re.MULTILINE,
            )
        )
    assert {"points.reverse", "staff.password_change", "staff.password_reset"} <= constants
    assert constants <= set(audit_view.FIELD_ALLOWLISTS), constants - set(
        audit_view.FIELD_ALLOWLISTS
    )
    assert set(audit_view.EXPORT_ACTIONS) == {
        "export.started",
        "export.finished",
        "export.rejected",
    }


SECRET_VALUES = {
    "password": "pw-PLAINTEXT-1",
    "password_hash": "$argon2id$v=19$m=65536,t=3,p=4$SECRETSALT$SECRETHASH",
    "otp": "482913",
    "otp_code": "991822",
    "qr_token": "QRTOKEN-abcdefghijklmnopqrstuvwxyz0123456789",
    "card_token": "CARDTOKEN-abcdefghijklmnopqrstuvwxyz01234",
    "ticket": "TICKET-abcdefghijklmnopqrstuvwxyz0123456789ab",
    "session": "SESSIONCOOKIE-abcdef",
    "session_token": "SESSIONTOKEN-ghijkl",
    "ciphertext": "CIPHERTEXT-mnopqr",
    "api_key": "sk-live-APIKEYVALUE",
    "secret": "SHHH-value",
    "authorization": "Bearer BEARERVALUE",
}


@pytest.mark.parametrize("action", sorted(audit_view.FIELD_ALLOWLISTS))
def test_secrets_never_pass_an_allowlisted_event_even_when_nested(
    admin: tuple[TestClient, Engine], action: str
) -> None:
    client, engine = admin
    allowed = {name: 1 for name in audit_view.FIELD_ALLOWLISTS[action]}
    payload = {
        **allowed,
        **SECRET_VALUES,
        "nested": {"password": "NESTED-PW", "deep": {"token": "DEEP-TOKEN"}},
        "list": ["LIST-SECRET", {"api_key": "LIST-KEY"}],
    }
    # a secret hidden inside the value of an allowed field must not come out either
    first_text_field = next(
        (n for n in audit_view.FIELD_ALLOWLISTS[action] if n in {"name", "title", "slug", "label"}),
        None,
    )
    if first_text_field is not None:
        payload[first_text_field] = {"password": "INSIDE-FIELD"}
    _plant(engine, action=action, before=payload, after=payload)

    response = client.get(AUDIT)
    assert response.status_code == 200
    raw = response.text
    for value in [
        *SECRET_VALUES.values(),
        "NESTED-PW",
        "DEEP-TOKEN",
        "LIST-SECRET",
        "LIST-KEY",
        "INSIDE-FIELD",
    ]:
        assert value not in raw, (action, value)
    for name in ("password_hash", "qr_token", "api_key", "ciphertext", "nested"):
        assert name not in raw, (action, name)
    event = response.json()["items"][0]
    assert event["hidden_field_count"] >= len(SECRET_VALUES) + 2  # counted, never named
    for side in ("before", "after"):
        assert set(event[side]) <= set(audit_view.FIELD_ALLOWLISTS[action])
        assert all(not isinstance(v, (dict, list)) for v in event[side].values())


def test_a_value_of_the_wrong_type_is_withheld_not_converted(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _plant(
        engine,
        action="booth.update",
        before={"id": 1, "name": "ok", "is_active": "yes", "slug": 12345},
        after={"id": 1.5, "name": "x" * 5000, "is_active": True, "description": ["a"]},
    )
    event = client.get(AUDIT).json()["items"][0]
    assert event["before"] == {"id": 1, "name": "ok"}
    assert event["after"]["is_active"] is True and "id" not in event["after"]
    assert len(event["after"]["name"]) <= audit_view.TEXT_LIMIT
    assert "description" not in event["after"]
    assert event["hidden_field_count"] >= 4

    _plant(engine, action="staff.create", after={"id": 9, "role": "SUPERUSER", "is_active": True})
    staff = client.get(AUDIT).json()["items"][0]
    assert "role" not in staff["after"]


def test_text_that_looks_like_a_credential_is_masked_before_it_is_shown(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    card = "A" * 43
    ticket = "B" * 43
    _plant(
        engine,
        action="booth.update",
        after={
            "id": 1,
            "description": (
                f"scan https://x.test/c/{card} or /api/admin/exports/download?ticket={ticket}"
            ),
        },
    )
    shown = client.get(AUDIT).json()["items"][0]["after"]["description"]
    assert card not in shown and ticket not in shown
    assert "/c/***" in shown and "ticket=***" in shown


def test_markup_is_data_not_markup(admin: tuple[TestClient, Engine]) -> None:
    client, engine = admin
    payload = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    _plant(engine, action="booth.update", after={"id": 1, "name": payload, "slug": "x"})
    response = client.get(AUDIT)
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["items"][0]["after"]["name"] == payload  # shown verbatim as TEXT


def test_an_unknown_event_is_shown_as_metadata_only(admin: tuple[TestClient, Engine]) -> None:
    client, engine = admin
    _plant(
        engine,
        action="weird.new_thing",
        target_type="gadget",
        target_id="42",
        before={"password": "OLD-SECRET"},
        after={"api_key": "sk-NEW-SECRET", "nested": {"token": "TOKEN-VALUE"}, "name": "Gadget"},
    )
    response = client.get(AUDIT)
    event = response.json()["items"][0]
    assert event["details"] == "limited"
    assert (event["action"], event["target_type"], event["target_id"]) == (
        "weird.new_thing",
        "gadget",
        "42",
    )
    assert event["before"] is None and event["after"] is None
    assert event["result"] is None and event["changed_fields"] == []
    assert event["hidden_field_count"] == 4  # password, api_key, nested, name - counted only
    for leaked in ("OLD-SECRET", "sk-NEW-SECRET", "TOKEN-VALUE", "Gadget", "api_key", "nested"):
        assert leaked not in response.text


@pytest.mark.parametrize("stored", ['"just a string"', "[1, 2, 3]", "42", "true"])
def test_a_stored_value_that_is_not_an_object_is_shown_as_nothing(
    admin: tuple[TestClient, Engine], stored: str
) -> None:
    client, engine = admin
    for action in ("booth.update", "weird.kind", "export.finished"):
        _sql(
            engine,
            "INSERT INTO admin_audit (staff_account_id, action, target_type, target_id, "
            "before_value, after_value) VALUES "
            "(:a, :ac, 'x', '1', CAST(:v AS jsonb), CAST(:v AS jsonb))",
            a=_staff_ids(engine)[0],
            ac=action,
            v=stored,
        )
    response = client.get(AUDIT)
    assert response.status_code == 200
    for event in response.json()["items"][:3]:
        assert event["before"] is None and event["after"] is None
        assert event["hidden_field_count"] >= 1
    assert "just a string" not in response.text


# --- export events -------------------------------------------------------------


def test_export_events_show_outcome_and_counts_and_share_an_operation_id(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    admin_id = _staff_ids(engine)[0]
    op = "0123456789abcdef"
    _plant(
        engine,
        action="export.started",
        target_type="export",
        target_id=op,
        after={
            "kind": "attendees",
            "filters_used": ["q", "status", "not_a_filter"],
            "snapshot_at": "2026-09-01T10:00:00+00:00",
            "rows_expected": 12,
            "leak": "SHOULD-NOT-SHOW",
        },
        at=BASE,
    )
    _plant(
        engine,
        action="export.finished",
        target_type="export",
        target_id=op,
        after={
            "kind": "attendees",
            "outcome": "interrupted",
            "reason": "closed_before_end",
            "error_class": None,
            "rows_written": 5,
            "rows_expected": 12,
            "bytes_written": 900,
            "elapsed_ms": 31,
        },
        at=BASE + timedelta(seconds=2),
    )
    _plant(
        engine,
        action="export.finished",
        target_type="export",
        target_id="ffffffffffffffff",
        after={
            "kind": "points-log",
            "outcome": "failed",
            "reason": "error",
            "error_class": "RuntimeError",
            "rows_written": 1,
            "rows_expected": 9,
            "bytes_written": 10,
            "elapsed_ms": 4,
        },
        at=BASE + timedelta(seconds=3),
    )
    _plant(
        engine,
        action="export.rejected",
        target_type="export",
        target_id="eeeeeeeeeeeeeeee",
        after={
            "kind": "sponsor-contacts",
            "filters_used": [],
            "reason": "row_limit",
            "rows_matched": 150000,
            "max_rows": 100000,
        },
        at=BASE + timedelta(seconds=4),
    )

    response = client.get(AUDIT)
    by_id: dict[str, list[dict[str, Any]]] = {e["target_id"]: [] for e in response.json()["items"]}
    for event in response.json()["items"]:
        by_id[event["target_id"]].append(event)
    assert "SHOULD-NOT-SHOW" not in response.text

    started, finished = (
        next(e for e in by_id[op] if e["action"] == "export.started"),
        next(e for e in by_id[op] if e["action"] == "export.finished"),
    )
    assert started["operation_id"] == finished["operation_id"] == op
    assert started["result"]["outcome"] == "started"
    assert started["result"]["filters_used"] == ["q", "status"]  # a made-up name is dropped
    assert started["result"]["rows_expected"] == 12 and started["result"]["rows_written"] is None
    assert started["result"]["snapshot_at"].startswith("2026-09-01T10:00:00")
    assert started["hidden_field_count"] == 1  # "leak"
    assert started["before"] is None and started["after"] is None
    assert finished["result"] == {
        "kind": "attendees",
        "outcome": "interrupted",
        "reason": "closed_before_end",
        "error_class": None,
        "rows_expected": 12,
        "rows_written": 5,
        "rows_matched": None,
        "max_rows": None,
        "bytes_written": 900,
        "elapsed_ms": 31,
        "snapshot_at": None,
        "filters_used": [],
    }
    failed = by_id["ffffffffffffffff"][0]["result"]
    assert (failed["outcome"], failed["error_class"], failed["rows_written"]) == (
        "failed",
        "RuntimeError",
        1,
    )
    rejected = by_id["eeeeeeeeeeeeeeee"][0]
    assert (
        rejected["result"]["outcome"] == "rejected" and rejected["result"]["reason"] == "row_limit"
    )
    assert (rejected["result"]["rows_matched"], rejected["result"]["max_rows"]) == (150000, 100000)

    # the events of one operation are found together by its id
    events, _ = _walk(client, target_type="export", target_id=op)
    assert [e["action"] for e in events] == ["export.finished", "export.started"]
    assert events[0]["actor_id"] == admin_id


def test_an_export_event_with_malformed_fields_shows_only_what_is_well_formed(
    admin: tuple[TestClient, Engine],
) -> None:
    client, engine = admin
    _plant(
        engine,
        action="export.finished",
        target_type="export",
        target_id="abc",
        after={
            "kind": "<b>x</b>",
            "outcome": "party",
            "reason": "bad reason with spaces",
            "error_class": "Not A Class!",
            "rows_written": "12",
            "rows_expected": -4,
            "bytes_written": {"a": 1},
            "elapsed_ms": 1.5,
            "filters_used": "q",
            "snapshot_at": "yesterday",
        },
    )
    result = client.get(AUDIT).json()["items"][0]["result"]
    assert result == {
        "kind": None,
        "outcome": None,
        "reason": None,
        "error_class": None,
        "rows_expected": None,
        "rows_written": None,
        "rows_matched": None,
        "max_rows": None,
        "bytes_written": None,
        "elapsed_ms": None,
        "snapshot_at": None,
        "filters_used": [],
    }


def test_known_actions_and_filter_names_are_consistent() -> None:
    from app.modules.admin.schemas import AttendeeFilters, PointsFilters

    assert (
        set(AttendeeFilters.model_fields) | set(PointsFilters.model_fields)
        == audit_view._FILTER_NAMES
    )
    assert (*audit_view.FIELD_ALLOWLISTS, *audit_view.EXPORT_ACTIONS) == audit_view.KNOWN_ACTIONS


def test_audit_rows_are_never_modified_by_viewing(admin: tuple[TestClient, Engine]) -> None:
    client, engine = admin
    _plant_many(engine, 40)
    before = _sql(engine, "SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM admin_audit t")
    _walk(client, limit=7)
    with Session(engine) as db:
        assert db.query(AdminAudit).count() == 40
    assert (
        _sql(engine, "SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM admin_audit t")
        == before
    )


# --- the indexes serve the queries ---------------------------------------------


@pytest.mark.parametrize(
    ("where", "index"),
    [
        ("", "ix_admin_audit_created_at_id"),
        ("WHERE staff_account_id = {staff}", "ix_admin_audit_actor_created_at_id"),
        ("WHERE action = 'export.finished'", "ix_admin_audit_action_created_at_id"),
        ("WHERE target_type = 'export'", "ix_admin_audit_target_created_at_id"),
        (
            "WHERE target_type = 'export' AND target_id = '3'",
            "ix_admin_audit_target_created_at_id",
        ),
    ],
)
def test_each_audit_query_can_use_an_index_that_ends_in_the_list_order(
    reset_db_engine: Engine, where: str, index: str
) -> None:
    """A trail of realistic shape (a few rare actors, actions and targets among
    many ordinary rows): each filter is answered from its own index, in the
    list's order (the planner may still sort the few rows a rare value matched)."""
    admin_id, staff_id = _staff_ids(reset_db_engine)
    _sql(
        reset_db_engine,
        "INSERT INTO admin_audit (staff_account_id, action, target_type, target_id, created_at) "
        "SELECT CASE WHEN g % 100 = 0 THEN :staff ELSE :admin END, "
        "       CASE WHEN g % 100 = 0 THEN 'export.finished' ELSE 'booth.update' END, "
        "       CASE WHEN g % 100 = 0 THEN 'export' ELSE 'booth' END, "
        "       CASE WHEN g % 100 = 0 THEN (g / 100)::text ELSE '1' END, "
        "       :base + g * interval '1 second' "
        "FROM generate_series(1, 4000) AS g",
        admin=admin_id,
        staff=staff_id,
        base=BASE,
    )
    with reset_db_engine.connect() as conn:
        conn.execute(text("ANALYZE admin_audit"))
        plan = "\n".join(
            str(r[0])
            for r in conn.execute(
                text(
                    f"EXPLAIN SELECT id FROM admin_audit {where.format(staff=staff_id)} "
                    "ORDER BY created_at DESC, id DESC LIMIT 51"
                )
            )
        )
        conn.rollback()
    assert index in plan, plan
