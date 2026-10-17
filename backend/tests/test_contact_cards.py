"""M06 acceptance: contact card and dual-purpose QR — ARCHITECTURE.md §9,
§21.7, §21.11, §24.4, §24.19, §24.20, §24.25.

Real PostgreSQL throughout. Attendees are created through the real M04
registration/activation path (which commits), so each test owns unique
emails and cleans up after itself. Staff sessions are planted directly:
staff login is M09, and M06 only needs a session to exist.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from functools import partial
from typing import Any
from urllib.parse import quote, urlsplit

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import event

from app.core.config import get_settings
from app.core.errors import InvalidQrError
from app.core.locks import FACEMARK_RESET_FENCE
from app.core.privacy import CardTokenLogFilter, mask_card_tokens
from app.core.ratelimit import RateLimit, limiter
from app.core.security import generate_card_token, generate_session_token, hash_session_token
from app.db.session import engine
from app.main import app
from app.modules.contact_cards import routers as card_routers
from app.modules.contact_cards.service import contact_action_for
from app.modules.staff import routers as staff_routers
from app.seed import demo_fixtures as fx
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

REGISTER = "/api/attendees/register"
VERIFY = "/api/auth/otp/verify"
MY_CARD = "/api/attendees/me/card"
RESOLVE = "/api/staff/scan/resolve"

SEEDED_BOOTH_STAFF = "aiworks@staff.facemark.demo"
SEEDED_ADMIN = "admin@facemark.demo"

TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")
PUBLIC_CARD_KEYS = {
    "first_name",
    "last_name",
    "profession",
    "interests",
    "contact",
    "photo_url",  # the profile photo the attendee chose to show; None without one
}
PRIVACY_HEADERS = {
    "referrer-policy": "no-referrer",
    "x-robots-tag": "noindex",
    "cache-control": "no-store",
}

# --------------------------------------------------------------------------
# Helpers and fixtures
# --------------------------------------------------------------------------


def card_path(token: str) -> str:
    # Percent-encode everything a path segment cannot carry, but keep
    # "/" so the "malformed token containing a slash" case is real.
    return "/api/cards/" + quote(token, safe="/")


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def _token_of(email: str) -> str:
    row = fetch_one(
        "SELECT c.public_token FROM contact_cards c JOIN users u ON u.id = c.user_id "
        "WHERE u.email = :e AND c.revoked_at IS NULL",
        e=email.lower(),
    )
    assert row is not None
    return str(row[0])


def _user_id(email: str) -> int:
    row = fetch_one("SELECT id FROM users WHERE email = :e", e=email.lower())
    assert row is not None
    return int(row[0])


@pytest.fixture
def emails() -> Iterator[Callable[[], str]]:
    """Hands out fresh addresses; every one is cleaned up afterwards."""
    issued: list[str] = []

    def issue() -> str:
        email = new_email("m06")
        issued.append(email)
        return email

    yield issue
    for email in issued:
        cleanup_email(email)


@pytest.fixture
def attendee(
    otp_log: pytest.LogCaptureFixture,
) -> Callable[..., tuple[str, str, TestClient]]:
    """Registers and ACTIVATES an attendee through the real M04 path.
    Returns (email, card token, a client holding that attendee's
    session). Cleanup belongs to whoever issued the email."""
    clients: list[TestClient] = []

    def make(email: str, **overrides: Any) -> tuple[str, str, TestClient]:
        c = make_client()
        clients.append(c)
        assert c.post(REGISTER, json=register_payload(email, **overrides)).status_code == 200
        code = captured_otp(otp_log.records, email)
        assert c.post(VERIFY, json={"email": email, "code": code}).status_code == 200
        return email, _token_of(email), c

    return make


@pytest.fixture
def anonymous() -> Iterator[TestClient]:
    """A second browser: no cookies, never signed in."""
    with make_client() as c:
        yield c


@pytest.fixture
def staff_session() -> Iterator[Callable[..., str]]:
    """Plants a STAFF session row (M09 will create these by logging in)
    and returns the raw cookie value."""
    hashes: list[str] = []

    def plant(staff_email: str, *, expired: bool = False, revoked: bool = False) -> str:
        raw = generate_session_token()
        hashes.append(hash_session_token(raw))
        execute(
            "INSERT INTO sessions (token_hash, principal_type, staff_account_id, issued_at, "
            "expires_at, revoked_at) "
            "SELECT :h, 'STAFF', id, now() - interval '1 hour', "
            "CASE WHEN :expired THEN now() - interval '1 minute' "
            "ELSE now() + interval '12 hours' END, "
            "CASE WHEN :revoked THEN now() ELSE NULL END "
            "FROM staff_accounts WHERE email = :e",
            h=hashes[-1],
            e=staff_email,
            expired=expired,
            revoked=revoked,
        )
        return raw

    yield plant
    for h in hashes:
        execute("DELETE FROM sessions WHERE token_hash = :h", h=h)


@pytest.fixture
def inactive_staff_email() -> Iterator[str]:
    email = f"m06-inactive-{uuid.uuid4().hex[:10]}@staff.test.facemark.demo"
    execute(
        "INSERT INTO staff_accounts (email, password_hash, full_name, role, is_active) "
        "VALUES (:e, 'not-a-real-hash', 'Inactive Admin', 'ADMIN', false)",
        e=email,
    )
    yield email
    execute(
        "DELETE FROM sessions WHERE staff_account_id = "
        "(SELECT id FROM staff_accounts WHERE email = :e)",
        e=email,
    )
    execute("DELETE FROM staff_accounts WHERE email = :e", e=email)


def _resolve(token: str, session: str | None, **body: Any) -> Response:
    with make_client() as c:
        if session:
            c.cookies.set("session", session)
        response: Response = c.post(RESOLVE, json={"card_token": token, **body})
        return response


# --------------------------------------------------------------------------
# Unit: token issuance (§9.1)
# --------------------------------------------------------------------------


def test_card_token_is_43_urlsafe_chars_encoding_256_bits() -> None:
    for _ in range(200):
        token = generate_card_token()
        assert TOKEN_SHAPE.fullmatch(token)
        assert len(base64.urlsafe_b64decode(token + "=")) == 32  # 256 bits


def test_card_token_takes_no_input_so_nothing_can_be_derived_from() -> None:
    """§9.1: "there is no function from attendee data to token"."""
    import inspect

    assert inspect.signature(generate_card_token).parameters == {}


def test_card_tokens_are_unique_and_uncorrelated_with_user_id() -> None:
    """IMPLEMENTATION_PLAN.md M06 unit test. Tokens issued to users with
    ids 1..N in order: their order is unrelated to the ids (Spearman
    rank correlation ~ 0; the bound is > 4 sigma for N = 4000), and the
    bits are balanced as uniform CSPRNG output must be."""
    n = 4000
    tokens = [generate_card_token() for _ in range(n)]
    assert len(set(tokens)) == n

    ranks = {t: r for r, t in enumerate(sorted(tokens))}
    d2 = sum((i - ranks[t]) ** 2 for i, t in enumerate(tokens))
    rho = 1 - 6 * d2 / (n * (n * n - 1))
    assert abs(rho) < 0.07, rho

    ones = sum(bin(b).count("1") for t in tokens for b in base64.urlsafe_b64decode(t + "="))
    assert abs(ones / (n * 256) - 0.5) < 0.005


def test_demo_fixture_tokens_share_the_runtime_format() -> None:
    """§19.8 fixed tokens, in §9.1's format — stable AND conformant."""
    for a in fx.ATTENDEES:
        assert TOKEN_SHAPE.fullmatch(a.public_token)
        assert len(base64.urlsafe_b64decode(a.public_token + "=")) == 32


