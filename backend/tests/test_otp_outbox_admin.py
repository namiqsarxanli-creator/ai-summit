"""`GET /api/admin/otp-deliveries`: authority, pagination, and what it
must never reveal. Uses the disposable reset-test database, like the other
admin tests."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.sessions import create_attendee_session, create_staff_session
from app.db.session import get_db
from app.main import app
from app.modules.attendees.models import OtpChallenge, OtpPurpose, User
from app.modules.auth import otp_outbox as ob
from app.modules.staff.models import StaffAccount, StaffRole
from tests.outbox_support import TEST_KEY, outbox_settings

pytestmark = pytest.mark.usefixtures("clean_reset_db")

URL = "/api/admin/otp-deliveries"


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


def _add(
    engine: Engine,
    *,
    email: str | None = None,
    code: str = "314159",
    language: str = "en",
    status: str = "pending",
    reason: str | None = None,
    http_status: int | None = None,
    message_id: str | None = None,
) -> int:
    address = email or f"person-{uuid.uuid4().hex[:8]}@mail.facemark-tests.net"
    with Session(engine) as db:
        challenge = OtpChallenge(
            email=address,
            user_id=None,
            purpose=OtpPurpose.LOGIN,
            code_hash="$argon2id$not-used",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        db.add(challenge)
        db.flush()
        delivery = ob.enqueue(
            db,
            challenge=challenge,
            code=code,
            language=language,
            settings=outbox_settings(otp_outbox_encryption_key=TEST_KEY),
        )
        if status != "pending":
            db.execute(
                text(
                    "UPDATE otp_deliveries SET status = :s, reason_code = :r, "
                    "last_http_status = :h, provider_message_id = :m, "
                    "payload_ciphertext = NULL, payload_key_id = NULL, "
                    "finished_at = now(), accepted_at = CASE WHEN :s = 'accepted' THEN now() END, "
                    "attempt_count = 2 WHERE id = :i"
                ),
                {"s": status, "r": reason, "h": http_status, "m": message_id, "i": delivery.id},
            )
        db.commit()
        return delivery.id


# --- authority -------------------------------------------------------------------


def test_only_an_admin_may_read_delivery_status(admin_client: tuple[TestClient, Engine]) -> None:
    client, engine = admin_client
    _add(engine)

    assert client.get(URL).status_code == 401  # anonymous

    with Session(engine) as db:
        user = db.execute(select(User)).scalars().first()
        assert user is not None
        token = create_attendee_session(db, user_id=user.id, now=datetime.now(UTC))
        db.commit()
    client.cookies.set("session", token)
    assert client.get(URL).status_code == 401  # an attendee session is not staff

    _sign_in(client, engine, StaffRole.BOOTH_STAFF)
    forbidden = client.get(URL)
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "STAFF_NOT_AUTHORIZED"

    _sign_in(client, engine, StaffRole.ADMIN)
    assert client.get(URL).status_code == 200


def test_the_endpoint_is_read_only(admin_client: tuple[TestClient, Engine]) -> None:
    client, engine = admin_client
    _sign_in(client, engine, StaffRole.ADMIN)
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)(URL).status_code in (404, 405)


# --- secrecy ---------------------------------------------------------------------


def test_no_code_ciphertext_key_or_full_address_is_ever_returned(
    admin_client: tuple[TestClient, Engine],
) -> None:
    client, engine = admin_client
    address = "alice.example@mail.facemark-tests.net"
    delivery_id = _add(engine, email=address, code="902817")
    finished = _add(
        engine,
        status="failed",
        reason="provider_auth_rejected",
        http_status=403,
        code="665544",
    )
    with Session(engine) as db:
        stored = db.execute(
            text("SELECT payload_ciphertext, idempotency_key FROM otp_deliveries WHERE id = :i"),
            {"i": delivery_id},
        ).one()
    _sign_in(client, engine, StaffRole.ADMIN)

    response = client.get(URL)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.text
    for secret in ("902817", "665544", address, "alice.example", stored[1], bytes(stored[0]).hex()):
        assert secret not in body, secret
    assert "a***@mail.facemark-tests.net" in body  # masked, still recognisable
    keys: set[str] = set()
    for item in response.json()["items"]:
        keys |= set(item)
    assert keys.isdisjoint(
        {"payload", "payload_ciphertext", "ciphertext", "code", "idempotency_key", "email", "key"}
    )
    assert finished in {i["id"] for i in response.json()["items"]}


def test_failures_carry_a_safe_explanation_and_acceptance_is_not_called_delivery(
    admin_client: tuple[TestClient, Engine],
) -> None:
    client, engine = admin_client
    failed = _add(engine, status="failed", reason="provider_auth_rejected", http_status=403)
    ok = _add(engine, status="accepted", message_id="msg_12345")
    gone = _add(engine, status="expired", reason="challenge_consumed")
    _sign_in(client, engine, StaffRole.ADMIN)

    page = client.get(URL).json()
    items = {i["id"]: i for i in page["items"]}

    assert "API key" in items[failed]["reason"] and items[failed]["http_status"] == 403
    assert items[failed]["reason_code"] == "provider_auth_rejected"
    assert items[ok]["status"] == "accepted" and items[ok]["provider_message_id"] == "msg_12345"
    assert items[ok]["reason"] is None
    assert "already been used" in items[gone]["reason"]
    assert "not that it reached an inbox" in page["note"]
    # nothing in any message is provider text: they come from a fixed table
    assert {i["reason"] for i in items.values() if i["reason"]} <= set(ob.ERROR_MESSAGES.values())


# --- pagination, filters, summary --------------------------------------------------


def test_pagination_walks_every_row_exactly_once_newest_first(
    admin_client: tuple[TestClient, Engine],
) -> None:
    client, engine = admin_client
    created = [_add(engine) for _ in range(7)]
    _sign_in(client, engine, StaffRole.ADMIN)

    seen: list[int] = []
    cursor: int | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 3}
        if cursor is not None:
            params["before_id"] = cursor
        page = client.get(URL, params=params).json()
        pages += 1
        assert len(page["items"]) <= 3
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_before_id"]
        if cursor is None:
            break

    assert seen == sorted(created, reverse=True)
    assert pages == 3


def test_filters_and_summary(admin_client: tuple[TestClient, Engine]) -> None:
    client, engine = admin_client
    target = "find.me@mail.facemark-tests.net"
    _add(engine, email=target)
    _add(engine, status="accepted", message_id="m1")
    _add(engine, status="failed", reason="network_error")
    _sign_in(client, engine, StaffRole.ADMIN)

    only_failed = client.get(URL, params={"status": "failed"}).json()
    assert [i["status"] for i in only_failed["items"]] == ["failed"]
    by_email = client.get(URL, params={"email": " FIND.me@mail.facemark-tests.net "}).json()
    assert [i["email_masked"] for i in by_email["items"]] == ["f***@mail.facemark-tests.net"]
    summary = client.get(URL).json()["summary"]
    assert summary["counts"] == {
        "pending": 1,
        "processing": 0,
        "accepted": 1,
        "failed": 1,
        "expired": 0,
    }
    assert summary["due_now"] == 1 and summary["oldest_due_seconds"] is not None


@pytest.mark.parametrize(
    "params",
    [
        {"status": "delivered"},  # not a state this system has
        {"status": "PENDING"},
        {"limit": 0},
        {"limit": 101},
        {"before_id": 0},
        {"before_id": "abc"},
        {"email": "x" * 400},
    ],
)
def test_bad_query_parameters_are_rejected(
    admin_client: tuple[TestClient, Engine], params: dict[str, Any]
) -> None:
    client, engine = admin_client
    _sign_in(client, engine, StaffRole.ADMIN)
    assert client.get(URL, params=params).status_code == 422


def test_the_response_schema_has_no_field_for_secrets() -> None:
    props = app.openapi()["components"]["schemas"]["OtpDeliveryItem"]["properties"]
    forbidden = {"payload_ciphertext", "payload_key_id", "idempotency_key", "code", "email"}
    assert not forbidden & set(props)
