"""Admin control of an activity's award rule: points, repeat policy, audit,
and how the ledger names activities. Uses the disposable reset-test
database, like the other admin tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.sessions import create_staff_session
from app.db.session import get_db
from app.main import app
from app.modules.admin.models import AdminAudit
from app.modules.staff.models import StaffAccount, StaffRole

pytestmark = pytest.mark.usefixtures("clean_reset_db")


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


def _booth_id(engine: Engine) -> int:
    with Session(engine) as db:
        return int(db.execute(text("SELECT id FROM booths ORDER BY id LIMIT 1")).scalar_one())


def _create(client: TestClient, booth_id: int, **over: Any) -> dict[str, Any]:
    body = {"booth_id": booth_id, "title": "Demo of the product", "points": 12, **over}
    response = client.post("/api/admin/tasks", json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def test_an_admin_creates_an_activity_with_its_rule_and_the_change_is_audited(
    admin_client: tuple[TestClient, Engine],
) -> None:
    client, engine = admin_client
    _sign_in(client, engine, StaffRole.ADMIN)
    booth_id = _booth_id(engine)

    created = _create(client, booth_id, allow_repeat_points=True)

    assert (created["points"], created["allow_repeat_points"]) == (12, True)
    with Session(engine) as db:
        entry = db.execute(select(AdminAudit).order_by(AdminAudit.id.desc())).scalars().first()
        assert entry is not None and entry.action == "task.create"
        assert entry.after_value is not None
        assert entry.after_value["points"] == 12
        assert entry.after_value["allow_repeat_points"] is True


def test_editing_the_rule_records_before_and_after(admin_client: tuple[TestClient, Engine]) -> None:
    client, engine = admin_client
    _sign_in(client, engine, StaffRole.ADMIN)
    task = _create(client, _booth_id(engine), allow_repeat_points=None)

    changed = client.patch(f"/api/admin/tasks/{task['id']}", json={"points": 25})
    assert changed.status_code == 200 and changed.json()["points"] == 25
    repeat = client.patch(f"/api/admin/tasks/{task['id']}", json={"allow_repeat_points": False})
    assert repeat.json()["allow_repeat_points"] is False
    # an explicit null returns to inheriting from the booth / event
    inherit = client.patch(f"/api/admin/tasks/{task['id']}", json={"allow_repeat_points": None})
    assert inherit.status_code == 200 and inherit.json()["allow_repeat_points"] is None

    with Session(engine) as db:
        entries = (
            db.execute(
                select(AdminAudit).where(AdminAudit.action == "task.update").order_by(AdminAudit.id)
            )
            .scalars()
            .all()
        )
    assert [(e.before_value["points"], e.after_value["points"]) for e in entries][0] == (12, 25)  # type: ignore[index]
    assert [e.after_value["allow_repeat_points"] for e in entries] == [None, False, None]  # type: ignore[index]
    assert entries[1].before_value["allow_repeat_points"] is None  # type: ignore[index]


@pytest.mark.parametrize("points", [0, -1, 101, "ten", None, 2.5])
def test_the_award_value_is_validated(admin_client: tuple[TestClient, Engine], points: Any) -> None:
    client, engine = admin_client
    _sign_in(client, engine, StaffRole.ADMIN)
    body = {"booth_id": _booth_id(engine), "title": "Bad value", "points": points}
    assert client.post("/api/admin/tasks", json=body).status_code == 422


def test_an_activity_cannot_be_created_without_a_point_value(
    admin_client: tuple[TestClient, Engine],
) -> None:
    client, engine = admin_client
    _sign_in(client, engine, StaffRole.ADMIN)
    response = client.post(
        "/api/admin/tasks", json={"booth_id": _booth_id(engine), "title": "No value"}
    )
    assert response.status_code == 422


def test_the_value_cannot_be_cleared_or_set_out_of_range_on_edit(
    admin_client: tuple[TestClient, Engine],
) -> None:
    client, engine = admin_client
    _sign_in(client, engine, StaffRole.ADMIN)
    task = _create(client, _booth_id(engine))

    assert client.patch(f"/api/admin/tasks/{task['id']}", json={"points": None}).status_code == 422
    assert client.patch(f"/api/admin/tasks/{task['id']}", json={"points": 500}).status_code == 422
    assert client.patch(f"/api/admin/tasks/{task['id']}", json={"points": 0}).status_code == 422
    assert client.patch(f"/api/admin/tasks/{task['id']}", json={}).status_code == 200  # no-op ok
    again = next(t for t in client.get("/api/admin/tasks").json() if t["id"] == task["id"])
    assert again["points"] == 12


def test_the_rule_is_admin_only_and_unknown_fields_are_rejected(
    admin_client: tuple[TestClient, Engine],
) -> None:
    client, engine = admin_client
    booth_id = _booth_id(engine)
    body = {"booth_id": booth_id, "title": "x", "points": 5}
    assert client.post("/api/admin/tasks", json=body).status_code == 401
    _sign_in(client, engine, StaffRole.BOOTH_STAFF)
    assert client.post("/api/admin/tasks", json=body).status_code == 403
    _sign_in(client, engine, StaffRole.ADMIN)
    assert client.post("/api/admin/tasks", json={**body, "booth": 1}).status_code == 422


def test_the_activity_list_shows_the_rule_that_actually_applies(
    admin_client: tuple[TestClient, Engine],
) -> None:
    client, engine = admin_client
    _sign_in(client, engine, StaffRole.ADMIN)
    booth_id = _booth_id(engine)
    inherits = _create(client, booth_id, title="Inherits")
    forces = _create(client, booth_id, title="Forces repeats", allow_repeat_points=True)

    rows = {r["id"]: r for r in client.get("/api/admin/tasks").json()}

    assert rows[inherits["id"]]["repeat_allowed_effective"] is False  # event default: once
    assert rows[inherits["id"]]["allow_repeat_points"] is None
    assert rows[forces["id"]]["repeat_allowed_effective"] is True
    assert rows[forces["id"]]["points"] == 12

    with Session(engine) as db:
        db.execute(
            text("UPDATE booths SET allow_repeat_task_points = true WHERE id = :b"), {"b": booth_id}
        )
        db.commit()
    rows = {r["id"]: r for r in client.get("/api/admin/tasks").json()}
    assert rows[inherits["id"]]["repeat_allowed_effective"] is True  # now inherits the booth


def test_the_ledger_names_the_activity_and_labels_older_rows_honestly(
    admin_client: tuple[TestClient, Engine],
) -> None:
    client, engine = admin_client
    _sign_in(client, engine, StaffRole.ADMIN)
    booth_id = _booth_id(engine)
    task = _create(client, booth_id, title="Quiz corner")
    with Session(engine) as db:
        user_id = int(db.execute(text("SELECT id FROM users ORDER BY id LIMIT 1")).scalar_one())
        staff_id = int(
            db.execute(
                text("SELECT id FROM staff_accounts WHERE booth_id = :b LIMIT 1"), {"b": booth_id}
            ).scalar_one()
        )
        insert = (
            "INSERT INTO points_log (user_id, booth_id, source, points, task_id, "
            "entered_by_staff_id, entered_by_system, dedupe_key) VALUES "
            "(:u, :b, :src, 5, :t, :s, :sys, :k)"
        )
        db.execute(
            text(insert),
            {
                "u": user_id,
                "b": booth_id,
                "src": "TASK",
                "t": task["id"],
                "s": staff_id,
                "sys": False,
                "k": "n1",
            },
        )
        db.execute(
            text(insert),
            {
                "u": user_id,
                "b": booth_id,
                "src": "TASK",
                "t": None,
                "s": staff_id,
                "sys": False,
                "k": "legacy",
            },
        )
        db.execute(
            text(insert),
            {
                "u": user_id,
                "b": booth_id,
                "src": "FEEDBACK",
                "t": None,
                "s": None,
                "sys": True,
                "k": None,
            },
        )
        db.commit()

    rows = client.get("/api/admin/points-log").json()["items"]
    by_key = {r["activity"]: r for r in rows}

    assert by_key["Quiz corner"]["task_id"] == task["id"]
    assert by_key["Booth-level award (before activities)"]["task_id"] is None
    assert by_key["Feedback reward"]["source"] == "FEEDBACK"