# --------------------------------------------------------------------------
# Unit: contact-action selection (§9.3.1)
# --------------------------------------------------------------------------


def test_linkedin_present_selects_linkedin_and_carries_no_email() -> None:
    action = contact_action_for(
        linkedin_url="https://www.linkedin.com/in/ada", email="ada@example.com"
    )
    assert action.model_dump() == {"type": "LINKEDIN", "value": "https://www.linkedin.com/in/ada"}
    assert "ada@example.com" not in action.model_dump_json()


@pytest.mark.parametrize("linkedin_url", [None, "", "   "])
def test_linkedin_absent_falls_back_to_email(linkedin_url: str | None) -> None:
    action = contact_action_for(linkedin_url=linkedin_url, email="ada@example.com")
    assert action.model_dump() == {"type": "EMAIL", "value": "ada@example.com"}


@pytest.mark.parametrize(
    "linkedin_url",
    ["https://linkedin.com.evil.example/in/ada", "https://linkedin.com@evil.example/in/ada"],
)
def test_legacy_lookalike_linkedin_url_falls_back_to_email(linkedin_url: str) -> None:
    action = contact_action_for(linkedin_url=linkedin_url, email="ada@example.com")
    assert action.model_dump() == {"type": "EMAIL", "value": "ada@example.com"}


