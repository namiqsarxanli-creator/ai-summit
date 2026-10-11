"""Security audit (run-1) regressions around registration, OTP issuance and the
shared request budgets.

* attendees/provisional-overwrite-activates-third-party-profile-and-consent:
  activation applies only the submission made in the verifying browser.
* auth/register-bypasses-per-email-otp-issuance-limit: register shares the
  per-email issuance bucket with POST /auth/otp/request.
* ratelimit/victim-identity-bucket-exhaustion: a client over its shared-IP
  ceiling no longer spends anyone's per-email bucket.
* otp/outbox-fifo-starvation-no-aggregate-producer-bound: sign-in and staff
  recovery codes are claimed before activation codes.
* realtime/sse-no-concurrent-stream-cap: one attendee cannot hold streams
  without bound.

Real PostgreSQL; every client is a separate "browser" with its own cookies.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core.errors import RateLimitedError
from app.core.ratelimit import (
    OTP_REQUEST_PER_EMAIL,
    OTP_REQUEST_PER_IP,
    InMemoryFixedWindowStore,
    RateLimit,
    RateLimiter,
)
from app.db.session import SessionLocal
from app.modules.attendees.models import OtpPurpose
from app.modules.auth import otp_outbox as ob
from app.modules.realtime import routers as realtime_routers
from app.modules.realtime import service as realtime_service
from app.modules.realtime.broker import event_bus
from tests.m04_support import (
    captured_otp,
    fetch_all,
    fetch_one,
    make_client,
    register_payload,
)
from tests.outbox_support import clean_outbox, make_delivery, make_due, outbox_settings

REGISTER = "/api/attendees/register"
OTP_REQUEST = "/api/auth/otp/request"
VERIFY = "/api/auth/otp/verify"


@pytest.fixture
def other_browser() -> Iterator[TestClient]:
    with make_client() as c:
        yield c


def _user(email: str) -> Any:
    return fetch_one(
        "SELECT id, first_name, phone, sponsor_consent, sponsor_consent_version, status "
        "FROM users WHERE email = :e",
        e=email.lower(),
    )


def _interests(user_id: int) -> set[str]:
    rows = fetch_all(
        "SELECT i.slug FROM user_interests ui JOIN interests i ON i.id = ui.interest_id "
        "WHERE ui.user_id = :u",
        u=user_id,
    )
    return {r[0] for r in rows}


# --------------------------------------------------------------------------
# Activation is bound to the submitting browser
# --------------------------------------------------------------------------


def test_someone_elses_resubmission_cannot_decide_what_the_owner_activates(
    client: TestClient,
    other_browser: TestClient,
    unique_email: str,
    otp_log: pytest.LogCaptureFixture,
) -> None:
    owner = register_payload(unique_email, first_name="Owner", sponsor_consent=False)
    assert client.post(REGISTER, json=owner).status_code == 200
    forged = register_payload(
        unique_email,
        first_name="Attacker",
        phone="+10000000000",
        interest_slugs=["cybersecurity"],
        sponsor_consent=True,
    )
    assert other_browser.post(REGISTER, json=forged).status_code == 200
    # The forged submission superseded the owner's code; the owner uses the new one.
    code = captured_otp(otp_log.records, unique_email)

    assert client.post(VERIFY, json={"email": unique_email, "code": code}).status_code == 200

    user = _user(unique_email)
    assert user.status == "ACTIVE"
    assert (user.first_name, user.phone) == ("Owner", owner["phone"])
    assert user.sponsor_consent is False
    assert _interests(user.id) == set(owner["interest_slugs"])


def test_the_owners_own_consent_survives_a_forged_resubmission(
    client: TestClient,
    other_browser: TestClient,
    unique_email: str,
    otp_log: pytest.LogCaptureFixture,
) -> None:
    client.post(REGISTER, json=register_payload(unique_email, sponsor_consent=True))
    other_browser.post(
        REGISTER, json=register_payload(unique_email, first_name="X", sponsor_consent=False)
    )
    code = captured_otp(otp_log.records, unique_email)
    assert client.post(VERIFY, json={"email": unique_email, "code": code}).status_code == 200

    user = _user(unique_email)
    assert user.sponsor_consent is True
    assert user.sponsor_consent_version is not None
    assert user.first_name == "Grace"


def test_activation_without_the_submitting_browser_never_grants_consent(
    client: TestClient,
    other_browser: TestClient,
    unique_email: str,
    otp_log: pytest.LogCaptureFixture,
) -> None:
    """Someone registers an address with consent; the code is then entered from a
    browser that did not submit it (another device, or the real owner of an
    address someone else registered). Consent is off; it is never granted on
    someone else's say-so, and the switch is audited."""
    other_browser.post(REGISTER, json=register_payload(unique_email, sponsor_consent=True))
    code = captured_otp(otp_log.records, unique_email)

    assert client.post(VERIFY, json={"email": unique_email, "code": code}).status_code == 200

    user = _user(unique_email)
    assert user.status == "ACTIVE"
    assert user.sponsor_consent is False
    audit = fetch_all(
        "SELECT granted, source FROM consent_audit WHERE user_id = :u ORDER BY id", u=user.id
    )
    assert [(a.granted, a.source) for a in audit] == [(True, "registration"), (False, "activation")]


