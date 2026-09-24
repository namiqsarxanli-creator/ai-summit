"""M16 contract tests use only M15's disposable PostgreSQL database."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.core.auth_session import AuthSession
from app.core.config import get_settings
from app.core.locks import FACEMARK_RESET_FENCE
from app.core.ratelimit import RateLimit
from app.core.sessions import create_attendee_session, create_staff_session
from app.db.session import get_db
from app.main import app
from app.modules.admin import service as admin_service
from app.modules.admin.models import AdminAudit, EventConfig
from app.modules.attendees.models import User
from app.modules.booths.models import Booth
from app.modules.points.models import PointsLog
from app.modules.staff.models import StaffAccount, StaffRole

pytestmark = pytest.mark.usefixtures("clean_reset_db")


@pytest.fixture
def admin_test_client(
    reset_db_url: str, reset_db_engine: Engine
) -> Iterator[tuple[TestClient, Engine]]:
    # Override only request DB sessions. The generation middleware's separate
    # read remains harmless; every mutation/read under test uses this engine.
    def isolated_db() -> Iterator[Session]:
        with Session(reset_db_engine) as db:
            yield db

    app.dependency_overrides[get_db] = isolated_db
    try:
        with TestClient(app, headers={"Origin": get_settings().app_origin}) as client:
            yield client, reset_db_engine
    finally:
        app.dependency_overrides.clear()


def _as_role(client: TestClient, engine: Engine, role: StaffRole) -> int:
    with Session(engine) as db:
        staff = db.execute(select(StaffAccount).where(StaffAccount.role == role)).scalars().first()
        assert staff is not None
        token = create_staff_session(db, staff_account_id=staff.id, now=datetime.now(UTC))
        staff_id = staff.id
        db.commit()
    client.cookies.clear()
    client.cookies.set("session", token)
    return staff_id


def test_admin_namespace_denies_anonymous_and_booth_staff(
    admin_test_client: tuple[TestClient, Engine],
) -> None:
    client, engine = admin_test_client
    paths = [
        "attendees",
        "booths",
        "interests",
        "staff",
        "tasks",
        "presets",
        "event-config",
        "points-log",
        "demo-otps",
    ]
    for path in paths:
        assert client.get(f"/api/admin/{path}").status_code == 401
    _as_role(client, engine, StaffRole.BOOTH_STAFF)
    for path in paths:
        response = client.get(f"/api/admin/{path}")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "STAFF_NOT_AUTHORIZED"
    assert client.post("/api/admin/booths", json={"slug": "x", "name": "X"}).status_code == 403


def test_attendee_session_and_spoofed_authority_are_rejected(
    admin_test_client: tuple[TestClient, Engine],
) -> None:
    client, engine = admin_test_client
    with Session(engine) as db:
        user = db.execute(select(User)).scalars().first()
        assert user is not None
        token = create_attendee_session(db, user_id=user.id, now=datetime.now(UTC))
        db.commit()
    client.cookies.set("session", token)
    assert client.get("/api/admin/staff").status_code == 401
    assert client.post("/api/admin/booths", json={"slug": "x", "name": "X"}).status_code == 401
    _as_role(client, engine, StaffRole.ADMIN)
    assert (
        client.post(
            "/api/admin/booths",
            json={"slug": "x", "name": "X", "staff_account_id": 1, "role": "ADMIN"},
        ).status_code
        == 422
    )


def test_admin_reads_and_mutation_audit_are_atomic(
    admin_test_client: tuple[TestClient, Engine],
) -> None:
    client, engine = admin_test_client
    _as_role(client, engine, StaffRole.ADMIN)
    for path in ("attendees", "booths", "interests", "staff", "tasks", "presets", "points-log"):
        assert client.get(f"/api/admin/{path}").status_code == 200
    with Session(engine) as db:
        before_points = db.query(PointsLog).count()
        before_audit = db.query(AdminAudit).count()
    made = client.post("/api/admin/booths", json={"slug": "m16-disposable", "name": "Disposable"})
    assert made.status_code == 201, made.text
    assert made.json()["slug"] == "m16-disposable"
    with Session(engine) as db:
        assert db.query(AdminAudit).count() == before_audit + 1
        assert db.query(PointsLog).count() == before_points
        entry = db.execute(select(AdminAudit).order_by(AdminAudit.id.desc())).scalars().first()
        assert entry is not None and entry.action == "booth.create"
    duplicate = client.post("/api/admin/booths", json={"slug": "m16-disposable", "name": "Other"})
    assert duplicate.status_code == 409
    with Session(engine) as db:
        assert db.query(AdminAudit).count() == before_audit + 1


def test_staff_lifecycle_revokes_existing_sessions(
    admin_test_client: tuple[TestClient, Engine],
) -> None:
    client, engine = admin_test_client
    _as_role(client, engine, StaffRole.ADMIN)
    booth_id = client.get("/api/admin/booths").json()[0]["id"]
    made = client.post(
        "/api/admin/staff",
        json={
            "email": "m16@example.com",
            "password": "strong-password-123",
            "full_name": "M16 Test",
            "role": "BOOTH_STAFF",
            "booth_id": booth_id,
        },
    )
    assert made.status_code == 201, made.text
    staff_id = made.json()["id"]
    duplicate = client.post(
        "/api/admin/staff",
        json={
            "email": "m16@example.com",
            "password": "strong-password-123",
            "full_name": "Duplicate",
            "role": "BOOTH_STAFF",
            "booth_id": booth_id,
        },
    )
    assert duplicate.status_code == 409
    assert (
        client.post(
            "/api/admin/staff",
            json={
                "email": "missing-booth@example.com",
                "password": "strong-password-123",
                "full_name": "Invalid",
                "role": "BOOTH_STAFF",
                "booth_id": None,
            },
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/admin/staff",
            json={
                "email": "admin-booth@example.com",
                "password": "strong-password-123",
                "full_name": "Invalid",
                "role": "ADMIN",
                "booth_id": booth_id,
            },
        ).status_code
        == 400
    )
    with Session(engine) as db:
        staff = db.get(StaffAccount, staff_id)
        assert staff is not None
        assert staff.password_hash != "strong-password-123"
        token = create_staff_session(db, staff_account_id=staff_id, now=datetime.now(UTC))
        db.commit()
    login_payload = {"email": "m16@example.com", "password": "strong-password-123"}
    login_result = client.post("/api/staff/auth/login", json=login_payload)
    assert login_result.status_code == 204, login_result.text
    assert client.get("/api/staff/me").status_code == 200
    _as_role(client, engine, StaffRole.ADMIN)
    assert client.post(f"/api/admin/staff/{staff_id}/deactivate").status_code == 200
    with Session(engine) as db:
        assert db.get(StaffAccount, staff_id).is_active is False  # type: ignore[union-attr]
        session = (
            db.execute(select(AuthSession).where(AuthSession.staff_account_id == staff_id))
            .scalars()
            .first()
        )
        assert session is not None and session.revoked_at is not None
    client.cookies.set("session", token)
    assert client.get("/api/staff/me").status_code == 401
    assert client.post("/api/staff/auth/login", json=login_payload).status_code == 401


def test_config_cannot_edit_generation_or_maintenance(
    admin_test_client: tuple[TestClient, Engine],
) -> None:
    client, engine = admin_test_client
    _as_role(client, engine, StaffRole.ADMIN)
    for field in ("dataset_generation", "maintenance_mode", "staff_account_id"):
        assert client.patch("/api/admin/event-config", json={field: 9}).status_code == 422
    assert client.patch("/api/admin/event-config", json={"feedback_points": 0}).status_code == 422
    assert client.patch("/api/admin/event-config", json={"feedback_points": 7}).status_code == 200
    with Session(engine) as db:
        config = db.get(EventConfig, 1)
        assert config is not None and config.feedback_points == 7
        assert config.dataset_generation == 1 and config.maintenance_mode is False


def test_no_admin_points_write_or_reset_route(admin_test_client: tuple[TestClient, Engine]) -> None:
    client, engine = admin_test_client
    _as_role(client, engine, StaffRole.ADMIN)
    schema = client.get("/openapi.json").json()
    assert "/api/admin/reset" not in schema["paths"]
    assert not any(path.startswith("/api/admin") and "award" in path for path in schema["paths"])
    assert client.post("/api/admin/points-log", json={"points": 999}).status_code == 405
    assert client.post("/api/admin/reset", json={}).status_code == 404


def test_all_catalogue_mutations_audit_without_touching_points(
    admin_test_client: tuple[TestClient, Engine],
) -> None:
    client, engine = admin_test_client
    _as_role(client, engine, StaffRole.ADMIN)
    with Session(engine) as db:
        initial_audits = db.query(AdminAudit).count()
        initial_points = db.query(PointsLog).count()
    calls: list[tuple[str, str, dict[str, object]]] = [
        ("POST", "interests", {"slug": "m16-interest", "name": "M16 Interest"}),
        ("POST", "booths", {"slug": "m16-booth", "name": "M16 Booth"}),
    ]
    created: list[int] = []
    for method, path, body in calls:
        response = client.request(method, f"/api/admin/{path}", json=body)
        assert response.status_code == 201, response.text
        created.append(response.json()["id"])
    interest_id, booth_id = created
    operations: list[tuple[str, str, dict[str, object]]] = [
        ("PATCH", f"interests/{interest_id}", {"name": "Updated interest"}),
        ("PATCH", f"booths/{booth_id}", {"name": "Updated booth"}),
        ("PUT", f"booths/{booth_id}/weights/{interest_id}", {"weight": "0.75"}),
        ("POST", "tasks", {"booth_id": booth_id, "title": "M16 Task"}),
        ("POST", "presets", {"booth_id": booth_id, "points": 17}),
        ("POST", "presets", {"booth_id": None, "points": 19}),
        ("PATCH", "event-config", {"leaderboard_top_n": 11}),
    ]
    task_id = preset_id = 0
    for method, path, body in operations:
        response = client.request(method, f"/api/admin/{path}", json=body)
        assert response.status_code in {200, 201}, (path, response.text)
        if path == "tasks":
            task_id = response.json()["id"]
        if path == "presets" and body["booth_id"] is not None:
            preset_id = response.json()["id"]
    edits: list[tuple[str, dict[str, object]]] = [
        (f"tasks/{task_id}", {"title": "Updated task"}),
        (f"presets/{preset_id}", {"points": 18}),
    ]
    for path, body in edits:
        response = client.patch(f"/api/admin/{path}", json=body)
        assert response.status_code == 200, response.text
    removed = client.delete(f"/api/admin/booths/{booth_id}/weights/{interest_id}")
    assert removed.status_code == 200, removed.text
    with Session(engine) as db:
        assert db.query(AdminAudit).count() == initial_audits + 12
        assert db.query(PointsLog).count() == initial_points
    # Database partial uniqueness remains authoritative for global and booth presets.
    assert (
        client.post("/api/admin/presets", json={"booth_id": None, "points": 19}).status_code == 409
    )
    assert (
        client.post("/api/admin/presets", json={"booth_id": booth_id, "points": 18}).status_code
        == 409
    )


def test_failed_audit_rolls_back_business_mutation(
    admin_test_client: tuple[TestClient, Engine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, engine = admin_test_client
    _as_role(client, engine, StaffRole.ADMIN)
    real_audit = AdminAudit

    def invalid_audit(**kwargs: Any) -> AdminAudit:
        kwargs["staff_account_id"] = -999999
        return real_audit(**kwargs)

    monkeypatch.setattr(admin_service, "AdminAudit", invalid_audit)
    response = client.post("/api/admin/booths", json={"slug": "audit-must-rollback", "name": "X"})
    assert response.status_code == 400
    with Session(engine) as db:
        assert db.execute(select(Booth).where(Booth.slug == "audit-must-rollback")).first() is None


def test_maintenance_rejects_admin_mutation(admin_test_client: tuple[TestClient, Engine]) -> None:
    client, engine = admin_test_client
    _as_role(client, engine, StaffRole.ADMIN)
    with Session(engine) as db:
        config = db.get(EventConfig, 1)
        assert config is not None
        config.maintenance_mode = True
        db.commit()
    response = client.post("/api/admin/booths", json={"slug": "blocked", "name": "Blocked"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "MAINTENANCE"


def test_admin_mutation_waits_on_reset_fence(
    admin_test_client: tuple[TestClient, Engine],
) -> None:
    client, engine = admin_test_client
    _as_role(client, engine, StaffRole.ADMIN)
    with engine.connect() as owner:
        owner.execute(text("SELECT pg_advisory_lock(:key)"), {"key": FACEMARK_RESET_FENCE})
        owner.commit()
        try:
            response = client.post(
                "/api/admin/booths", json={"slug": "fence-blocked", "name": "Blocked"}
            )
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "MAINTENANCE"
        finally:
            owner.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": FACEMARK_RESET_FENCE})
            owner.commit()
    with Session(engine) as db:
        assert db.execute(select(Booth).where(Booth.slug == "fence-blocked")).first() is None


def test_demo_otp_view_fails_closed_outside_demo(
    admin_test_client: tuple[TestClient, Engine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, engine = admin_test_client
    _as_role(client, engine, StaffRole.ADMIN)
    assert client.get("/api/admin/demo-otps").status_code == 200
    from app.modules.admin import routers as admin_routers

    settings = get_settings().model_copy(update={"demo_mode": False})
    monkeypatch.setattr(admin_routers, "get_settings", lambda: settings)
    assert client.get("/api/admin/demo-otps").status_code == 404


def test_demo_otp_view_shows_delivery_without_changing_hash_storage(
    admin_test_client: tuple[TestClient, Engine],
) -> None:
    from app.modules.attendees.models import OtpChallenge

    client, engine = admin_test_client
    _as_role(client, engine, StaffRole.ADMIN)
    with Session(engine) as db:
        user = db.execute(select(User)).scalars().first()
        assert user is not None
        email = user.email
    issued = client.post("/api/auth/otp/request", json={"email": email})
    assert issued.status_code == 200, issued.text
    visible = client.get("/api/admin/demo-otps")
    assert visible.status_code == 200
    assert visible.headers["Cache-Control"] == "no-store"
    item = next(row for row in visible.json() if row["email"] == email)
    assert len(item["code"]) == 6
    with Session(engine) as db:
        challenge = (
            db.execute(
                select(OtpChallenge)
                .where(OtpChallenge.email == email)
                .order_by(OtpChallenge.id.desc())
            )
            .scalars()
            .first()
        )
        assert challenge is not None
        assert item["code"] not in challenge.code_hash


def test_admin_reads_are_rate_limited(
    admin_test_client: tuple[TestClient, Engine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, engine = admin_test_client
    _as_role(client, engine, StaffRole.ADMIN)
    monkeypatch.setattr(admin_service, "ADMIN_PER_ACCOUNT", RateLimit("admin:test", 1, 60))
    assert client.get("/api/admin/attendees").status_code == 200
    assert client.get("/api/admin/attendees").status_code == 429


def test_admin_routes_are_absent_from_production_openapi(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main as app_main

    production = get_settings().model_copy(update={"environment": "production", "demo_mode": False})
    monkeypatch.setattr(app_main, "get_settings", lambda: production)
    production_app = app_main.create_app()
    assert not any(path.startswith("/api/admin/") for path in production_app.openapi()["paths"])
    assert "/api/admin/reset" not in production_app.openapi()["paths"]