# --------------------------------------------------------------------------
# Public card — GET /api/cards/{token}
# --------------------------------------------------------------------------


def test_public_card_linkedin_case_without_authentication(
    emails: Callable[[], str], attendee: Callable[..., Any], anonymous: TestClient
) -> None:
    email, token, _ = attendee(emails(), sponsor_consent=True)
    response = anonymous.get(card_path(token))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == PUBLIC_CARD_KEYS
    assert body["first_name"] == "Grace"
    assert body["last_name"] == "Hopper"
    assert body["profession"] == "Systems Engineer"
    assert body["interests"] == [
        {"slug": "generative-ai", "name": "Generative AI"},
        {"slug": "data-analytics", "name": "Data Analytics"},
    ]
    # Beyond Now contact gate (migration 0021): no contact for an anonymous viewer.
    assert body["contact"] is None


def _connect(a_email: str, b_email: str) -> None:
    low, high = sorted((_user_id(a_email), _user_id(b_email)))
    execute(
        "INSERT INTO connections (user_low_id, user_high_id, kind, same_company, established_at) "
        "VALUES (:l, :h, 'OTHER', false, now())",
        l=low,
        h=high,
    )


def test_the_contact_opens_only_to_a_connected_signed_in_viewer(
    emails: Callable[[], str], attendee: Callable[..., Any], anonymous: TestClient
) -> None:
    """The server-chosen action (LinkedIn first, e-mail as fallback) is shown
    only after the mutual QR scan; another signed-in attendee gets nothing."""
    owner, token, _ = attendee(emails(), sponsor_consent=True)
    viewer, _, viewer_client = attendee(emails(), linkedin_url=None)
    stranger_email, _, stranger_client = attendee(emails(), linkedin_url=None)
    assert viewer_client.get(card_path(token)).json()["contact"] is None
    _connect(owner, viewer)
    assert viewer_client.get(card_path(token)).json()["contact"] == {
        "type": "LINKEDIN",
        "value": "https://www.linkedin.com/in/grace-hopper-demo",
    }
    assert stranger_client.get(card_path(token)).json()["contact"] is None
    assert anonymous.get(card_path(token)).json()["contact"] is None
    del stranger_email


def test_public_card_response_leaks_nothing_private(
    emails: Callable[[], str], attendee: Callable[..., Any], anonymous: TestClient
) -> None:
    """IMPLEMENTATION_PLAN.md M06: "public card response contains no
    phone, no sponsor_consent, no user_id, no email when LinkedIn
    exists." Checked on the raw bytes, not just the parsed keys."""
    email, token, _ = attendee(emails(), sponsor_consent=True)
    text = anonymous.get(card_path(token)).text
    for leaked in (
        "phone",
        "+1-555-0142",
        "consent",
        "user_id",
        '"id"',
        email,
        "email",
        str(_user_id(email)),
        token,
        "session",
        "status",
    ):
        assert leaked not in text, leaked


