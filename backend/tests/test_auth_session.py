"""M05 acceptance: returning-attendee login, session resolution,
logout, CSRF, rate limiting and the reset fence on every M05 mutation —
ARCHITECTURE.md §8.2, §8.3, §8.3.1, §19.4, §21.7, §21.7.1.

Real PostgreSQL throughout; every test owns its rows and removes them.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from httpx import Response

from app.core import deps as deps_module
from app.core import sessions as sessions_module
from app.core.config import Settings
from app.core.locks import FACEMARK_RESET_FENCE
from app.core.ratelimit import limiter
from app.core.security import hash_otp_code, hash_session_token
from app.main import app
from tests.m04_support import (
    PSYCOPG_URL,
    assert_fenced_before_any_read,
    captured_otp,
    execute,
    fetch_all,
    fetch_one,
    make_client,
    new_email,
    register_payload,
)

REGISTER = "/api/attendees/register"
OTP_REQUEST = "/api/auth/otp/request"
VERIFY = "/api/auth/otp/verify"
LOGOUT = "/api/auth/logout"
ME = "/api/attendees/me"

SEEDED_ATTENDEE = "marcus.lindqvist@attendee.facemark.demo"  # M03 fixture, ACTIVE
SEEDED_STAFF = "aiworks@staff.facemark.demo"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def _activate(client: TestClient, otp_log: pytest.LogCaptureFixture, email: str) -> str:
    """Registers and activates `email` (the M04 path); returns the raw
    session token the activation issued."""
    assert client.post(REGISTER, json=register_payload(email)).status_code == 200
    response = client.post(
        VERIFY, json={"email": email, "code": captured_otp(otp_log.records, email)}
    )
    assert response.status_code == 200
    token = response.cookies.get("session")
    assert isinstance(token, str) and token
    return token


def _login(client: TestClient, otp_log: pytest.LogCaptureFixture, email: str) -> Response:
    """The returning-attendee flow: request a LOGIN code, verify it."""
    assert client.post(OTP_REQUEST, json={"email": email}).status_code == 200
    code = captured_otp(otp_log.records, email, purpose="LOGIN")
    response: Response = client.post(VERIFY, json={"email": email, "code": code})
    return response


def _me(client: TestClient, token: str | None) -> Response:
    """Presents exactly `token` (or none): the jar is cleared so nothing
    else rides along."""
    client.cookies.clear()
    headers = {"Cookie": f"session={token}"} if token else {}
    response: Response = client.get(ME, headers=headers)
    return response


def _session_row(token: str) -> Any:
    return fetch_one(
        "SELECT id, user_id, principal_type, issued_at, expires_at, revoked_at "
        "FROM sessions WHERE token_hash = :h",
        h=hash_session_token(token),
    )


def _user_id(email: str) -> int:
    row = fetch_one("SELECT id FROM users WHERE email = :e", e=email.lower())
    assert row is not None
    return int(row[0])


def _challenge(email: str, purpose: str) -> Any:
    return fetch_one(
        "SELECT id, code_hash, attempt_count, consumed_at FROM otp_challenges "
        "WHERE email = :e AND purpose = :p ORDER BY id DESC LIMIT 1",
        e=email.lower(),
        p=purpose,
    )


def _plant_challenge(email: str, user_id: int, purpose: str, code: str) -> None:
    """A live challenge with a KNOWN code, written straight to the
    database — the only way to construct the cross-purpose attack a
    well-behaved server never issues itself."""
    execute(
        "UPDATE otp_challenges SET consumed_at = now() "
        "WHERE email = :e AND purpose = :p AND consumed_at IS NULL",
        e=email,
        p=purpose,
    )
    execute(
        "INSERT INTO otp_challenges (email, user_id, purpose, code_hash, expires_at) "
        "VALUES (:e, :u, :p, :h, now() + interval '10 minutes')",
        e=email,
        u=user_id,
        p=purpose,
        h=hash_otp_code(code),
    )


@pytest.fixture
def seeded_attendee_cleanup() -> Iterator[str]:
    """Logging in as a seeded M03 attendee writes challenges and
    sessions for them; remove exactly those, never the attendee."""
    yield SEEDED_ATTENDEE
    uid = _user_id(SEEDED_ATTENDEE)
    execute("DELETE FROM sessions WHERE user_id = :u", u=uid)
    execute("DELETE FROM otp_challenges WHERE email = :e", e=SEEDED_ATTENDEE)


def test_api_responses_are_never_stored_by_shared_caches(client: TestClient) -> None:
    assert client.get("/api/health").headers["cache-control"] == "no-store"
    assert client.get(ME).headers["cache-control"] == "no-store"
    assert client.get("/api/admin/attendees").headers["cache-control"] == "no-store"


# --------------------------------------------------------------------------
# Returning-attendee login
# --------------------------------------------------------------------------


def test_seeded_active_attendee_can_log_in(
    client: TestClient, otp_log: pytest.LogCaptureFixture, seeded_attendee_cleanup: str
) -> None:
    """The demo's own attendees (M03, seeded ACTIVE with no session) sign
    in through email -> LOGIN code -> verify."""
    response = _login(client, otp_log, seeded_attendee_cleanup)
    assert response.status_code == 200
    token = response.cookies.get("session")
    assert token

    me = _me(client, token)
    assert me.status_code == 200
    assert me.json()["email"] == seeded_attendee_cleanup
    assert me.json()["first_name"] == "Marcus"


def test_login_code_is_a_hashed_login_purpose_challenge(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    _activate(client, otp_log, unique_email)
    client.post(OTP_REQUEST, json={"email": unique_email})
    code = captured_otp(otp_log.records, unique_email, purpose="LOGIN")

    challenge = _challenge(unique_email, "LOGIN")
    assert challenge.code_hash.startswith("$argon2id$")
    assert code not in challenge.code_hash
    assert challenge.consumed_at is None
    # Requesting a code alone signs nobody in.
    assert (
        len(fetch_all("SELECT id FROM sessions WHERE user_id = :u", u=_user_id(unique_email))) == 1
    )


def test_successful_login_creates_a_correct_attendee_session(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    _activate(client, otp_log, unique_email)
    response = _login(client, otp_log, unique_email)
    assert response.status_code == 200
    assert response.json() == {"status": "ACTIVE"}

    token = response.cookies.get("session")
    assert token
    row = _session_row(token)
    assert row.user_id == _user_id(unique_email)
    assert row.principal_type == "ATTENDEE"
    assert row.revoked_at is None
    assert row.expires_at - row.issued_at == timedelta(hours=12)
    assert _challenge(unique_email, "LOGIN").consumed_at is not None

    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert "Max-Age=43200" in cookie


def test_session_token_is_stored_only_as_its_hash(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """§8.3: "store only its SHA-256 hash". The raw token appears in no
    column of the sessions table."""
    token = _login_after_activation(client, otp_log, unique_email)
    rows = fetch_all(
        "SELECT row_to_json(s)::text AS j FROM sessions s WHERE user_id = :u",
        u=_user_id(unique_email),
    )
    assert rows and all(token not in r.j for r in rows)
    assert _session_row(token).id is not None  # found only via its hash
    assert hashlib.sha256(token.encode()).hexdigest() == hash_session_token(token)


def _login_after_activation(
    client: TestClient, otp_log: pytest.LogCaptureFixture, email: str
) -> str:
    _activate(client, otp_log, email)
    token = _login(client, otp_log, email).cookies.get("session")
    assert token
    return token


def test_unknown_email_login_is_indistinguishable(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """§8.2 enumeration resistance: requesting a code for an unknown
    address looks exactly like a registered one and creates nothing;
    verifying against it fails exactly like a wrong code."""
    unknown = new_email()
    _activate(client, otp_log, unique_email)

    r_known = client.post(OTP_REQUEST, json={"email": unique_email})
    r_unknown = client.post(OTP_REQUEST, json={"email": unknown})
    assert r_known.status_code == r_unknown.status_code == 200
    assert r_known.content == r_unknown.content
    assert fetch_all("SELECT id FROM otp_challenges WHERE email = :e", e=unknown) == []
    assert fetch_all("SELECT id FROM users WHERE email = :e", e=unknown) == []

    wrong = client.post(VERIFY, json={"email": unique_email, "code": "000000"})
    ghost = client.post(VERIFY, json={"email": unknown, "code": "000000"})
    assert wrong.content != b"" and ghost.content != b""
    assert (wrong.status_code, wrong.json()["error"]["message"]) == (
        ghost.status_code,
        ghost.json()["error"]["message"],
    )


def test_activation_code_cannot_be_used_as_a_login_code(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """§8.2: "an activation code cannot be replayed as a login code".
    An ACTIVE attendee holding a live ACTIVATION challenge with a known
    code still cannot sign in with it — verification for an ACTIVE
    attendee only ever consults LOGIN challenges."""
    _activate(client, otp_log, unique_email)
    uid = _user_id(unique_email)
    sessions_before = len(fetch_all("SELECT id FROM sessions WHERE user_id = :u", u=uid))
    _plant_challenge(unique_email, uid, "ACTIVATION", "424242")

    response = client.post(VERIFY, json={"email": unique_email, "code": "424242"})
    assert _error(response) == (400, "OTP_INVALID")
    planted = _challenge(unique_email, "ACTIVATION")
    assert planted.consumed_at is None and planted.attempt_count == 0  # never even consulted
    assert len(fetch_all("SELECT id FROM sessions WHERE user_id = :u", u=uid)) == sessions_before


def test_login_code_cannot_activate_a_provisional_attendee(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """...and vice versa: a LOGIN code never activates. Only a genuine
    ACTIVATION code proves email ownership for a PROVISIONAL record."""
    client.post(REGISTER, json=register_payload(unique_email))
    uid = _user_id(unique_email)
    _plant_challenge(unique_email, uid, "LOGIN", "737373")

    response = client.post(VERIFY, json={"email": unique_email, "code": "737373"})
    assert _error(response) == (400, "OTP_INVALID")
    status = fetch_one("SELECT status FROM users WHERE id = :u", u=uid)
    assert status[0] == "PROVISIONAL"
    assert fetch_all("SELECT id FROM sessions WHERE user_id = :u", u=uid) == []


def test_invalid_expired_and_consumed_login_codes_fail(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    _activate(client, otp_log, unique_email)
    client.post(OTP_REQUEST, json={"email": unique_email})
    code = captured_otp(otp_log.records, unique_email, purpose="LOGIN")
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"

    assert _error(client.post(VERIFY, json={"email": unique_email, "code": wrong})) == (
        400,
        "OTP_INVALID",
    )
    assert _challenge(unique_email, "LOGIN").attempt_count == 1

    execute(
        "UPDATE otp_challenges SET expires_at = now() - interval '1 second' "
        "WHERE email = :e AND purpose = 'LOGIN'",
        e=unique_email,
    )
    assert _error(client.post(VERIFY, json={"email": unique_email, "code": code})) == (
        400,
        "OTP_INVALID",
    )

    fresh = _login(client, otp_log, unique_email)  # new code supersedes; succeeds once
    assert fresh.status_code == 200
    reuse_code = captured_otp(otp_log.records, unique_email, purpose="LOGIN")
    assert _error(client.post(VERIFY, json={"email": unique_email, "code": reuse_code})) == (
        400,
        "OTP_INVALID",
    )


def test_login_rolls_back_completely_if_session_creation_fails(
    unique_email: str, otp_log: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_client() as c:
        _activate(c, otp_log, unique_email)
        c.post(OTP_REQUEST, json={"email": unique_email})
    code = captured_otp(otp_log.records, unique_email, purpose="LOGIN")

    def boom() -> str:
        raise RuntimeError("simulated failure while creating the session")

    monkeypatch.setattr(sessions_module, "generate_session_token", boom)
    with make_client(raise_server_exceptions=False) as c:
        response = c.post(VERIFY, json={"email": unique_email, "code": code})

    assert _error(response) == (500, "INTERNAL_ERROR")
    assert "set-cookie" not in response.headers
    assert _challenge(unique_email, "LOGIN").consumed_at is None  # code still usable
    assert (
        len(fetch_all("SELECT id FROM sessions WHERE user_id = :u", u=_user_id(unique_email))) == 1
    )


# --------------------------------------------------------------------------
# Session resolution — GET /api/attendees/me
# --------------------------------------------------------------------------


def test_activation_created_session_resolves_under_the_m05_resolver(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """M04's activation session is a first-class session: nothing about
    M05 invalidates it."""
    token = _activate(client, otp_log, unique_email)
    response = _me(client, token)
    assert response.status_code == 200
    assert response.json() == {
        "first_name": "Grace",
        "last_name": "Hopper",
        "email": unique_email,
        "profession": "Systems Engineer",
    }


def test_me_exposes_nothing_beyond_the_minimal_identity(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    token = _activate(client, otp_log, unique_email)
    body = _me(client, token).text.lower()
    for forbidden in ("phone", "+1-", "consent", "token", "hash", '"id"', "user_id", "status"):
        assert forbidden not in body, forbidden


def test_missing_or_unknown_session_is_unauthenticated(client: TestClient) -> None:
    assert _error(_me(client, None)) == (401, "UNAUTHENTICATED")
    assert _error(_me(client, "not-a-real-token")) == (401, "UNAUTHENTICATED")


def test_expired_session_is_rejected(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    token = _activate(client, otp_log, unique_email)
    execute(
        "UPDATE sessions SET expires_at = now() - interval '1 second' WHERE token_hash = :h",
        h=hash_session_token(token),
    )
    assert _error(_me(client, token)) == (401, "UNAUTHENTICATED")


def test_revoked_session_is_rejected(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    token = _activate(client, otp_log, unique_email)
    execute(
        "UPDATE sessions SET revoked_at = now() WHERE token_hash = :h", h=hash_session_token(token)
    )
    assert _error(_me(client, token)) == (401, "UNAUTHENTICATED")


def test_staff_session_is_not_an_attendee_session(client: TestClient) -> None:
    """Wrong principal type: a perfectly valid STAFF session does not
    authenticate an attendee route."""
    raw = f"staff-{uuid.uuid4().hex}"
    staff_id = fetch_one("SELECT id FROM staff_accounts WHERE email = :e", e=SEEDED_STAFF)[0]
    execute(
        "INSERT INTO sessions "
        "(token_hash, principal_type, staff_account_id, issued_at, expires_at) "
        "VALUES (:h, 'STAFF', :s, now(), now() + interval '12 hours')",
        h=hash_session_token(raw),
        s=staff_id,
    )
    try:
        assert _error(_me(client, raw)) == (401, "UNAUTHENTICATED")
    finally:
        execute("DELETE FROM sessions WHERE token_hash = :h", h=hash_session_token(raw))


def test_session_of_a_non_active_attendee_is_refused(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """§8.1 capability gating: no attendee capability without ACTIVE."""
    token = _activate(client, otp_log, unique_email)
    execute("UPDATE users SET status = 'PROVISIONAL' WHERE email = :e", e=unique_email)
    assert _error(_me(client, token)) == (403, "REGISTRATION_NOT_VERIFIED")


def test_identity_comes_only_from_the_session(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """There is no client-supplied user id anywhere: query parameters or
    headers naming another attendee change nothing."""
    token = _activate(client, otp_log, unique_email)
    client.cookies.clear()
    response = client.get(
        ME,
        params={"user_id": 1, "email": SEEDED_ATTENDEE},
        headers={"Cookie": f"session={token}", "X-User-Id": "1"},
    )
    assert response.json()["email"] == unique_email


def test_read_only_resolution_takes_no_reset_fence(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """§19.4: pure reads are exempt — /me answers while reset holds the
    fence, instead of queueing behind it."""
    token = _activate(client, otp_log, unique_email)
    holder = psycopg.connect(PSYCOPG_URL)
    holder.autocommit = True
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (FACEMARK_RESET_FENCE,))
        started = time.monotonic()
        response = _me(client, token)
        assert response.status_code == 200
        assert time.monotonic() - started < 1.5
    finally:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (FACEMARK_RESET_FENCE,))
        holder.close()


# --------------------------------------------------------------------------
# Logout
# --------------------------------------------------------------------------


def test_logout_revokes_server_side_and_clears_the_cookie(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """IMPLEMENTATION_PLAN.md M05 acceptance #3: "Logout kills the
    session server-side, not just client-side" — the SAME raw token,
    replayed after logout, no longer authenticates."""
    token = _activate(client, otp_log, unique_email)

    client.cookies.clear()
    response = client.post(LOGOUT, headers={"Cookie": f"session={token}"})
    assert response.status_code == 204

    assert _session_row(token).revoked_at is not None
    assert _error(_me(client, token)) == (401, "UNAUTHENTICATED")

    cleared = response.headers["set-cookie"]
    assert cleared.startswith('session=""') or cleared.startswith("session=;")
    assert "Max-Age=0" in cleared
    assert "HttpOnly" in cleared and "Path=/" in cleared


def test_logout_only_ends_the_presented_session(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    first = _activate(client, otp_log, unique_email)
    second = _login(client, otp_log, unique_email).cookies.get("session")
    assert second

    client.cookies.clear()
    client.post(LOGOUT, headers={"Cookie": f"session={first}"})
    assert _error(_me(client, first)) == (401, "UNAUTHENTICATED")
    assert _me(client, second).status_code == 200


def test_logout_without_a_session_is_harmless(client: TestClient) -> None:
    client.cookies.clear()
    response = client.post(LOGOUT)
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_session_cookie_is_secure_when_the_site_is_https(
    client: TestClient,
    unique_email: str,
    otp_log: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§8.3 / §21.12: `Secure` whenever the public site is HTTPS — decided
    by APP_ORIGIN, since the internal proxy hop is plain HTTP either way."""
    https = Settings(_env_file=None, app_origin="https://summit.example.org")
    monkeypatch.setattr(sessions_module, "get_settings", lambda: https)
    client.post(REGISTER, json=register_payload(unique_email))
    response = client.post(
        VERIFY, json={"email": unique_email, "code": captured_otp(otp_log.records, unique_email)}
    )
    assert "Secure" in response.headers["set-cookie"]
    assert "Secure" in client.post(LOGOUT).headers["set-cookie"]


