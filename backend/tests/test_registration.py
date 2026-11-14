"""M04 acceptance: `POST /api/attendees/register` — ARCHITECTURE.md §8.1.

Real PostgreSQL throughout. The endpoint commits (via
`mutating_transaction`), so every test uses the `unique_email` fixture
and cleans up after itself.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.modules.attendees.service import CONSENT_VERSION
from tests.m04_support import (
    captured_otp,
    cleanup_email,
    execute,
    fetch_all,
    fetch_one,
    new_email,
    register_payload,
)

REGISTER = "/api/attendees/register"
VERIFY = "/api/auth/otp/verify"


def _user(email: str) -> Any:
    return fetch_one(
        "SELECT id, first_name, last_name, email, phone, profession, linkedin_url, "
        "sponsor_consent, sponsor_consent_at, sponsor_consent_version, status, activated_at "
        "FROM users WHERE email = :e",
        e=email.lower(),
    )


def _interest_slugs(user_id: int) -> set[str]:
    rows = fetch_all(
        "SELECT i.slug FROM user_interests ui JOIN interests i ON i.id = ui.interest_id "
        "WHERE ui.user_id = :u",
        u=user_id,
    )
    return {r[0] for r in rows}


# --------------------------------------------------------------------------
# Happy path: PROVISIONAL, and no session
# --------------------------------------------------------------------------


def test_valid_registration_creates_a_provisional_attendee(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    response = client.post(REGISTER, json=register_payload(unique_email))
    assert response.status_code == 200

    user = _user(unique_email)
    assert user is not None
    assert user.status == "PROVISIONAL"
    assert user.activated_at is None
    assert user.first_name == "Grace"
    assert user.profession == "Systems Engineer"
    assert user.linkedin_url == "https://www.linkedin.com/in/grace-hopper-demo"


def test_registration_returns_no_session(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """ARCHITECTURE.md §8.1: "It returns no session." — no Set-Cookie,
    and no `sessions` row exists for the new user."""
    response = client.post(REGISTER, json=register_payload(unique_email))
    assert response.status_code == 200
    # The only cookie is the registration intent, scoped to /api/auth; it
    # authenticates nothing (see test_registration_intent.py).
    cookies = response.headers.get_list("set-cookie")
    assert all(c.startswith("registration_intent=") for c in cookies)
    assert all("Path=/api/auth" in c and "HttpOnly" in c for c in cookies)
    assert set(response.json().keys()) == {"message"}  # no id, no status, nothing internal

    user = _user(unique_email)
    session_count = fetch_one("SELECT count(*) FROM sessions WHERE user_id = :u", u=user.id)
    assert session_count[0] == 0


def test_registration_creates_an_inert_contact_card(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """§8.1: "The card token is generated at registration but is inert
    until activation." 256-bit, 43 URL-safe chars (§9.1, reconciled in
    M06), not derived from any attribute."""
    client.post(REGISTER, json=register_payload(unique_email))
    user = _user(unique_email)
    card = fetch_one(
        "SELECT public_token, revoked_at FROM contact_cards WHERE user_id = :u", u=user.id
    )
    assert card is not None
    assert card.revoked_at is None
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", card.public_token)
    assert "grace" not in card.public_token.lower()


# --------------------------------------------------------------------------
# Validation (422 VALIDATION_ERROR, never reaches the database)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing_field",
    ["first_name", "last_name", "email", "phone", "profession", "sponsor_consent"],
)
def test_missing_required_field_is_rejected(
    client: TestClient, unique_email: str, missing_field: str
) -> None:
    payload = register_payload(unique_email)
    del payload[missing_field]
    response = client.post(REGISTER, json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _user(unique_email) is None


def test_sponsor_consent_has_no_default(client: TestClient, unique_email: str) -> None:
    """ARCHITECTURE.md §8.1: "a required explicit boolean with no
    default. Absence is a validation error, not an implied value." A
    null is equally not an explicit choice."""
    payload = register_payload(unique_email)
    payload["sponsor_consent"] = None
    response = client.post(REGISTER, json=payload)
    assert response.status_code == 422
    assert _user(unique_email) is None


@pytest.mark.parametrize("field", ["first_name", "last_name", "phone", "profession"])
def test_blank_required_text_is_rejected(client: TestClient, unique_email: str, field: str) -> None:
    response = client.post(REGISTER, json=register_payload(unique_email, **{field: "   "}))
    assert response.status_code == 422


def test_malformed_email_is_rejected(client: TestClient) -> None:
    response = client.post(REGISTER, json=register_payload("not-an-email"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "bad_url",
    [
        "linkedin.com/in/x",  # no scheme
        "ftp://www.linkedin.com/in/x",  # wrong scheme
        "https://evil.example.com/in/x",  # wrong host
        "javascript:alert(1)",
        "https://linkedin.com.evil.example/in/x",
        "https://linkedin.com@evil.example/in/x",
    ],
)
def test_linkedin_url_shape_is_validated(
    client: TestClient, unique_email: str, bad_url: str
) -> None:
    """§8.1: "LinkedIn URL validated for scheme and host shape." """
    response = client.post(REGISTER, json=register_payload(unique_email, linkedin_url=bad_url))
    assert response.status_code == 422
    assert _user(unique_email) is None


def test_linkedin_url_is_optional(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    payload = register_payload(unique_email)
    del payload["linkedin_url"]
    assert client.post(REGISTER, json=payload).status_code == 200
    assert _user(unique_email).linkedin_url is None


def test_unexpected_field_is_rejected(client: TestClient, unique_email: str) -> None:
    """Invariant #16: `extra="forbid"` — a client cannot smuggle in
    server-owned state like `status`."""
    response = client.post(REGISTER, json=register_payload(unique_email, status="ACTIVE"))
    assert response.status_code == 422
    assert _user(unique_email) is None


def test_validation_error_exposes_no_internals(client: TestClient) -> None:
    body = client.post(REGISTER, json={}).json()
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {"code", "message", "details", "correlation_id"}
    assert "Traceback" not in str(body)


# --------------------------------------------------------------------------
# Email normalisation
# --------------------------------------------------------------------------


def test_email_is_normalised_to_lowercase(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    mixed = unique_email.upper()
    assert client.post(REGISTER, json=register_payload(mixed)).status_code == 200
    user = _user(unique_email)
    assert user is not None
    assert user.email == unique_email.lower()


def test_differently_cased_email_is_the_same_attendee(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """citext + normalisation: a second registration differing only in
    case hits the PROVISIONAL branch, never creates a second user."""
    client.post(REGISTER, json=register_payload(unique_email))
    client.post(REGISTER, json=register_payload(unique_email.upper(), first_name="Second"))
    rows = fetch_all("SELECT id FROM users WHERE email = :e", e=unique_email)
    assert len(rows) == 1
    assert _user(unique_email).first_name == "Second"


# --------------------------------------------------------------------------
# Interests
# --------------------------------------------------------------------------


def test_interests_are_persisted(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    payload = register_payload(unique_email, interest_slugs=["robotics", "automation", "fintech"])
    client.post(REGISTER, json=payload)
    assert _interest_slugs(_user(unique_email).id) == {"robotics", "automation", "fintech"}


def test_duplicate_interest_slugs_are_collapsed(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    payload = register_payload(unique_email, interest_slugs=["robotics", "robotics"])
    assert client.post(REGISTER, json=payload).status_code == 200
    assert _interest_slugs(_user(unique_email).id) == {"robotics"}


def test_registration_with_no_interests_is_allowed(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """§11.4 handles "attendee selected no interests" explicitly — it
    is a legitimate state, not a validation failure."""
    assert (
        client.post(REGISTER, json=register_payload(unique_email, interest_slugs=[])).status_code
        == 200
    )
    assert _interest_slugs(_user(unique_email).id) == set()


def test_unknown_interest_slug_is_rejected_not_dropped(
    client: TestClient, unique_email: str
) -> None:
    """§8.1: "unknown slugs -> UNKNOWN_INTEREST, rather than silently
    dropped." Nothing is created — not even the valid parts."""
    payload = register_payload(unique_email, interest_slugs=["robotics", "quantum-knitting"])
    response = client.post(REGISTER, json=payload)
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "UNKNOWN_INTEREST"
    assert "quantum-knitting" in body["message"]
    assert _user(unique_email) is None