def test_public_card_email_fallback_case(
    emails: Callable[[], str], attendee: Callable[..., Any], anonymous: TestClient
) -> None:
    email, token, _ = attendee(emails(), linkedin_url=None)
    viewer, _, viewer_client = attendee(emails(), linkedin_url=None)
    assert anonymous.get(card_path(token)).json()["contact"] is None
    _connect(email, viewer)
    body = viewer_client.get(card_path(token)).json()
    assert body["contact"] == {"type": "EMAIL", "value": email}
    assert set(body) == PUBLIC_CARD_KEYS
    assert "phone" not in body and "+1-555-0142" not in str(body)


@pytest.mark.parametrize("a", fx.ATTENDEES, ids=lambda a: a.first_name)
def test_every_fixed_demo_token_resolves_to_its_attendee(
    a: fx.AttendeeFixture, anonymous: TestClient
) -> None:
    """§19.8: the fixed demo tokens are live, scannable cards. Seeded
    attendees have no LinkedIn, so they exercise the email fallback."""
    body = anonymous.get(card_path(a.public_token)).json()
    assert (body["first_name"], body["last_name"], body["profession"]) == (
        a.first_name,
        a.last_name,
        a.profession,
    )
    assert {i["slug"] for i in body["interests"]} == set(a.interest_slugs)
    assert body["contact"] is None  # the contact gate: anonymous sees none
    assert a.phone not in str(body)


def test_card_responses_carry_the_privacy_headers(anonymous: TestClient) -> None:
    """§21.11 / §9.5, on success AND on the INVALID_QR path."""
    for token in (fx.ATTENDEES[0].public_token, generate_card_token()):
        response = anonymous.get(card_path(token))
        for name, value in PRIVACY_HEADERS.items():
            assert response.headers.get(name) == value, (name, response.status_code)


def test_other_routes_keep_the_default_referrer_behaviour(anonymous: TestClient) -> None:
    assert "referrer-policy" not in anonymous.get("/api/health").headers


def test_revoked_token_stops_resolving_and_a_new_live_card_does(
    emails: Callable[[], str], attendee: Callable[..., Any], anonymous: TestClient
) -> None:
    """§9.6 is schema-ready, not built: rotation is simulated here with
    SQL exactly as §9.6 describes it (revoke the row, insert a fresh one,
    bump version). Resolution needs no rotation code to honour it."""
    email, old_token, _ = attendee(emails())
    new_token = generate_card_token()
    execute("UPDATE contact_cards SET revoked_at = now() WHERE public_token = :t", t=old_token)
    execute(
        "INSERT INTO contact_cards (user_id, public_token, issued_at, version) "
        "VALUES (:u, :t, now(), 2)",
        u=_user_id(email),
        t=new_token,
    )
    assert _error(anonymous.get(card_path(old_token))) == (404, "INVALID_QR")
    assert anonymous.get(card_path(new_token)).json()["first_name"] == "Grace"


# --------------------------------------------------------------------------
# §9.4 — five invalid cases, one response
# --------------------------------------------------------------------------


@pytest.fixture
def invalid_tokens(
    emails: Callable[[], str],
    attendee: Callable[..., Any],
    otp_log: pytest.LogCaptureFixture,
) -> dict[str, str]:
    """One token per §9.4 case."""
    _, revoked, _ = attendee(emails())
    execute("UPDATE contact_cards SET revoked_at = now() WHERE public_token = :t", t=revoked)

    provisional_email = emails()
    with make_client() as c:
        assert c.post(REGISTER, json=register_payload(provisional_email)).status_code == 200
    provisional = _token_of(provisional_email)

    deleted_email, deleted, _ = attendee(emails())
    cleanup_email(deleted_email)  # the attendee and every child row are gone

    return {
        "malformed": "not a token/with a slash & spaces",
        "unknown": generate_card_token(),
        "revoked": revoked,
        "provisional": provisional,
        "deleted": deleted,
    }