# --------------------------------------------------------------------------
# CSRF — §8.3.1 Origin assertion
# --------------------------------------------------------------------------


_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def _mutating_routes() -> list[tuple[str, str]]:
    """Full paths from the live OpenAPI schema."""
    paths: dict[str, dict[str, Any]] = app.openapi()["paths"]
    return sorted(
        (method.upper(), path)
        for path, operations in paths.items()
        for method in operations
        if method.upper() in _MUTATING
    )


def _count_mutating_api_routes(routes: list[Any]) -> int:
    """Walks the real route tree (this FastAPI version nests included
    routers), so a mutating route hidden from the schema still counts."""
    total = 0
    for route in routes:
        if isinstance(route, APIRoute):
            total += len((route.methods or set()) & _MUTATING)
        nested = getattr(route, "original_router", None)
        if nested is not None:
            total += _count_mutating_api_routes(nested.routes)
    return total


def test_the_mutating_route_inventory_is_what_m16_expects() -> None:
    # M13 adds only reads (GET /api/leaderboard, GET
    # /api/attendees/me/score) — neither is a mutating method, so this
    # inventory is unchanged from M12. M14 adds one more read (GET
    # /api/realtime/stream) — also not a mutating method, so still
    # unchanged.
    assert _count_mutating_api_routes(app.routes) == len(_mutating_routes())
    baseline = [
        # The attendee's own profile photo: removal (the upload is the PUT below).
        ("DELETE", "/api/attendees/me/photo"),
        # M12: attendee feedback submission + automatic reward.
        ("POST", "/api/attendees/feedback"),
        ("POST", "/api/attendees/register"),
        ("POST", "/api/auth/logout"),
        ("POST", "/api/auth/otp/request"),
        ("POST", "/api/auth/otp/verify"),
        ("POST", "/api/staff/auth/login"),
        ("POST", "/api/staff/auth/logout"),
        # Staff passwords: change (signed in) and the emailed reset (public, no oracle).
        ("POST", "/api/staff/auth/password"),
        ("POST", "/api/staff/auth/password-reset/confirm"),
        ("POST", "/api/staff/auth/password-reset/request"),
        # M11: the award mutation itself.
        ("POST", "/api/staff/points/award"),
        # M06: a read, but a POST by definition (§13.1), so it is under
        # the Origin assertion like every other POST.
        ("POST", "/api/staff/scan/resolve"),
        # The attendee's own profile (ARCHITECTURE.md §8.4): photo upload, details and
        # interests, and sponsor consent. Each takes the reset fence first and names no
        # attendee - who is edited is the session's.
        ("PUT", "/api/attendees/me/photo"),
        ("PUT", "/api/attendees/me/profile"),
        ("PUT", "/api/attendees/me/sponsor-consent"),
    ]
    routes = _mutating_routes()
    assert [entry for entry in routes if not entry[1].startswith("/api/admin/")] == baseline
    assert [entry for entry in routes if entry[1].startswith("/api/admin/")] == [
        ("DELETE", "/api/admin/booths/{booth_id}/weights/{interest_id}"),
        ("PATCH", "/api/admin/booths/{booth_id}"),
        ("PATCH", "/api/admin/event-config"),
        ("PATCH", "/api/admin/interests/{interest_id}"),
        ("PATCH", "/api/admin/presets/{preset_id}"),
        ("PATCH", "/api/admin/tasks/{task_id}"),
        ("POST", "/api/admin/booths"),
        # CSV exports: step 1 of 2 (prepare + one-time ticket); the file itself is a GET.
        ("POST", "/api/admin/exports/attendees"),
        ("POST", "/api/admin/exports/points-log"),
        ("POST", "/api/admin/exports/sponsor-contacts"),
        ("POST", "/api/admin/interests"),
        # Points reversal: cancels one AWARD in full (administrators only).
        ("POST", "/api/admin/points-log/{entry_id}/reverse"),
        ("POST", "/api/admin/presets"),
        ("POST", "/api/admin/staff"),
        ("POST", "/api/admin/staff/{staff_id}/deactivate"),
        # An administrator starts a reset EMAIL for a staff member (no password, no code).
        ("POST", "/api/admin/staff/{staff_id}/password-reset"),
        ("POST", "/api/admin/tasks"),
        ("PUT", "/api/admin/booths/{booth_id}/weights/{interest_id}"),
    ]