def test_inactive_interest_is_treated_as_unknown(client: TestClient, unique_email: str) -> None:
    """§8.1: "resolved by slug against ACTIVE rows"."""
    slug = f"retired-{unique_email.split('@')[0]}"
    execute("INSERT INTO interests (slug, name, is_active) VALUES (:s, 'Retired', false)", s=slug)
    try:
        response = client.post(REGISTER, json=register_payload(unique_email, interest_slugs=[slug]))
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "UNKNOWN_INTEREST"
    finally:
        execute("DELETE FROM interests WHERE slug = :s", s=slug)


def test_unknown_interest_is_rejected_identically_for_an_active_email(
    client: TestClient, otp_log: pytest.LogCaptureFixture
) -> None:
    """Interests are validated BEFORE the email branch, so a bad slug
    produces the same UNKNOWN_INTEREST for a brand-new address as for
    an existing ACTIVE one — the error cannot itself become a
    membership oracle. Uses a seeded ACTIVE demo attendee (M03)."""
    active = "elena.marchetti@attendee.facemark.demo"
    fresh = new_email()
    try:
        bad = {"interest_slugs": ["not-a-real-interest"]}
        r_active = client.post(REGISTER, json=register_payload(active, **bad))
        r_fresh = client.post(REGISTER, json=register_payload(fresh, **bad))
        assert r_active.status_code == r_fresh.status_code == 400
        assert r_active.json()["error"]["code"] == r_fresh.json()["error"]["code"]
        assert r_active.json()["error"]["message"] == r_fresh.json()["error"]["message"]
    finally:
        cleanup_email(fresh)