def _normalised(response: Response) -> tuple[int, bytes, dict[str, str]]:
    body = re.sub(rb'"correlation_id":"[0-9a-f-]{36}"', b'"correlation_id":"*"', response.content)
    headers = {
        k: v
        for k, v in response.headers.items()
        if k in {"content-type", "content-length", *PRIVACY_HEADERS}
    }
    return response.status_code, body, headers


def _capture_statements(call: Callable[[], Response]) -> list[str]:
    seen: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        call()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return seen


def test_five_invalid_public_cases_are_byte_identical(
    invalid_tokens: dict[str, str], anonymous: TestClient
) -> None:
    """IMPLEMENTATION_PLAN.md M06: "all five invalid-token cases produce
    byte-identical 404 INVALID_QR". The only per-response difference is
    §20.2's `correlation_id`, which is unique to every response by
    design; it is normalised out and everything else compared as bytes."""
    responses = {case: anonymous.get(card_path(t)) for case, t in invalid_tokens.items()}
    shapes = {case: _normalised(r) for case, r in responses.items()}
    assert len(set(map(repr, shapes.values()))) == 1, shapes
    status, _, _ = shapes["unknown"]
    assert status == 404
    error = responses["unknown"].json()["error"]
    assert error["code"] == "INVALID_QR"
    assert error["message"] == InvalidQrError.MESSAGE
    assert error["details"] == {}


def test_five_invalid_public_cases_run_the_identical_queries(
    invalid_tokens: dict[str, str], anonymous: TestClient
) -> None:
    """M06 acceptance #3's "timing class": every failure executes the
    same SQL, the same number of times — no case short-circuits early or
    does extra work that a stopwatch could tell apart."""
    traces = {
        case: _capture_statements(partial(anonymous.get, card_path(t)))
        for case, t in invalid_tokens.items()
    }
    assert len({tuple(trace) for trace in traces.values()}) == 1, traces
    # M14: `DatasetGenerationHeaderMiddleware` runs one extra, identical
    # `SELECT` on every request regardless of outcome (§19.3, "every
    # API response carries X-Dataset-Generation") — it is counted here
    # too, since it is part of the same identical-across-cases trace
    # the assertion above already confirmed.
    assert len(traces["unknown"]) == 2


def test_a_provisional_attendees_token_resolves_on_neither_endpoint(
    invalid_tokens: dict[str, str], anonymous: TestClient, staff_session: Callable[..., str]
) -> None:
    """IMPLEMENTATION_PLAN.md M06 / invariant #25: inert until ACTIVE."""
    token = invalid_tokens["provisional"]
    assert _error(anonymous.get(card_path(token))) == (404, "INVALID_QR")
    assert _error(_resolve(token, staff_session(SEEDED_BOOTH_STAFF))) == (404, "INVALID_QR")


def test_activation_is_what_makes_a_card_resolvable(
    emails: Callable[[], str], otp_log: pytest.LogCaptureFixture, anonymous: TestClient
) -> None:
    email = emails()
    with make_client() as c:
        c.post(REGISTER, json=register_payload(email))
        token = _token_of(email)
        assert _error(anonymous.get(card_path(token))) == (404, "INVALID_QR")
        code = captured_otp(otp_log.records, email)
        assert c.post(VERIFY, json={"email": email, "code": code}).status_code == 200
    # The very same token — nothing was reissued at activation.
    assert _token_of(email) == token
    assert anonymous.get(card_path(token)).status_code == 200


# --------------------------------------------------------------------------
# Rate limiting (§21.7) — before any database work
# --------------------------------------------------------------------------


def test_card_reads_are_limited_per_token(anonymous: TestClient) -> None:
    token = fx.ATTENDEES[1].public_token
    for _ in range(30):
        assert anonymous.get(card_path(token)).status_code == 200
    limited = anonymous.get(card_path(token))
    assert _error(limited) == (429, "RATE_LIMITED")
    assert int(limited.headers["Retry-After"]) >= 1
    for name, value in PRIVACY_HEADERS.items():
        assert limited.headers.get(name) == value
    # A different card is its own bucket: one popular QR throttles no one else.
    assert anonymous.get(card_path(fx.ATTENDEES[2].public_token)).status_code == 200


