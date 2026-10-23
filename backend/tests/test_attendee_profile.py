"""M08 acceptance: `GET /api/attendees/me/profile` — the attendee's own
private profile screen (IMPLEMENTATION_PLAN.md M08: "registration data,
selected interests, consent state, private view only").

Real PostgreSQL, real registration/activation, like the M06/M07 suites.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.core.locks import FACEMARK_RESET_FENCE
from tests.m04_support import (
    PSYCOPG_URL,
    captured_otp,
    cleanup_email,
    fetch_one,
    make_client,
    new_email,
    register_payload,
)

REGISTER = "/api/attendees/register"
VERIFY = "/api/auth/otp/verify"
PROFILE = "/api/attendees/me/profile"


def _error(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


@pytest.fixture
def emails() -> Iterator[Callable[[], str]]:
    issued: list[str] = []

    def issue() -> str:
        email = new_email("m08")
        issued.append(email)
        return email

    yield issue
    for email in issued:
        cleanup_email(email)


@pytest.fixture
def attendee(
    otp_log: pytest.LogCaptureFixture,
) -> Callable[..., tuple[str, TestClient]]:
    """Registers and activates. Returns (email, a client holding the
    session)."""
    clients: list[TestClient] = []

    def make(email: str, **overrides: Any) -> tuple[str, TestClient]:
        c = make_client()
        clients.append(c)
        assert c.post(REGISTER, json=register_payload(email, **overrides)).status_code == 200
        code = captured_otp(otp_log.records, email)
        assert c.post(VERIFY, json={"email": email, "code": code}).status_code == 200
        return email, c

    return make


def test_profile_requires_an_attendee_session() -> None:
    with make_client() as c:
        assert _error(c.get(PROFILE)) == (401, "UNAUTHENTICATED")


def test_profile_returns_registration_data_interests_and_consent(
    emails: Callable[[], str], attendee: Callable[..., tuple[str, TestClient]]
) -> None:
    email, client = attendee(
        emails(),
        first_name="Ada",
        last_name="Lovelace",
        phone="+1-555-0142",
        profession="Systems Engineer",
        linkedin_url="https://www.linkedin.com/in/ada-demo",
        interest_slugs=["generative-ai", "data-analytics"],
        sponsor_consent=True,
    )
    response = client.get(PROFILE)
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": email.lower(),
        "phone": "+1-555-0142",
        "profession": "Systems Engineer",
        "linkedin_url": "https://www.linkedin.com/in/ada-demo",
        "sponsor_consent": True,
        # Ordered by the interest catalogue's own sort_order (§7.2),
        # same as the registration form's interest list — NOT
        # alphabetical and not selection order.
        "interests": [
            {"slug": "generative-ai", "name": "Generative AI"},
            {"slug": "data-analytics", "name": "Data Analytics"},
        ],
        # Added with profile editing: what an edit must send back, the consent's
        # time and wording (None while withheld), and the photo (None until set).
        "sponsor_consent_at": body["sponsor_consent_at"],
        "sponsor_consent_version": "registration-v1",
        "version": 1,
        "photo": None,
        # Added with the interest card. The company is required at registration
        # (23 Oct 2026); the card stays optional, empty when skipped.
        "company": "Hopper Labs",
        "interest_card": {"sector": None, "topics": [], "seeking": [], "offering": []},
    }
    assert body["sponsor_consent_at"] is not None


def test_profile_reflects_consent_false_and_no_linkedin(
    emails: Callable[[], str], attendee: Callable[..., tuple[str, TestClient]]
) -> None:
    _, client = attendee(emails(), sponsor_consent=False, linkedin_url=None, interest_slugs=[])
    body = client.get(PROFILE).json()
    assert body["sponsor_consent"] is False
    assert body["linkedin_url"] is None
    assert body["interests"] == []


def test_profile_never_includes_internal_ids_or_session_data(
    emails: Callable[[], str], attendee: Callable[..., tuple[str, TestClient]]
) -> None:
    _, client = attendee(emails(), interest_slugs=["generative-ai"])
    response = client.get(PROFILE)
    for leaked in ('"id"', '"user_id"', '"session"', '"status"', '"activated_at"'):
        assert leaked not in response.text
    assert set(response.json()) == {
        "first_name",
        "last_name",
        "email",
        "phone",
        "profession",
        "linkedin_url",
        "sponsor_consent",
        "sponsor_consent_at",
        "sponsor_consent_version",
        "interests",
        "version",
        "photo",
        "company",
        "interest_card",
    }


def test_profile_is_the_signed_in_attendees_own_never_a_client_supplied_id(
    emails: Callable[[], str], attendee: Callable[..., tuple[str, TestClient]]
) -> None:
    email_a, client_a = attendee(emails(), first_name="Alice")
    email_b, _ = attendee(emails(), first_name="Bob")
    victim_id = fetch_one("SELECT id FROM users WHERE email = :e", e=email_b.lower())[0]

    response = client_a.get(f"{PROFILE}?user_id={victim_id}")
    assert response.status_code == 200
    assert response.json()["first_name"] == "Alice"


def test_a_provisional_attendee_cannot_reach_profile(emails: Callable[[], str]) -> None:
    email = emails()
    with make_client() as c:
        assert c.post(REGISTER, json=register_payload(email)).status_code == 200
        assert _error(c.get(PROFILE)) == (401, "UNAUTHENTICATED")


def test_profile_does_not_wait_on_the_reset_fence(
    emails: Callable[[], str], attendee: Callable[..., tuple[str, TestClient]]
) -> None:
    """Pure read (§19.4)."""
    _, client = attendee(emails())
    holder = psycopg.connect(PSYCOPG_URL, autocommit=True)
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (FACEMARK_RESET_FENCE,))
        results: list[int] = []
        worker = threading.Thread(target=lambda: results.append(client.get(PROFILE).status_code))
        worker.start()
        worker.join(timeout=5)
        assert results == [200]
    finally:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (FACEMARK_RESET_FENCE,))
        holder.close()
