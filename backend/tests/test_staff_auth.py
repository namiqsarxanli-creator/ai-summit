"""M09 acceptance: staff authentication and booth isolation —
ARCHITECTURE.md §12, §21.1, §21.7, §21.8, IMPLEMENTATION_PLAN.md M09.

Real PostgreSQL throughout. Test accounts and booths are fresh,
uniquely-named, committed rows (mirroring test_matching.py's own booth
fixtures — the rollback `db` fixture cannot be used here because the
running app serves requests through a different connection).
"""

from __future__ import annotations

import inspect
import logging
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text

from app.core.deps import current_admin, current_booth_staff
from app.core.errors import StaffNotAuthorizedError
from app.core.locks import FACEMARK_RESET_FENCE
from app.core.ratelimit import RateLimit
from app.core.security import (
    generate_session_token,
    hash_password,
    hash_session_token,
    verify_secret,
)
from app.db.session import SessionLocal
from app.main import app
from app.modules.staff import service as staff_service
from app.modules.staff.models import StaffAccount, StaffRole
from tests.m04_support import (
    PSYCOPG_URL,
    captured_otp,
    cleanup_email,
    execute,
    fetch_one,
    make_client,
    new_email,
    register_payload,
)

LOGIN = "/api/staff/auth/login"
LOGOUT = "/api/staff/auth/logout"
ME = "/api/staff/me"
PRESETS = "/api/staff/points/presets"
RESOLVE = "/api/staff/scan/resolve"
ATTENDEE_REGISTER = "/api/attendees/register"
ATTENDEE_VERIFY = "/api/auth/otp/verify"
ATTENDEE_ME = "/api/attendees/me"

# A fixed plaintext, hashed once (Argon2id is ~100ms/call) — never a
# real secret, and never asserted to equal anything sensitive.
TEST_PASSWORD = "m09-test-only-staff-password"  # noqa: S105
TEST_PASSWORD_HASH = hash_password(TEST_PASSWORD)

SEEDED_BOOTH_STAFF_EMAIL = "aiworks@staff.facemark.demo"  # M03 fixture
SEEDED_ADMIN_EMAIL = "admin@facemark.demo"


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def _insert_returning_id(sql: str, **params: Any) -> int:
    """A committing INSERT ... RETURNING — `m04_support.execute()`
    commits but returns nothing, and `fetch_one()` does not commit at
    all (only safe for SELECTs)."""
    db = SessionLocal()
    try:
        row: Any = db.execute(text(sql), params).scalar_one()
        db.commit()
        return int(row)
    finally:
        db.close()


# --------------------------------------------------------------------------
# Fixtures — fresh, committed booths and staff accounts
# --------------------------------------------------------------------------


@pytest.fixture
def test_booth() -> Iterator[Callable[..., int]]:
    """Each call creates a fresh, uniquely-slugged, committed booth.
    Cleans up every booth it created, and their preset rows, at
    teardown."""
    booth_ids: list[int] = []

    def make(*, with_own_preset: int | None = None) -> int:
        slug = f"m09-test-booth-{uuid.uuid4().hex[:10]}"
        booth_id = _insert_returning_id(
            "INSERT INTO booths (slug, name) VALUES (:slug, :name) RETURNING id",
            slug=slug,
            name=f"M09 Test Booth {slug[-6:]}",
        )
        booth_ids.append(booth_id)
        if with_own_preset is not None:
            execute(
                "INSERT INTO task_preset_points (booth_id, points, label) VALUES (:b, :p, 'Own')",
                b=booth_id,
                p=with_own_preset,
            )
        return booth_id

    yield make
    for booth_id in booth_ids:
        execute("DELETE FROM task_preset_points WHERE booth_id = :b", b=booth_id)
        execute("DELETE FROM staff_accounts WHERE booth_id = :b", b=booth_id)
        execute("DELETE FROM booths WHERE id = :b", b=booth_id)


@pytest.fixture
def staff_account() -> Iterator[Callable[..., tuple[str, int]]]:
    """Each call creates a fresh, committed staff_accounts row with a
    KNOWN password (`TEST_PASSWORD`). Returns (email, id). Cleans up
    every account and its sessions at teardown."""
    emails: list[str] = []

    def make(
        *,
        role: StaffRole = StaffRole.BOOTH_STAFF,
        booth_id: int | None = None,
        is_active: bool = True,
        failed_login_count: int = 0,
        locked_until: str | None = None,
    ) -> tuple[str, int]:
        email = f"m09-{uuid.uuid4().hex[:10]}@staff.test.facemark.demo"
        emails.append(email)
        staff_id = _insert_returning_id(
            "INSERT INTO staff_accounts "
            "(email, password_hash, full_name, role, booth_id, is_active, "
            " failed_login_count, locked_until) "
            "VALUES (:email, :hash, 'M09 Test Staff', :role, :booth_id, :is_active, "
            " :failed_login_count, :locked_until) RETURNING id",
            email=email,
            hash=TEST_PASSWORD_HASH,
            role=role.value,
            booth_id=booth_id,
            is_active=is_active,
            failed_login_count=failed_login_count,
            locked_until=locked_until,
        )
        return email, staff_id

    yield make
    for email in emails:
        execute(
            "DELETE FROM sessions WHERE staff_account_id = "
            "(SELECT id FROM staff_accounts WHERE email = :e)",
            e=email,
        )
        execute("DELETE FROM staff_accounts WHERE email = :e", e=email)