@pytest.mark.parametrize(
    "headers",
    [
        {},  # neither Origin nor Referer
        {"Origin": "https://evil.example"},
        {"Origin": "null", "Referer": "http://localhost:3000/login"},
        {"Referer": "https://evil.example/page"},
        {"Origin": "http://localhost:3001"},  # same host, wrong port
    ],
)
def test_every_mutating_route_enforces_the_origin_assertion(headers: dict[str, str]) -> None:
    """IMPLEMENTATION_PLAN.md M05 acceptance #4, checked against the
    live route table so a future route cannot quietly skip it."""
    with TestClient(app, headers=headers) as c:
        for method, path in _mutating_routes():
            response = c.request(method, path, json={})
            assert _error(response) == (403, "CSRF_REJECTED"), (method, path, headers)


def test_rejected_request_has_no_side_effects(unique_email: str) -> None:
    """Rejected before the handler runs: no user, no challenge."""
    with TestClient(app, headers={"Origin": "https://evil.example"}) as c:
        response = c.post(REGISTER, json=register_payload(unique_email))
    assert _error(response) == (403, "CSRF_REJECTED")
    assert fetch_all("SELECT id FROM users WHERE email = :e", e=unique_email) == []
    assert fetch_all("SELECT id FROM otp_challenges WHERE email = :e", e=unique_email) == []