def test_card_reads_have_a_shared_ip_ceiling(
    anonymous: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real ceiling is 1200/min; shrunk here to prove it is wired."""
    monkeypatch.setattr(card_routers, "CARD_READ_PER_IP", RateLimit("card_read:ip", 3, 60))
    for _ in range(3):
        anonymous.get(card_path(generate_card_token()))
    assert _error(anonymous.get(card_path(fx.ATTENDEES[0].public_token))) == (
        429,
        "RATE_LIMITED",
    )


def test_the_limiter_never_holds_a_raw_card_token(anonymous: TestClient) -> None:
    token = fx.ATTENDEES[0].public_token
    anonymous.get(card_path(token))
    keys = repr(limiter.store._counts)  # type: ignore[attr-defined]
    assert "card_read:token:" in keys
    assert token not in keys


def test_scan_resolve_is_limited_per_staff_account(
    staff_session: Callable[..., str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        staff_routers, "SCAN_RESOLVE_PER_STAFF", RateLimit("scan_resolve:staff", 2, 60)
    )
    booth_staff = staff_session(SEEDED_BOOTH_STAFF)
    token = fx.ATTENDEES[0].public_token
    assert _resolve(token, booth_staff).status_code == 200
    assert _resolve(token, booth_staff).status_code == 200
    assert _error(_resolve(token, booth_staff)) == (429, "RATE_LIMITED")
    # Another staff account is unaffected.
    assert _resolve(token, staff_session(SEEDED_ADMIN)).status_code == 200


# --------------------------------------------------------------------------
# Staff resolution — POST /api/staff/scan/resolve (§9.3, §24.20)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("staff_email", [SEEDED_BOOTH_STAFF, SEEDED_ADMIN])
def test_staff_resolve_returns_exactly_three_identity_fields_plus_scan_ref(
    staff_email: str,
    emails: Callable[[], str],
    attendee: Callable[..., Any],
    staff_session: Callable[..., str],
) -> None:
    """IMPLEMENTATION_PLAN.md M06 / §12.3 (booth staff ✅, admin ✅), M10
    (§13.3: resolve gains `scan_ref`). The attendee has LinkedIn, consent
    and a phone — none of it reaches staff."""
    email, token, _ = attendee(emails(), sponsor_consent=True)
    response = _resolve(token, staff_session(staff_email))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"first_name", "last_name", "profession", "scan_ref"}
    assert body["first_name"] == "Grace"
    assert body["last_name"] == "Hopper"
    assert body["profession"] == "Systems Engineer"
    assert re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", body["scan_ref"])
    for leaked in ("linkedin", email, "+1-555-0142", "consent", "interests"):
        assert leaked not in response.text


def test_staff_resolve_is_not_widened_by_the_public_card() -> None:
    from app.modules.contact_cards.schemas import CardIdentity, PublicCard

    assert set(CardIdentity.model_fields) == {"first_name", "last_name", "profession"}
    assert not issubclass(CardIdentity, PublicCard)
    assert not issubclass(PublicCard, CardIdentity)


def test_five_invalid_staff_cases_are_byte_identical(
    invalid_tokens: dict[str, str], staff_session: Callable[..., str]
) -> None:
    session = staff_session(SEEDED_BOOTH_STAFF)
    shapes = {case: _normalised(_resolve(t, session)) for case, t in invalid_tokens.items()}
    assert len(set(map(repr, shapes.values()))) == 1, shapes
    assert shapes["unknown"][0] == 404
    assert b'"code":"INVALID_QR"' in shapes["unknown"][1]


@pytest.mark.parametrize("kind", ["none", "expired", "revoked", "attendee"])
def test_staff_resolve_requires_a_live_staff_session(
    kind: str,
    staff_session: Callable[..., str],
    emails: Callable[[], str],
    attendee: Callable[..., Any],
) -> None:
    session: str | None
    if kind == "none":
        session = None
    elif kind == "attendee":
        _, _, attendee_client = attendee(emails())
        session = attendee_client.cookies.get("session")
    else:
        session = staff_session(SEEDED_BOOTH_STAFF, **{kind: True})
    response = _resolve(fx.ATTENDEES[0].public_token, session)
    assert _error(response) == (401, "UNAUTHENTICATED")
    assert "Marcus" not in response.text and "Elena" not in response.text


def test_staff_resolve_rejects_an_inactive_staff_account(
    staff_session: Callable[..., str], inactive_staff_email: str
) -> None:
    response = _resolve(fx.ATTENDEES[0].public_token, staff_session(inactive_staff_email))
    assert _error(response) == (403, "STAFF_NOT_AUTHORIZED")


def test_staff_resolve_rejects_unexpected_fields(staff_session: Callable[..., str]) -> None:
    """Invariant #16 — nothing but the token can be sent."""
    response = _resolve(fx.ATTENDEES[0].public_token, staff_session(SEEDED_BOOTH_STAFF), booth_id=1)
    assert _error(response) == (422, "VALIDATION_ERROR")


def test_staff_resolve_is_under_the_origin_assertion(staff_session: Callable[..., str]) -> None:
    with make_client(headers={"Origin": "https://evil.example"}) as c:
        c.cookies.set("session", staff_session(SEEDED_BOOTH_STAFF))
        response = c.post(RESOLVE, json={"card_token": fx.ATTENDEES[0].public_token})
    assert _error(response) == (403, "CSRF_REJECTED")


# --------------------------------------------------------------------------
# The attendee's own card — GET /api/attendees/me/card
# --------------------------------------------------------------------------


def test_my_card_is_the_session_attendees_card_and_its_qr_url(
    emails: Callable[[], str], attendee: Callable[..., Any], anonymous: TestClient
) -> None:
    email, token, client = attendee(emails())
    response = client.get(MY_CARD)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"card_url", "card"}
    assert body["card_url"] == f"{get_settings().app_origin}/c/{token}"
    # The preview is exactly what a scanner of the QR sees.
    assert body["card"] == anonymous.get(card_path(token)).json()
    for name, value in PRIVACY_HEADERS.items():
        assert response.headers.get(name) == value