def _login(client: TestClient, email: str, password: str) -> Response:
    response: Response = client.post(LOGIN, json={"email": email, "password": password})
    return response


# --------------------------------------------------------------------------
# Login: success, generic failures, enumeration resistance
# --------------------------------------------------------------------------


def test_valid_booth_staff_login_sets_a_session_cookie_and_reaches_me(
    test_booth: Callable[..., int], staff_account: Callable[..., tuple[str, int]]
) -> None:
    booth_id = test_booth()
    email, _ = staff_account(booth_id=booth_id)
    with make_client() as c:
        response = _login(c, email, TEST_PASSWORD)
        assert response.status_code == 204
        assert response.text == ""
        raw = c.cookies.get("session")
        assert raw and len(raw) >= 32

        me = c.get(ME)
        assert me.status_code == 200
        body = me.json()
        assert body["role"] == "BOOTH_STAFF"
        assert body["booth_name"] is not None
        c.post(LOGOUT)


def test_admin_login_has_no_booth_name(staff_account: Callable[..., tuple[str, int]]) -> None:
    email, _ = staff_account(role=StaffRole.ADMIN, booth_id=None)
    with make_client() as c:
        assert _login(c, email, TEST_PASSWORD).status_code == 204
        body = c.get(ME).json()
        assert body["role"] == "ADMIN"
        assert body["booth_name"] is None
        c.post(LOGOUT)


def test_wrong_password_is_invalid_credentials(
    test_booth: Callable[..., int], staff_account: Callable[..., tuple[str, int]]
) -> None:
    email, _ = staff_account(booth_id=test_booth())
    with make_client() as c:
        response = _login(c, email, "not-the-password")
        assert _error(response) == (401, "INVALID_CREDENTIALS")
        assert "session" not in response.cookies


def test_unknown_email_and_wrong_password_produce_byte_identical_bodies(
    test_booth: Callable[..., int], staff_account: Callable[..., tuple[str, int]]
) -> None:
    import re

    email, _ = staff_account(booth_id=test_booth())
    with make_client() as c1, make_client() as c2:
        wrong = c1.post(LOGIN, json={"email": email, "password": "not-it"})
        unknown = c2.post(
            LOGIN, json={"email": f"nobody-{uuid.uuid4().hex[:8]}@staff.demo", "password": "x"}
        )

    def normalise(body: str) -> str:
        return re.sub(r'"correlation_id":"[0-9a-f-]{36}"', '"correlation_id":"*"', body)

    assert normalise(wrong.text) == normalise(unknown.text)
    assert wrong.status_code == unknown.status_code == 401


def test_inactive_staff_account_gets_the_same_generic_failure(
    test_booth: Callable[..., int], staff_account: Callable[..., tuple[str, int]]
) -> None:
    """§7.7: "deactivation kills future logins" — treated identically
    to wrong password/unknown email, never a distinguishable response
    (this implementation's extension of §12.1's stated principle)."""
    booth_id = test_booth()
    email, _ = staff_account(booth_id=booth_id, is_active=False)
    with make_client() as c:
        response = _login(c, email, TEST_PASSWORD)  # the CORRECT password
        assert _error(response) == (401, "INVALID_CREDENTIALS")
        assert "session" not in response.cookies
    assert (
        fetch_one(
            "SELECT count(*) FROM sessions WHERE staff_account_id = "
            "(SELECT id FROM staff_accounts WHERE email = :e)",
            e=email,
        )[0]
        == 0
    )