# --------------------------------------------------------------------------
# Sponsor consent — separate, explicit, and audited in the same transaction
# --------------------------------------------------------------------------


def test_sponsor_consent_true_is_recorded_with_timestamp_and_version(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    client.post(REGISTER, json=register_payload(unique_email, sponsor_consent=True))
    user = _user(unique_email)
    assert user.sponsor_consent is True
    assert user.sponsor_consent_at is not None
    assert user.sponsor_consent_version == CONSENT_VERSION

    audit = fetch_all(
        "SELECT granted, consent_version, source FROM consent_audit WHERE user_id = :u", u=user.id
    )
    assert [(a.granted, a.consent_version, a.source) for a in audit] == [
        (True, CONSENT_VERSION, "registration")
    ]


def test_sponsor_consent_false_is_recorded_and_never_inferred(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """Registering does not imply consent. A refusal is itself an
    auditable decision, so it still gets an audit row."""
    client.post(REGISTER, json=register_payload(unique_email, sponsor_consent=False))
    user = _user(unique_email)
    assert user.sponsor_consent is False
    assert user.sponsor_consent_at is None
    assert user.sponsor_consent_version is None

    audit = fetch_all("SELECT granted FROM consent_audit WHERE user_id = :u", u=user.id)
    assert [a.granted for a in audit] == [False]


def test_consent_change_on_reregistration_appends_audit_history(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """§7.1: "the current value alone cannot show a later withdrawal" —
    history is appended, never overwritten."""
    client.post(REGISTER, json=register_payload(unique_email, sponsor_consent=True))
    client.post(REGISTER, json=register_payload(unique_email, sponsor_consent=False))
    user = _user(unique_email)
    assert user.sponsor_consent is False
    audit = fetch_all("SELECT granted FROM consent_audit WHERE user_id = :u ORDER BY id", u=user.id)
    assert [a.granted for a in audit] == [True, False]


def test_consent_is_not_exposed_in_the_response(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """Invariant #19: sponsor-consent state never appears in a public
    response."""
    response = client.post(REGISTER, json=register_payload(unique_email, sponsor_consent=True))
    assert "consent" not in response.text.lower()
    assert "phone" not in response.text.lower()


# --------------------------------------------------------------------------
# Duplicate-email behaviour — three branches, one byte-identical body
# --------------------------------------------------------------------------


def test_reregistering_a_provisional_email_overwrites_the_profile(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """§8.1 Step 2, PROVISIONAL branch: "Overwrite the provisional
    profile with the new submission and send a fresh OTP." The card is
    kept (same token), the old code is superseded."""
    client.post(REGISTER, json=register_payload(unique_email, interest_slugs=["robotics"]))
    first_code = captured_otp(otp_log.records, unique_email)
    first_user = _user(unique_email)
    first_token = fetch_one(
        "SELECT public_token FROM contact_cards WHERE user_id = :u", u=first_user.id
    )[0]

    client.post(
        REGISTER,
        json=register_payload(
            unique_email,
            first_name="Ada",
            profession="Mathematician",
            interest_slugs=["fintech", "sustainability"],
        ),
    )
    second_code = captured_otp(otp_log.records, unique_email)

    user = _user(unique_email)
    assert user.id == first_user.id
    assert (user.first_name, user.profession) == ("Ada", "Mathematician")
    assert user.status == "PROVISIONAL"
    assert _interest_slugs(user.id) == {"fintech", "sustainability"}

    cards = fetch_all("SELECT public_token FROM contact_cards WHERE user_id = :u", u=user.id)
    assert [c.public_token for c in cards] == [first_token]

    live = fetch_all(
        "SELECT id FROM otp_challenges WHERE email = :e AND consumed_at IS NULL", e=unique_email
    )
    assert len(live) == 1  # the first challenge was superseded

    if first_code != second_code:  # 1-in-a-million collision would make this vacuous
        stale = client.post(VERIFY, json={"email": unique_email, "code": first_code})
        assert stale.status_code == 400
    fresh = client.post(VERIFY, json={"email": unique_email, "code": second_code})
    assert fresh.status_code == 200


def test_registering_an_active_email_creates_nothing_and_sends_a_login_code(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """§8.1 Step 2, ACTIVE branch: "Create nothing. Send a login OTP to
    the registered address. The submitted profile fields are
    discarded." This is what stops registration being an
    account-takeover vector."""
    client.post(REGISTER, json=register_payload(unique_email))
    code = captured_otp(otp_log.records, unique_email)
    assert client.post(VERIFY, json={"email": unique_email, "code": code}).status_code == 200
    before = _user(unique_email)

    response = client.post(
        REGISTER,
        json=register_payload(
            unique_email,
            first_name="Attacker",
            phone="+1-555-6666",
            interest_slugs=["cybersecurity"],
            sponsor_consent=True,
        ),
    )
    assert response.status_code == 200

    after = _user(unique_email)
    assert after == before  # every column untouched, including consent
    assert len(fetch_all("SELECT id FROM users WHERE email = :e", e=unique_email)) == 1
    assert _interest_slugs(after.id) == {"generative-ai", "data-analytics"}
    audit = fetch_all("SELECT granted FROM consent_audit WHERE user_id = :u", u=after.id)
    assert len(audit) == 1  # no new consent decision recorded

    login = fetch_all(
        "SELECT user_id FROM otp_challenges WHERE email = :e AND purpose = 'LOGIN' "
        "AND consumed_at IS NULL",
        e=unique_email,
    )
    assert [r.user_id for r in login] == [after.id]
    captured_otp(otp_log.records, unique_email, purpose="LOGIN")  # delivered to the owner


def test_all_three_duplicate_email_branches_return_byte_identical_bodies(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """IMPLEMENTATION_PLAN.md M04: "all three duplicate-email branches
    return byte-identical bodies" — the endpoint is not a membership
    oracle."""
    new_branch = client.post(REGISTER, json=register_payload(unique_email))
    provisional_branch = client.post(REGISTER, json=register_payload(unique_email))
    code = captured_otp(otp_log.records, unique_email)
    client.post(VERIFY, json={"email": unique_email, "code": code})
    active_branch = client.post(REGISTER, json=register_payload(unique_email))

    assert new_branch.status_code == provisional_branch.status_code == active_branch.status_code
    assert new_branch.content == provisional_branch.content == active_branch.content
    # Every branch sets the same kind of cookie (a fresh registration intent),
    # so the headers are no oracle either; none of them sets a session.
    for r in (new_branch, provisional_branch, active_branch):
        cookies = r.headers.get_list("set-cookie")
        assert len(cookies) == 1 and cookies[0].startswith("registration_intent=")


# --------------------------------------------------------------------------
# GET /api/interests — the catalogue the registration form offers
# --------------------------------------------------------------------------


def test_interest_catalogue_lists_active_interests_in_order(client: TestClient) -> None:
    """Uses the M03 seed: the exact slugs registration accepts, in
    curated sort order, exposing only slug + name (no internal ids)."""
    response = client.get("/api/interests")
    assert response.status_code == 200
    body = response.json()
    assert all(set(item.keys()) == {"slug", "name"} for item in body)
    slugs = [item["slug"] for item in body]
    assert slugs[:4] == ["artificial-intelligence", "generative-ai", "data-analytics", "automation"]
    assert {"robotics", "fintech", "sustainability"} <= set(slugs)


def test_interest_catalogue_hides_inactive_interests(client: TestClient) -> None:
    slug = f"retired-{new_email().split('@')[0]}"
    execute("INSERT INTO interests (slug, name, is_active) VALUES (:s, 'Retired', false)", s=slug)
    try:
        slugs = {item["slug"] for item in client.get("/api/interests").json()}
        assert slug not in slugs
    finally:
        execute("DELETE FROM interests WHERE slug = :s", s=slug)