def test_qr_payload_is_only_origin_plus_c_plus_opaque_token(
    emails: Callable[[], str], attendee: Callable[..., Any]
) -> None:
    """§9.2 / §24.4: no name, email, phone, interests, JSON or id."""
    email, token, client = attendee(emails(), first_name="Ada", last_name="Lovelace")
    card_url = client.get(MY_CARD).json()["card_url"]
    parts = urlsplit(card_url)
    assert f"{parts.scheme}://{parts.netloc}" == get_settings().app_origin
    assert parts.path == f"/c/{token}"
    assert parts.query == "" and parts.fragment == ""
    assert TOKEN_SHAPE.fullmatch(token)
    # Everything in the payload that is not the random token is fixed
    # text; nothing about the attendee or their session is in it.
    session = client.cookies.get("session")
    assert session and session not in card_url
    assert card_url.replace(token, "") == f"{get_settings().app_origin}/c/"
    for secret in (email, "Ada", "Lovelace", "+1-555-0142"):
        assert secret not in get_settings().app_origin


def test_my_card_ignores_any_client_supplied_identity(
    emails: Callable[[], str], attendee: Callable[..., Any]
) -> None:
    """The card is chosen by the session; a user id in the request —
    query or header — selects nothing."""
    email_a, token_a, client_a = attendee(emails(), first_name="Alice")
    email_b, token_b, _ = attendee(emails(), first_name="Bob")
    victim = _user_id(email_b)
    response = client_a.get(
        f"{MY_CARD}?user_id={victim}&token={token_b}", headers={"X-User-Id": str(victim)}
    )
    assert response.status_code == 200
    assert response.json()["card_url"].endswith(f"/c/{token_a}")
    assert response.json()["card"]["first_name"] == "Alice"