def test_staff_registration_endpoint_does_not_exist() -> None:
    """Acceptance criterion #1: no route creates a staff account from
    an unauthenticated request — checked against the generated OpenAPI
    schema, not just by convention."""
    from app.main import app

    paths = app.openapi()["paths"]
    staff_paths = {p: list(ops) for p, ops in paths.items() if p.startswith("/api/staff")}
    for path, methods in staff_paths.items():
        assert "register" not in path.lower(), (path, methods)
    assert set(staff_paths) == {
        "/api/staff/auth/login",
        "/api/staff/cards/lookup",  # a typed card code -> the card (3 Nov 2026)
        "/api/staff/id-reference",  # booth and staff numbers, names only (organisers)
        "/api/staff/panel-screen-links",  # the hall computer's links (organisers)
        "/api/staff/auth/logout",
        "/api/staff/me",
        "/api/staff/points/presets",
        "/api/staff/tasks",
        # M11: the award mutation — still no route creates a staff
        # account from an unauthenticated request.
        "/api/staff/points/award",
        "/api/staff/scan/resolve",
        # Passwords: change while signed in, and the emailed one-time reset. None of
        # them creates an account, and the reset never reveals whether one exists.
        "/api/staff/auth/password",
        "/api/staff/auth/password-reset/request",
        "/api/staff/auth/password-reset/confirm",
        # AI Playground arrivals: booth-scoped reads and the arrival confirmation.
        # They act on existing bookings only; none creates an account.
        "/api/staff/playground",
        "/api/staff/playground/slots/{slot_id}",
        "/api/staff/playground/bookings/{booking_id}/admit",
        # Verified participation (migration 0020): scopes are granted by an
        # administrator; none of these creates or changes an account.
        "/api/staff/me/scopes",
        # The FACEMARK organiser workspace: a read of what this account may do.
        "/api/staff/organiser",
        "/api/staff/masterclasses/{masterclass_id}/door",
        "/api/staff/playground/slots/{slot_id}/complete",
        "/api/staff/qmeter/identify",
        "/api/staff/qmeter/award",
        # Meet the Mentor hosts (MENTOR_HOST scope per session, migration 0022).
        "/api/staff/mentor-sessions",
        "/api/staff/mentor-sessions/{session_id}",
        "/api/staff/mentor-sessions/{session_id}/complete",
        # The final result: snapshots of the ranking, never an account.
        "/api/staff/final",
        "/api/staff/final/{result_id}",
        "/api/staff/final/{result_id}/publish",
        "/api/staff/final/{result_id}/discard",
        "/api/staff/final/{result_id}/founders.csv",
        # Offline scans and the booth's own report (migrations 0026, phase F).
        "/api/staff/offline-scans",
        "/api/staff/offline-scans/{scan_id}/approve",
        "/api/staff/offline-scans/{scan_id}/reject",
        "/api/staff/booth/report",
        # Points corrections in the organiser panel: an administrator or a Game Master.
        "/api/staff/points-log",
        "/api/staff/grand-final/open",
        "/api/staff/points-log/award",
        "/api/staff/points-log/{entry_id}/reverse",
        "/api/staff/connections/unpaid",
        "/api/staff/connections/reconcile",
    }


def test_login_rejects_any_field_besides_email_and_password() -> None:
    """Invariant #16 — `extra="forbid"`. In particular: no request can
    even EXPRESS a booth_id, staff_id or role at the login boundary."""
    with make_client() as c:
        for extra in ("booth_id", "role", "staff_id", "id"):
            response = c.post(LOGIN, json={"email": "x@example.com", "password": "x", extra: 1})
            assert _error(response) == (422, "VALIDATION_ERROR"), extra


# --------------------------------------------------------------------------
# Brute force (§21.8): persistent lockout, independent of the rate limiter
# --------------------------------------------------------------------------