def test_the_intent_cookie_is_cleared_after_a_successful_activation(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    client.post(REGISTER, json=register_payload(unique_email))
    assert client.cookies.get("registration_intent") is not None
    code = captured_otp(otp_log.records, unique_email)
    response = client.post(VERIFY, json={"email": unique_email, "code": code})
    assert response.status_code == 200
    assert any(
        c.startswith('registration_intent=""') or c.startswith("registration_intent=;")
        for c in response.headers.get_list("set-cookie")
    )


# --------------------------------------------------------------------------
# Register shares the per-email issuance limit
# --------------------------------------------------------------------------


def test_register_cannot_issue_more_codes_to_one_address_than_otp_request(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    for _ in range(OTP_REQUEST_PER_EMAIL.limit):
        assert client.post(REGISTER, json=register_payload(unique_email)).status_code == 200
    refused = client.post(REGISTER, json=register_payload(unique_email))
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "RATE_LIMITED"
    # The same bucket: an OTP request for the address is refused too.
    assert client.post(OTP_REQUEST, json={"email": unique_email}).status_code == 429
    live = fetch_all(
        "SELECT id FROM otp_challenges WHERE email = :e AND consumed_at IS NULL", e=unique_email
    )
    assert len(live) == 1
    issued = fetch_one("SELECT count(*) FROM otp_challenges WHERE email = :e", e=unique_email)
    assert issued[0] == OTP_REQUEST_PER_EMAIL.limit


# --------------------------------------------------------------------------
# A client over its IP ceiling spends no one's email bucket
# --------------------------------------------------------------------------


def test_a_request_refused_by_the_ip_ceiling_does_not_count_against_the_email() -> None:
    now = 1_800_000_000.0
    limiter = RateLimiter(InMemoryFixedWindowStore(), clock=lambda: now)
    attacker_ceiling = RateLimit(OTP_REQUEST_PER_IP.name, 1, 900)
    limiter.check([(attacker_ceiling, "203.0.113.1"), (OTP_REQUEST_PER_EMAIL, "other@x.demo")])
    for _ in range(10):
        with pytest.raises(RateLimitedError):
            limiter.check(
                [(attacker_ceiling, "203.0.113.1"), (OTP_REQUEST_PER_EMAIL, "victim@x.demo")]
            )
    # The victim, from their own address, still has their full allowance.
    for _ in range(OTP_REQUEST_PER_EMAIL.limit):
        limiter.check(
            [(OTP_REQUEST_PER_IP, "198.51.100.7"), (OTP_REQUEST_PER_EMAIL, "victim@x.demo")]
        )


def test_the_bucket_that_refuses_is_still_counted() -> None:
    now = 1_800_000_000.0
    limiter = RateLimiter(InMemoryFixedWindowStore(), clock=lambda: now)
    tight = RateLimit("t:email", 1, 900)
    limiter.check([(tight, "a")])
    with pytest.raises(RateLimitedError):
        limiter.check([(tight, "a")])
    with pytest.raises(RateLimitedError):
        limiter.check([(tight, "a")])


# --------------------------------------------------------------------------
# Outbox: sign-in and recovery codes are claimed before activation codes
# --------------------------------------------------------------------------


@pytest.fixture
def worker_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    settings = outbox_settings()
    monkeypatch.setattr("app.modules.auth.otp_outbox.get_settings", lambda: settings)
    clean_outbox()
    yield
    clean_outbox()


def test_login_and_staff_reset_codes_are_claimed_before_older_activation_codes(
    worker_settings: None,
) -> None:
    activations = [make_delivery(purpose=OtpPurpose.ACTIVATION)[1] for _ in range(3)]
    login = make_delivery(purpose=OtpPurpose.LOGIN)[1]
    reset = make_delivery(purpose=OtpPurpose.STAFF_PASSWORD_RESET)[1]
    for delivery_id in [*activations, login, reset]:
        make_due(delivery_id)

    db = SessionLocal()
    try:
        claimed = ob.claim_batch(db, worker_id="w-priority", limit=2)
        db.commit()
    finally:
        db.close()

    assert {c.id for c in claimed} == {login, reset}


# --------------------------------------------------------------------------
# Live streams are capped per attendee and in total
# --------------------------------------------------------------------------


def _stream_request() -> Request:
    return Request({"type": "http", "headers": [(b"cookie", b"session=x")], "method": "GET"})


def test_an_attendee_cannot_open_streams_beyond_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    attendee: Any = SimpleNamespace(id=-424242)
    monkeypatch.setattr(realtime_routers, "resolve_attendee_for_stream", lambda _t: attendee)
    queues: list[asyncio.Queue[Any]] = []
    try:
        for _ in range(realtime_service.MAX_STREAMS_PER_ATTENDEE):
            assert realtime_routers._current_attendee_for_stream(_stream_request()) is attendee
            queues.append(event_bus.subscribe(attendee.id))
        with pytest.raises(RateLimitedError):
            realtime_routers._current_attendee_for_stream(_stream_request())
    finally:
        for q in queues:
            event_bus.unsubscribe(attendee.id, q)
    assert realtime_routers._current_attendee_for_stream(_stream_request()) is attendee


def test_the_global_stream_cap_applies_to_everyone(monkeypatch: pytest.MonkeyPatch) -> None:
    attendee: Any = SimpleNamespace(id=-434343)
    monkeypatch.setattr(realtime_routers, "resolve_attendee_for_stream", lambda _t: attendee)
    monkeypatch.setattr(realtime_service, "MAX_STREAMS_TOTAL", event_bus.total_subscribers())
    with pytest.raises(RateLimitedError):
        realtime_routers._current_attendee_for_stream(_stream_request())