def test_my_card_requires_an_attendee_session(
    anonymous: TestClient, staff_session: Callable[..., str]
) -> None:
    assert _error(anonymous.get(MY_CARD)) == (401, "UNAUTHENTICATED")
    with make_client() as c:
        c.cookies.set("session", staff_session(SEEDED_BOOTH_STAFF))
        assert _error(c.get(MY_CARD)) == (401, "UNAUTHENTICATED")


def test_my_card_fails_loudly_if_an_active_attendee_has_no_live_card(
    emails: Callable[[], str], attendee: Callable[..., Any]
) -> None:
    email, token, client = attendee(emails())
    execute("UPDATE contact_cards SET revoked_at = now() WHERE public_token = :t", t=token)
    with TestClient(app, raise_server_exceptions=False, headers={"Origin": "x"}) as c:
        c.cookies.set("session", client.cookies.get("session") or "")
        assert _error(c.get(MY_CARD)) == (500, "INTERNAL_ERROR")


# --------------------------------------------------------------------------
# Reads take no reset fence (§19.4)
# --------------------------------------------------------------------------


def test_card_reads_do_not_wait_on_the_reset_fence(
    anonymous: TestClient, staff_session: Callable[..., str]
) -> None:
    """Public card and staff resolve are pure reads: they complete while
    reset holds the EXCLUSIVE fence, instead of queuing behind it."""
    session = staff_session(SEEDED_BOOTH_STAFF)
    holder = psycopg.connect(PSYCOPG_URL, autocommit=True)
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (FACEMARK_RESET_FENCE,))
        results: list[int] = []
        worker = threading.Thread(
            target=lambda: results.extend(
                [
                    anonymous.get(card_path(fx.ATTENDEES[0].public_token)).status_code,
                    _resolve(fx.ATTENDEES[0].public_token, session).status_code,
                ]
            )
        )
        worker.start()
        worker.join(timeout=5)
        assert results == [200, 200]
    finally:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (FACEMARK_RESET_FENCE,))
        holder.close()


# --------------------------------------------------------------------------
# Log masking (§21.11)
# --------------------------------------------------------------------------


def test_mask_card_tokens() -> None:
    token = fx.ATTENDEES[0].public_token
    assert mask_card_tokens(f"GET /c/{token}") == "GET /c/***"
    assert mask_card_tokens(f"/api/cards/{token}?x=1") == "/api/cards/***?x=1"
    assert mask_card_tokens("/api/attendees/me/card") == "/api/attendees/me/card"


def test_uvicorn_access_log_records_are_masked() -> None:
    """The exact record shape uvicorn's access logger emits, run through
    the filters the application installed on that logger."""
    token = fx.ATTENDEES[0].public_token
    access = logging.getLogger("uvicorn.access")
    assert any(isinstance(f, CardTokenLogFilter) for f in access.filters)
    for path in (f"/api/cards/{token}", f"/c/{token}"):
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            0,
            '%s - "%s %s HTTP/%s" %d',
            ("203.0.113.9:50000", "GET", path, "1.1", 200),
            None,
        )
        assert access.filter(record)
        line = record.getMessage()
        assert token not in line
        assert "/***" in line


def test_the_application_logs_no_card_token_while_serving_cards(
    caplog: pytest.LogCaptureFixture,
    anonymous: TestClient,
    staff_session: Callable[..., str],
) -> None:
    caplog.set_level(logging.DEBUG)
    token = fx.ATTENDEES[0].public_token
    anonymous.get(card_path(token))
    _resolve(token, staff_session(SEEDED_BOOTH_STAFF))
    # `httpx` is the TEST CLIENT logging its own outgoing request —
    # the browser's side of the wire, not the application's logs.
    server_side = [r for r in caplog.records if not r.name.startswith("httpx")]
    assert all(token not in r.getMessage() for r in server_side)