def test_foreign_origin_cannot_log_an_attendee_out(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    token = _activate(client, otp_log, unique_email)
    with TestClient(app, headers={"Origin": "https://evil.example"}) as evil:
        response = evil.post(LOGOUT, headers={"Cookie": f"session={token}"})
    assert _error(response) == (403, "CSRF_REJECTED")
    assert _session_row(token).revoked_at is None


def test_referer_fallback_and_same_origin_succeed(unique_email: str) -> None:
    with TestClient(app, headers={"Referer": "http://localhost:3000/login"}) as c:
        assert c.post(OTP_REQUEST, json={"email": unique_email}).status_code == 200
    with make_client() as c:  # the browser case: a matching Origin
        assert c.post(OTP_REQUEST, json={"email": unique_email}).status_code == 200


def test_safe_methods_are_not_subject_to_the_origin_assertion() -> None:
    with TestClient(app) as c:  # no Origin at all
        assert c.get("/api/health").status_code == 200
        assert _error(c.get(ME)) == (401, "UNAUTHENTICATED")  # auth, not CSRF


# --------------------------------------------------------------------------
# Rate limiting — §21.7 (per identity) and §24.26 (venue NAT)
# --------------------------------------------------------------------------


def test_fourth_otp_request_for_one_email_in_15_minutes_is_refused(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """IMPLEMENTATION_PLAN.md M05 acceptance #2 (second half)."""
    _activate(client, otp_log, unique_email)
    # Registering counted against the same per-email issuance bucket (it issues
    # a code too); measure the OTP request limit on its own.
    limiter.reset()
    for _ in range(3):
        assert client.post(OTP_REQUEST, json={"email": unique_email}).status_code == 200
    fourth = client.post(OTP_REQUEST, json={"email": unique_email.upper()})  # same identity
    assert _error(fourth) == (429, "RATE_LIMITED")
    assert int(fourth.headers["retry-after"]) > 0


def test_otp_request_throttle_is_not_a_membership_oracle(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """Registered and unregistered addresses are throttled identically."""
    _activate(client, otp_log, unique_email)
    limiter.reset()  # registration shares the issuance bucket; start both from zero
    unknown = new_email()
    known = [client.post(OTP_REQUEST, json={"email": unique_email}).status_code for _ in range(4)]
    ghost = [client.post(OTP_REQUEST, json={"email": unknown}).status_code for _ in range(4)]
    assert known == ghost == [200, 200, 200, 429]


def test_otp_verify_is_limited_per_email(client: TestClient, unique_email: str) -> None:
    """§21.7: 10 / 15 min per email on top of 5 attempts per challenge —
    requesting fresh codes cannot buy unlimited guesses. Uses an address
    that never registered (and so never verified anything yet): the
    throttle applies to it exactly as to a real one."""
    guess = {"email": unique_email, "code": "000000"}
    statuses = [client.post(VERIFY, json=guess).status_code for _ in range(10)]
    assert statuses == [400] * 10
    assert _error(client.post(VERIFY, json=guess)) == (429, "RATE_LIMITED")


def test_limits_reset_when_the_window_rolls_over(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    for _ in range(3):
        client.post(OTP_REQUEST, json={"email": unique_email})
    assert client.post(OTP_REQUEST, json={"email": unique_email}).status_code == 429
    frozen = limiter.clock()
    limiter.clock = lambda: frozen + 15 * 60
    assert client.post(OTP_REQUEST, json={"email": unique_email}).status_code == 200


def test_venue_nat_200_registrations_from_one_ip_all_succeed() -> None:
    """IMPLEMENTATION_PLAN.md M05 acceptance #2 (first half) and
    integration test: "200 distinct emails registering from one source
    IP all succeed — the regression that per-IP limiting would have
    caused" (§21.7 F07, §24.26)."""
    run = uuid.uuid4().hex[:8]
    emails = [f"nat-{run}-{i:03d}@test.facemark.demo" for i in range(200)]
    try:
        with make_client(client=("203.0.113.10", 51000)) as venue:
            codes = [venue.post(REGISTER, json=register_payload(e)).status_code for e in emails]
        assert codes == [200] * 200
        count = fetch_one(
            "SELECT count(*) FROM users WHERE email LIKE :p", p=f"nat-{run}-%@test.facemark.demo"
        )
        assert count[0] == 200
    finally:
        uids = [
            r[0]
            for r in fetch_all(
                "SELECT id FROM users WHERE email LIKE :p", p=f"nat-{run}-%@test.facemark.demo"
            )
        ]
        if uids:
            execute("DELETE FROM otp_challenges WHERE user_id = ANY(:ids)", ids=uids)
            execute("DELETE FROM consent_audit WHERE user_id = ANY(:ids)", ids=uids)
            execute("DELETE FROM contact_cards WHERE user_id = ANY(:ids)", ids=uids)
            execute("DELETE FROM user_interests WHERE user_id = ANY(:ids)", ids=uids)
            execute("DELETE FROM users WHERE id = ANY(:ids)", ids=uids)


def _recorded_ip_identities(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real_check = limiter.check

    def spy(checks: Any) -> None:
        seen.extend(identity for limit, identity in checks if limit.name.endswith(":ip"))
        real_check(checks)

    monkeypatch.setattr(limiter, "check", spy)
    return seen


def test_spoofed_forwarded_for_from_an_untrusted_peer_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IMPLEMENTATION_PLAN.md M05 acceptance #1. With the default (empty)
    TRUSTED_PROXY_CIDRS every peer is untrusted, so the rate-limit
    identity is the peer — whatever X-Forwarded-For claims."""
    seen = _recorded_ip_identities(monkeypatch)
    with make_client(client=("198.51.100.7", 40000)) as c:
        for spoof in ("1.2.3.4", "5.6.7.8, 9.9.9.9", "not-even-an-ip"):
            c.post(OTP_REQUEST, json={"email": new_email()}, headers={"X-Forwarded-For": spoof})
    assert seen == ["198.51.100.7"] * 3


def test_trusted_proxy_forwarded_for_is_walked_from_the_right(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusting = Settings(_env_file=None, trusted_proxy_cidrs="10.0.0.0/8")
    monkeypatch.setattr(deps_module, "get_settings", lambda: trusting)
    seen = _recorded_ip_identities(monkeypatch)
    with make_client(client=("10.0.0.5", 40000)) as c:  # the proxy
        c.post(
            OTP_REQUEST,
            json={"email": new_email()},
            headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.44, 10.0.0.9"},
        )
    assert seen == ["203.0.113.44"]


# --------------------------------------------------------------------------
# Reset fence — acquired before the first reset-sensitive read (§19.4)
# --------------------------------------------------------------------------


def test_login_otp_issuance_is_fenced_first(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    _activate(client, otp_log, unique_email)
    response = assert_fenced_before_any_read(
        lambda: client.post(OTP_REQUEST, json={"email": unique_email})
    )
    assert response.status_code == 200
    assert _challenge(unique_email, "LOGIN") is not None


def test_login_verification_is_fenced_first(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    _activate(client, otp_log, unique_email)
    client.post(OTP_REQUEST, json={"email": unique_email})
    code = captured_otp(otp_log.records, unique_email, purpose="LOGIN")
    response = assert_fenced_before_any_read(
        lambda: client.post(VERIFY, json={"email": unique_email, "code": code})
    )
    assert response.status_code == 200


def test_logout_is_fenced_first(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    token = _activate(client, otp_log, unique_email)
    client.cookies.clear()
    response = assert_fenced_before_any_read(
        lambda: client.post(LOGOUT, headers={"Cookie": f"session={token}"})
    )
    assert response.status_code == 204
    assert _session_row(token).revoked_at is not None


def test_session_near_expiry_is_still_valid(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """The boundary from the other side of test_expired_session_is_rejected:
    five seconds of life left (by the database clock) still authenticates."""
    token = _activate(client, otp_log, unique_email)
    execute(
        "UPDATE sessions SET expires_at = now() + interval '5 seconds' WHERE token_hash = :h",
        h=hash_session_token(token),
    )
    assert _me(client, token).status_code == 200


@pytest.mark.parametrize("remember", [False, True])
def test_remembered_session_lifetime_and_revocation(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture, remember: bool
) -> None:
    assert client.post(REGISTER, json=register_payload(unique_email)).status_code == 200
    response = client.post(
        VERIFY,
        json={
            "email": unique_email,
            "code": captured_otp(otp_log.records, unique_email),
            "remember_device": remember,
        },
    )
    assert response.status_code == 200
    token = response.cookies.get("session")
    assert token
    row = _session_row(token)
    expected = timedelta(days=30) if remember else timedelta(hours=12)
    assert row.expires_at - row.issued_at == expected
    cookie = response.headers["set-cookie"]
    assert f"Max-Age={int(expected.total_seconds())}" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    # Returning with the persisted cookie bypasses OTP but logout still revokes it.
    assert _me(client, token).status_code == 200
    assert client.post(LOGOUT, headers={"Cookie": f"session={token}"}).status_code == 204
    assert _me(client, token).status_code == 401


def test_remember_device_requires_a_boolean(client: TestClient) -> None:
    response = client.post(
        VERIFY, json={"email": "person@example.com", "code": "123456", "remember_device": "true"}
    )
    assert response.status_code == 422