def test_lockout_arms_at_ten_failures_and_blocks_the_next_attempt_without_checking_password(
    test_booth: Callable[..., int],
    staff_account: Callable[..., tuple[str, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rate limiter's own STAFF_LOGIN_PER_EMAIL bucket (60 per 15
    minutes) is widened here so this test observes ONLY the
    persistent, DB-backed lockout (§21.8), not the separate limiter."""
    from app.modules.staff import routers as staff_routers

    monkeypatch.setattr(
        staff_routers, "STAFF_LOGIN_PER_EMAIL", RateLimit("staff_login:email", 1000, 15 * 60)
    )
    booth_id = test_booth()
    email, _ = staff_account(booth_id=booth_id)

    with make_client() as c:
        for attempt in range(1, 10):
            response = _login(c, email, "wrong")
            assert _error(response) == (401, "INVALID_CREDENTIALS"), attempt

        # 10th failure: password IS evaluated (it is genuinely wrong),
        # so this is still INVALID_CREDENTIALS, and it is what ARMS the
        # lock for every attempt after it.
        tenth = _login(c, email, "wrong")
        assert _error(tenth) == (401, "INVALID_CREDENTIALS")

        row = fetch_one(
            "SELECT failed_login_count, locked_until FROM staff_accounts WHERE email = :e", e=email
        )
        assert row.failed_login_count == 10
        assert row.locked_until is not None

        # 11th attempt: locked. Uses the CORRECT password — if the
        # password were evaluated at all, this would succeed. It must
        # not: proof the password is skipped entirely while locked.
        eleventh = _login(c, email, TEST_PASSWORD)
        assert _error(eleventh) == (429, "ACCOUNT_LOCKED")
        assert "session" not in eleventh.cookies


def test_successful_login_resets_the_failed_login_counter(
    test_booth: Callable[..., int], staff_account: Callable[..., tuple[str, int]]
) -> None:
    booth_id = test_booth()
    email, _ = staff_account(booth_id=booth_id)
    with make_client() as c:
        for _ in range(3):
            assert _error(_login(c, email, "wrong")) == (401, "INVALID_CREDENTIALS")
        row = fetch_one("SELECT failed_login_count FROM staff_accounts WHERE email = :e", e=email)
        assert row.failed_login_count == 3

        success = _login(c, email, TEST_PASSWORD)
        assert success.status_code == 204
        row = fetch_one(
            "SELECT failed_login_count, locked_until FROM staff_accounts WHERE email = :e", e=email
        )
        assert row.failed_login_count == 0
        assert row.locked_until is None
        c.post(LOGOUT)


def test_staff_login_rate_limit_is_separate_from_the_lockout(
    test_booth: Callable[..., int],
    staff_account: Callable[..., tuple[str, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shrinks the limiter's own bucket to prove IT independently
    throttles — mirrors M06's `test_card_reads_have_a_shared_ip_ceiling`
    pattern."""
    from app.modules.staff import routers as staff_routers

    monkeypatch.setattr(
        staff_routers, "STAFF_LOGIN_PER_EMAIL", RateLimit("staff_login:email", 2, 15 * 60)
    )
    booth_id = test_booth()
    email, _ = staff_account(booth_id=booth_id)
    with make_client() as c:
        _login(c, email, "wrong")
        _login(c, email, "wrong")
        third = _login(c, email, TEST_PASSWORD)  # correct — still throttled
        assert _error(third) == (429, "RATE_LIMITED")
        assert int(third.headers["Retry-After"]) >= 1


def test_staff_login_has_a_shared_ip_ceiling(
    test_booth: Callable[..., int],
    staff_account: Callable[..., tuple[str, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.staff import routers as staff_routers

    monkeypatch.setattr(
        staff_routers, "STAFF_LOGIN_PER_IP", RateLimit("staff_login:ip", 2, 15 * 60)
    )
    booth_id = test_booth()
    email_a, _ = staff_account(booth_id=booth_id)
    email_b, _ = staff_account(booth_id=booth_id)
    with make_client() as c:
        _login(c, email_a, "wrong")
        _login(c, email_b, "wrong")
        third = _login(c, email_a, TEST_PASSWORD)
        assert _error(third) == (429, "RATE_LIMITED")


# --------------------------------------------------------------------------
# Unit: hashing / verification — reuses the shared, already-audited
# primitives rather than any bespoke comparison (§21.1, §21.8)
# --------------------------------------------------------------------------


def test_staff_passwords_are_hashed_argon2id_and_verify_via_the_shared_primitive() -> None:
    assert TEST_PASSWORD_HASH.startswith("$argon2id$")
    assert TEST_PASSWORD not in TEST_PASSWORD_HASH
    assert verify_secret(TEST_PASSWORD_HASH, TEST_PASSWORD) is True
    assert verify_secret(TEST_PASSWORD_HASH, "wrong") is False


def test_login_staff_uses_the_shared_verify_secret_not_a_bespoke_comparison() -> None:
    """ "Unit: password verification is constant-time" (M09 test list) —
    provided by `verify_secret`'s Argon2id backend, already relied on
    elsewhere (OTP, M03 seed). Checked by source inspection that
    `login_staff` calls it rather than comparing the password itself
    with `==` (Python's `str.__eq__` is NOT constant-time — it can
    short-circuit on the first differing byte), the same style M07 used
    to prove no randomness/LLM call exists."""
    source = inspect.getsource(staff_service.login_staff)
    assert "verify_secret(staff.password_hash, password)" in source
    assert "password ==" not in source
    assert "== password" not in source


def test_password_is_never_logged_during_login(
    caplog: pytest.LogCaptureFixture,
    test_booth: Callable[..., int],
    staff_account: Callable[..., tuple[str, int]],
) -> None:
    caplog.set_level(logging.DEBUG)
    booth_id = test_booth()
    email, _ = staff_account(booth_id=booth_id)
    with make_client() as c:
        _login(c, email, TEST_PASSWORD)
        _login(c, email, "wrong-one-too")
        c.post(LOGOUT)
    for record in caplog.records:
        message = record.getMessage()
        assert TEST_PASSWORD not in message
        assert "wrong-one-too" not in message


def test_password_hash_never_appears_in_any_response() -> None:
    """The error copy legitimately says the word "password" (e.g.
    "Incorrect email or password.") — what must never appear is the
    ENCODED HASH itself or the submitted plaintext."""
    with make_client() as c:
        response = _login(c, SEEDED_BOOTH_STAFF_EMAIL, "definitely-wrong-m09")
    assert "$argon2id$" not in response.text
    assert "definitely-wrong-m09" not in response.text


# --------------------------------------------------------------------------
# GET /api/staff/me, GET /api/staff/points/presets — booth isolation
# --------------------------------------------------------------------------


def test_presets_resolve_the_callers_own_booth_and_never_another(
    test_booth: Callable[..., int], staff_account: Callable[..., tuple[str, int]]
) -> None:
    """§7.10: a booth with its OWN preset rows overrides the global
    defaults entirely; a staff member for one booth must never see
    another booth's presets."""
    booth_a = test_booth(with_own_preset=42)
    booth_b = test_booth(with_own_preset=99)
    email_a, _ = staff_account(booth_id=booth_a)
    email_b, _ = staff_account(booth_id=booth_b)

    with make_client() as ca, make_client() as cb:
        assert _login(ca, email_a, TEST_PASSWORD).status_code == 204
        assert _login(cb, email_b, TEST_PASSWORD).status_code == 204

        presets_a = ca.get(PRESETS).json()
        presets_b = cb.get(PRESETS).json()
        ca.post(LOGOUT)
        cb.post(LOGOUT)

    assert presets_a == [{"points": 42, "label": "Own"}]
    assert presets_b == [{"points": 99, "label": "Own"}]
    assert presets_a != presets_b


def test_presets_fall_back_to_global_defaults_when_the_booth_has_none(
    test_booth: Callable[..., int], staff_account: Callable[..., tuple[str, int]]
) -> None:
    booth_id = test_booth()  # no with_own_preset -> none of its own
    email, _ = staff_account(booth_id=booth_id)
    with make_client() as c:
        assert _login(c, email, TEST_PASSWORD).status_code == 204
        presets = c.get(PRESETS).json()
        c.post(LOGOUT)
    assert {p["points"] for p in presets} == {5, 10, 15}  # the seeded global defaults (M03)


def test_presets_is_not_reachable_by_admin(staff_account: Callable[..., tuple[str, int]]) -> None:
    """§12.3: presets feed the award buttons; admins cannot award."""
    email, _ = staff_account(role=StaffRole.ADMIN, booth_id=None)
    with make_client() as c:
        assert _login(c, email, TEST_PASSWORD).status_code == 204
        assert _error(c.get(PRESETS)) == (403, "STAFF_NOT_AUTHORIZED")
        c.post(LOGOUT)


def test_client_cannot_choose_a_different_booth_for_presets(
    test_booth: Callable[..., int], staff_account: Callable[..., tuple[str, int]]
) -> None:
    """Query/header injection has no path to authority: the endpoint
    takes no booth parameter of any kind — booth comes only from the
    session (§12.2, §24.3)."""
    booth_a = test_booth(with_own_preset=42)
    booth_b = test_booth(with_own_preset=99)
    email_a, _ = staff_account(booth_id=booth_a)
    with make_client() as c:
        assert _login(c, email_a, TEST_PASSWORD).status_code == 204
        tampered = c.get(f"{PRESETS}?booth_id={booth_b}", headers={"X-Booth-Id": str(booth_b)})
        c.post(LOGOUT)
    assert tampered.json() == [{"points": 42, "label": "Own"}]


# --------------------------------------------------------------------------
# Principal separation and role narrowing (§12.2 layer 4, §12.3)
# --------------------------------------------------------------------------


def test_an_attendee_session_is_rejected_on_every_staff_only_route(
    otp_log: pytest.LogCaptureFixture,
) -> None:
    email = new_email("m09")
    try:
        with make_client() as c:
            assert c.post(ATTENDEE_REGISTER, json=register_payload(email)).status_code == 200
            code = captured_otp(otp_log.records, email)
            assert c.post(ATTENDEE_VERIFY, json={"email": email, "code": code}).status_code == 200
            assert _error(c.get(ME)) == (401, "UNAUTHENTICATED")
            assert _error(c.get(PRESETS)) == (401, "UNAUTHENTICATED")
            assert _error(c.post(RESOLVE, json={"card_token": "x"})) == (401, "UNAUTHENTICATED")
    finally:
        cleanup_email(email)


def test_a_staff_session_is_rejected_on_the_attendee_only_me_route(
    staff_account: Callable[..., tuple[str, int]],
) -> None:
    email, _ = staff_account(role=StaffRole.ADMIN, booth_id=None)
    with make_client() as c:
        assert _login(c, email, TEST_PASSWORD).status_code == 204
        assert _error(c.get(ATTENDEE_ME)) == (401, "UNAUTHENTICATED")
        c.post(LOGOUT)


def test_current_booth_staff_rejects_an_admin_account() -> None:
    admin = StaffAccount(
        email="x@example.com",
        password_hash="x",
        full_name="X",
        role=StaffRole.ADMIN,
        booth_id=None,
        is_active=True,
    )
    with pytest.raises(StaffNotAuthorizedError):
        current_booth_staff(admin)


def test_current_admin_rejects_a_booth_staff_account() -> None:
    staff = StaffAccount(
        email="x@example.com",
        password_hash="x",
        full_name="X",
        role=StaffRole.BOOTH_STAFF,
        booth_id=1,
        is_active=True,
    )
    with pytest.raises(StaffNotAuthorizedError):
        current_admin(staff)


def test_current_booth_staff_passes_through_a_genuine_booth_staff_account() -> None:
    staff = StaffAccount(
        email="x@example.com",
        password_hash="x",
        full_name="X",
        role=StaffRole.BOOTH_STAFF,
        booth_id=1,
        is_active=True,
    )
    assert current_booth_staff(staff) is staff


def test_current_admin_passes_through_a_genuine_admin_account() -> None:
    admin = StaffAccount(
        email="x@example.com",
        password_hash="x",
        full_name="X",
        role=StaffRole.ADMIN,
        booth_id=None,
        is_active=True,
    )
    assert current_admin(admin) is admin


# --------------------------------------------------------------------------
# Session lifecycle — expiry, revocation, token hashing, logout
# --------------------------------------------------------------------------


def test_staff_session_stores_only_the_token_hash_never_the_raw_token(
    test_booth: Callable[..., int], staff_account: Callable[..., tuple[str, int]]
) -> None:
    booth_id = test_booth()
    email, _ = staff_account(booth_id=booth_id)
    with make_client() as c:
        assert _login(c, email, TEST_PASSWORD).status_code == 204
        raw = c.cookies.get("session")
        assert raw is not None

        row = fetch_one(
            "SELECT token_hash, principal_type, staff_account_id, user_id FROM sessions "
            "WHERE staff_account_id = (SELECT id FROM staff_accounts WHERE email = :e)",
            e=email,
        )
        assert row is not None
        assert row.token_hash == hash_session_token(raw)
        assert row.token_hash != raw
        assert raw not in row.token_hash
        assert row.principal_type == "STAFF"
        assert row.user_id is None
        c.post(LOGOUT)


def test_expired_staff_session_is_rejected(
    test_booth: Callable[..., int], staff_account: Callable[..., tuple[str, int]]
) -> None:
    booth_id = test_booth()
    email, staff_id = staff_account(booth_id=booth_id)
    raw = generate_session_token()
    execute(
        "INSERT INTO sessions (token_hash, principal_type, staff_account_id, issued_at, "
        "expires_at) VALUES (:h, 'STAFF', :s, now() - interval '13 hours', "
        "now() - interval '1 hour')",
        h=hash_session_token(raw),
        s=staff_id,
    )
    with make_client() as c:
        c.cookies.set("session", raw)
        assert _error(c.get(ME)) == (401, "UNAUTHENTICATED")


def test_revoked_staff_session_is_rejected(
    test_booth: Callable[..., int], staff_account: Callable[..., tuple[str, int]]
) -> None:
    booth_id = test_booth()
    email, staff_id = staff_account(booth_id=booth_id)
    raw = generate_session_token()
    execute(
        "INSERT INTO sessions (token_hash, principal_type, staff_account_id, issued_at, "
        "expires_at, revoked_at) VALUES (:h, 'STAFF', :s, now(), now() + interval '12 hours', "
        "now())",
        h=hash_session_token(raw),
        s=staff_id,
    )
    with make_client() as c:
        c.cookies.set("session", raw)
        assert _error(c.get(ME)) == (401, "UNAUTHENTICATED")


def test_logout_revokes_server_side_and_the_old_cookie_stops_working(
    test_booth: Callable[..., int], staff_account: Callable[..., tuple[str, int]]
) -> None:
    booth_id = test_booth()
    email, _ = staff_account(booth_id=booth_id)
    with make_client() as c:
        assert _login(c, email, TEST_PASSWORD).status_code == 204
        raw = c.cookies.get("session")
        assert c.get(ME).status_code == 200

        logout_response = c.post(LOGOUT)
        assert logout_response.status_code == 204

        row = fetch_one(
            "SELECT revoked_at FROM sessions WHERE token_hash = :h", h=hash_session_token(raw)
        )
        assert row is not None and row.revoked_at is not None

        stale = make_client()
        try:
            stale.cookies.set("session", raw)
            assert _error(stale.get(ME)) == (401, "UNAUTHENTICATED")
        finally:
            stale.close()


def test_logout_is_a_harmless_no_op_with_no_session() -> None:
    with make_client() as c:
        assert c.post(LOGOUT).status_code == 204


# --------------------------------------------------------------------------
# CSRF (§8.3.1) — reused middleware, not a parallel system
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://evil.example"},
        {"Origin": "null", "Referer": "http://localhost:3000/staff/login"},
    ],
)
def test_staff_login_and_logout_enforce_the_origin_assertion(headers: dict[str, str]) -> None:
    # A raw TestClient, not `make_client()`: the latter always injects
    # a default Origin header, merged UNDER whatever `headers=` gives
    # it, so an empty dict cannot express "no Origin at all" through
    # it. `test_auth_session.py`'s own CSRF tests use this same raw
    # construction for exactly that reason.
    with TestClient(app, headers=headers) as c:
        assert _error(c.post(LOGIN, json={"email": "x@example.com", "password": "x"})) == (
            403,
            "CSRF_REJECTED",
        )
        assert _error(c.post(LOGOUT)) == (403, "CSRF_REJECTED")


# --------------------------------------------------------------------------
# Reset fence (§19.4) — acquired before the first reset-sensitive read
# --------------------------------------------------------------------------


def test_staff_login_is_fenced_before_the_staff_accounts_lookup(
    test_booth: Callable[..., int], staff_account: Callable[..., tuple[str, int]]
) -> None:
    from tests.m04_support import assert_fenced_before_any_read

    booth_id = test_booth()
    email, _ = staff_account(booth_id=booth_id)
    with make_client() as c:
        response = assert_fenced_before_any_read(
            lambda: c.post(LOGIN, json={"email": email, "password": TEST_PASSWORD})
        )
        assert response.status_code == 204
        c.post(LOGOUT)


def test_staff_logout_is_fenced_before_the_sessions_read(
    test_booth: Callable[..., int], staff_account: Callable[..., tuple[str, int]]
) -> None:
    from tests.m04_support import assert_fenced_before_any_read

    booth_id = test_booth()
    email, _ = staff_account(booth_id=booth_id)
    with make_client() as c:
        assert _login(c, email, TEST_PASSWORD).status_code == 204
        response = assert_fenced_before_any_read(lambda: c.post(LOGOUT))
        assert response.status_code == 204


def test_staff_reads_do_not_wait_on_the_reset_fence(
    test_booth: Callable[..., int], staff_account: Callable[..., tuple[str, int]]
) -> None:
    """`me`, `presets` and `scan/resolve` are pure reads (§19.4) —
    they complete while reset holds the EXCLUSIVE fence, never
    queueing behind it."""
    booth_id = test_booth()
    email, _ = staff_account(booth_id=booth_id)
    with make_client() as c:
        assert _login(c, email, TEST_PASSWORD).status_code == 204

        holder = psycopg.connect(PSYCOPG_URL, autocommit=True)
        try:
            with holder.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", (FACEMARK_RESET_FENCE,))
            results: list[int] = []
            worker = threading.Thread(
                target=lambda: results.extend([c.get(ME).status_code, c.get(PRESETS).status_code])
            )
            worker.start()
            worker.join(timeout=5)
            assert results == [200, 200]
        finally:
            with holder.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (FACEMARK_RESET_FENCE,))
            holder.close()
        c.post(LOGOUT)


# --------------------------------------------------------------------------
# M06 scan-resolve through a real M09 session; a full staff golden path
# --------------------------------------------------------------------------


def test_scan_resolve_works_through_a_real_staff_login(
    test_booth: Callable[..., int], staff_account: Callable[..., tuple[str, int]]
) -> None:
    from app.seed import demo_fixtures as fx

    booth_id = test_booth()
    email, _ = staff_account(booth_id=booth_id)
    with make_client() as c:
        assert _login(c, email, TEST_PASSWORD).status_code == 204
        response = c.post(RESOLVE, json={"card_token": fx.ATTENDEES[1].public_token})
        c.post(LOGOUT)
    assert response.status_code == 200
    body = response.json()
    assert body["first_name"] == fx.ATTENDEES[1].first_name
    assert body["last_name"] == fx.ATTENDEES[1].last_name
    assert body["profession"] == fx.ATTENDEES[1].profession
    # M10: resolve now also carries a scan_ref (§13.3) alongside the
    # unchanged M06 identity fields.
    assert set(body) == {"first_name", "last_name", "profession", "scan_ref"}
    assert re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", body["scan_ref"])


def test_admin_can_also_use_scan_resolve(staff_account: Callable[..., tuple[str, int]]) -> None:
    from app.seed import demo_fixtures as fx

    email, _ = staff_account(role=StaffRole.ADMIN, booth_id=None)
    with make_client() as c:
        assert _login(c, email, TEST_PASSWORD).status_code == 204
        response = c.post(RESOLVE, json={"card_token": fx.ATTENDEES[0].public_token})
        c.post(LOGOUT)
    assert response.status_code == 200


def test_the_seeded_demo_booth_staff_account_logs_in_and_resolves_a_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real golden path: the M03-seeded account, not a synthetic
    one — proves the seed's Argon2id hash was produced by the same
    `hash_password` this login path verifies against."""
    import os

    from app.seed import demo_fixtures as fx

    staff_password = os.environ.get("SEED_STAFF_PASSWORD")
    if not staff_password:
        pytest.skip("SEED_STAFF_PASSWORD not set in this environment")

    with make_client() as c:
        login_response = _login(c, SEEDED_BOOTH_STAFF_EMAIL, staff_password)
        assert login_response.status_code == 204
        me = c.get(ME).json()
        owner, generation = me.pop("queue_owner"), me.pop("dataset_generation")
        own = {k: me[k] for k in ("full_name", "role", "booth_name")}
        assert own == {
            "full_name": "Noah Bergström",
            "role": "BOOTH_STAFF",
            "booth_name": "AIWorks",
        }
        assert isinstance(me["staff_id"], int) and isinstance(me["booth_id"], int)
        # The offline-queue label is an opaque keyed hash, never the account id.
        assert len(owner) == 64 and int(owner, 16) >= 0 and isinstance(generation, int)

        resolved = c.post(RESOLVE, json={"card_token": fx.ATTENDEES[0].public_token})
        assert resolved.status_code == 200
        assert resolved.json()["first_name"] == fx.ATTENDEES[0].first_name

        c.post(LOGOUT)
        assert _error(c.get(ME)) == (401, "UNAUTHENTICATED")

    # Leave no trace: reset counters this run may have touched and drop
    # the session, so the seeded fixture is exactly as M03 left it.
    execute(
        "UPDATE staff_accounts SET failed_login_count = 0, locked_until = NULL WHERE email = :e",
        e=SEEDED_BOOTH_STAFF_EMAIL,
    )
    execute(
        "DELETE FROM sessions WHERE staff_account_id = "
        "(SELECT id FROM staff_accounts WHERE email = :e)",
        e=SEEDED_BOOTH_STAFF_EMAIL,
    )


def test_the_seeded_demo_admin_account_logs_in_but_cannot_reach_presets() -> None:
    """§12.3: admin is a real, seeded role boundary — can resolve a
    card, cannot award, and (M09) cannot reach the preset allow-list
    that only exists to feed award buttons."""
    import os

    from app.seed import demo_fixtures as fx

    admin_password = os.environ.get("SEED_ADMIN_PASSWORD")
    if not admin_password:
        pytest.skip("SEED_ADMIN_PASSWORD not set in this environment")

    with make_client() as c:
        assert _login(c, SEEDED_ADMIN_EMAIL, admin_password).status_code == 204
        me = c.get(ME).json()
        assert me["role"] == "ADMIN"
        assert me["booth_name"] is None

        assert c.post(RESOLVE, json={"card_token": fx.ATTENDEES[0].public_token}).status_code == 200
        assert _error(c.get(PRESETS)) == (403, "STAFF_NOT_AUTHORIZED")

        c.post(LOGOUT)

    execute(
        "UPDATE staff_accounts SET failed_login_count = 0, locked_until = NULL WHERE email = :e",
        e=SEEDED_ADMIN_EMAIL,
    )
    execute(
        "DELETE FROM sessions WHERE staff_account_id = "
        "(SELECT id FROM staff_accounts WHERE email = :e)",
        e=SEEDED_ADMIN_EMAIL,
    )


def test_a_shared_account_signs_in_on_many_phones_at_once(
    test_booth: Callable[..., int], staff_account: Callable[..., tuple[str, int]]
) -> None:
    """A team on one account: 30 sign-ins in a row all succeed, and each phone
    keeps its own session (none signs the others out)."""
    from app.core.ratelimit import STAFF_LOGIN_PER_EMAIL

    assert STAFF_LOGIN_PER_EMAIL.limit >= 30
    email, _ = staff_account(booth_id=test_booth())
    phones = [make_client() for _ in range(30)]
    try:
        for phone in phones:
            assert (
                phone.post(LOGIN, json={"email": email, "password": TEST_PASSWORD}).status_code
                == 204
            )
        assert all(p.get("/api/staff/me").status_code == 200 for p in phones)
    finally:
        for phone in phones:
            phone.close()
