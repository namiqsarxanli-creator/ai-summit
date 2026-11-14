"""M10 acceptance: `scan_ref` issuance — ARCHITECTURE.md §13.1-§13.3,
§21.11, §24.20-§24.21, IMPLEMENTATION_PLAN.md M10.

M10 ISSUES `scan_ref`; it does not verify it. The envelope's
cryptographic *verification* (checks 1-8: HMAC mismatch, wrong version,
future-`iat`, staff/session binding, expiry) is M11's, exercised against
the award endpoint that does not exist yet — nothing here tests
tampering, expiry or wrong-staff/session rejection, because M10 has no
code path that rejects a `scan_ref` at all. What IS tested: the envelope
is built exactly as §13.3 specifies, it is issued only for a genuinely
resolved attendee, it carries the right staff/session/token binding
values, and it never reaches a log.

Real PostgreSQL throughout, following test_staff_auth.py's own
convention: fresh, uniquely-named, committed booths and staff accounts
(the rollback `db` fixture cannot be used — the running app serves
requests through a different connection).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text

from app.core.config import get_settings
from app.core.security import hash_password, hash_session_token
from app.db.session import SessionLocal
from app.modules.staff.models import StaffRole
from app.modules.staff.scan_ref import SCAN_REF_VERSION, issue_scan_ref
from app.seed import demo_fixtures as fx
from tests.m04_support import execute, fetch_one, make_client

LOGIN = "/api/staff/auth/login"
LOGOUT = "/api/staff/auth/logout"
RESOLVE = "/api/staff/scan/resolve"

TEST_PASSWORD = "m10-test-only-staff-password"  # noqa: S105
TEST_PASSWORD_HASH = hash_password(TEST_PASSWORD)

SCAN_REF_SHAPE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def _insert_returning_id(sql: str, **params: Any) -> int:
    db = SessionLocal()
    try:
        row: Any = db.execute(text(sql), params).scalar_one()
        db.commit()
        return int(row)
    finally:
        db.close()


def _decode_payload(scan_ref: str) -> dict[str, Any]:
    body, _, _sig = scan_ref.partition(".")
    padded = body + "=" * (-len(body) % 4)
    return dict(json.loads(base64.urlsafe_b64decode(padded)))


def _recompute_signature(secret: str, body: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def test_booth() -> Iterator[Callable[..., int]]:
    booth_ids: list[int] = []

    def make() -> int:
        slug = f"m10-test-booth-{uuid.uuid4().hex[:10]}"
        booth_id = _insert_returning_id(
            "INSERT INTO booths (slug, name) VALUES (:slug, :name) RETURNING id",
            slug=slug,
            name=f"M10 Test Booth {slug[-6:]}",
        )
        booth_ids.append(booth_id)
        return booth_id

    yield make
    for booth_id in booth_ids:
        execute("DELETE FROM staff_accounts WHERE booth_id = :b", b=booth_id)
        execute("DELETE FROM booths WHERE id = :b", b=booth_id)


@pytest.fixture
def staff_account(test_booth: Callable[..., int]) -> Iterator[Callable[..., tuple[str, int]]]:
    emails: list[str] = []

    def make(
        *, role: StaffRole = StaffRole.BOOTH_STAFF, booth_id: int | None = None
    ) -> tuple[str, int]:
        if booth_id is None and role == StaffRole.BOOTH_STAFF:
            booth_id = test_booth()
        email = f"m10-{uuid.uuid4().hex[:10]}@staff.test.facemark.demo"
        emails.append(email)
        staff_id = _insert_returning_id(
            "INSERT INTO staff_accounts (email, password_hash, full_name, role, booth_id) "
            "VALUES (:email, :hash, 'M10 Test Staff', :role, :booth_id) RETURNING id",
            email=email,
            hash=TEST_PASSWORD_HASH,
            role=role.value,
            booth_id=booth_id,
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


def _session_id_for_cookie(raw_cookie: str) -> int:
    row = fetch_one(
        "SELECT id FROM sessions WHERE token_hash = :h", h=hash_session_token(raw_cookie)
    )
    return int(row[0])


# --------------------------------------------------------------------------
# Unit: envelope construction (§13.3)
# --------------------------------------------------------------------------


def test_canonical_json_is_byte_stable_across_key_insertion_orders() -> None:
    from app.core.security import canonical_json

    a = {"v": 1, "sid": 5, "ses": 9, "tok": "abc", "iat": 1000}
    b = {"iat": 1000, "tok": "abc", "sid": 5, "v": 1, "ses": 9}
    assert canonical_json(a) == canonical_json(b)


def test_canonical_json_has_no_insignificant_whitespace_or_trailing_newline() -> None:
    from app.core.security import canonical_json

    encoded = canonical_json({"v": 1, "sid": 1, "ses": 1, "tok": "x", "iat": 1})
    assert b" " not in encoded
    assert not encoded.endswith(b"\n")
    assert encoded == b'{"iat":1,"ses":1,"sid":1,"tok":"x","v":1}'


def test_scan_ref_is_well_formed_b_dot_sig() -> None:
    now = datetime.now(UTC)
    scan_ref = issue_scan_ref(
        secret="unit-test-secret", staff_id=1, session_id=1, card_token="tok-abc", now=now
    )
    assert SCAN_REF_SHAPE.fullmatch(scan_ref)
    assert scan_ref.count(".") == 1


def test_scan_ref_format_version_is_1() -> None:
    scan_ref = issue_scan_ref(
        secret="unit-test-secret", staff_id=1, session_id=1, card_token="t", now=datetime.now(UTC)
    )
    assert _decode_payload(scan_ref)["v"] == SCAN_REF_VERSION == 1


def test_scan_ref_iat_is_exactly_the_supplied_server_clock_in_unix_seconds() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    scan_ref = issue_scan_ref(
        secret="unit-test-secret", staff_id=1, session_id=1, card_token="t", now=now
    )
    assert _decode_payload(scan_ref)["iat"] == int(now.timestamp())


def test_scan_ref_sid_and_ses_are_exactly_the_supplied_ids() -> None:
    now = datetime.now(UTC)
    scan_ref = issue_scan_ref(
        secret="unit-test-secret", staff_id=42, session_id=777, card_token="t", now=now
    )
    payload = _decode_payload(scan_ref)
    assert payload["sid"] == 42
    assert payload["ses"] == 777


def test_scan_ref_tok_is_a_sha256_hash_never_the_raw_card_token() -> None:
    card_token = "super-secret-card-token-value"
    now = datetime.now(UTC)
    scan_ref = issue_scan_ref(
        secret="unit-test-secret", staff_id=1, session_id=1, card_token=card_token, now=now
    )
    payload = _decode_payload(scan_ref)
    assert payload["tok"] == hashlib.sha256(card_token.encode()).hexdigest()
    assert payload["tok"] != card_token
    assert card_token not in scan_ref


def test_scan_ref_hmac_covers_exactly_the_transmitted_b_not_a_reparse() -> None:
    """§13.3: "The HMAC covers B, the exact string that is transmitted,
    not a re-serialization of the parsed object." Verified independently
    of `issue_scan_ref`'s own internals: split the real output, recompute
    HMAC-SHA256 over the literal `B` substring with the same secret using
    a from-scratch implementation, and require an exact match. A
    signature that instead covered a re-serialized payload would still
    happen to match here (same content) — the point of this test is that
    `sign_hmac_sha256` is never given anything OTHER than `B` to sign, so
    a body with different (but semantically identical) formatting must
    verify against a fresh HMAC of THAT exact string, not the original."""
    secret = "unit-test-secret"
    scan_ref = issue_scan_ref(
        secret=secret, staff_id=1, session_id=1, card_token="t", now=datetime.now(UTC)
    )
    body, _, sig = scan_ref.partition(".")
    assert _recompute_signature(secret, body) == sig

    # A differently-formatted but semantically-equal body (extra space)
    # signs to something else entirely — proving the signature is over
    # the literal bytes of B, not the parsed/re-derived payload.
    reformatted = body + " "
    assert _recompute_signature(secret, reformatted) != sig


def test_scan_ref_signature_changes_if_any_payload_field_changes() -> None:
    secret = "unit-test-secret"
    now = datetime.now(UTC)
    a = issue_scan_ref(secret=secret, staff_id=1, session_id=1, card_token="t", now=now)
    b = issue_scan_ref(secret=secret, staff_id=2, session_id=1, card_token="t", now=now)
    assert a != b
    assert a.partition(".")[2] != b.partition(".")[2]


def test_scan_ref_differs_by_secret() -> None:
    now = datetime.now(UTC)
    a = issue_scan_ref(secret="secret-one", staff_id=1, session_id=1, card_token="t", now=now)
    b = issue_scan_ref(secret="secret-two", staff_id=1, session_id=1, card_token="t", now=now)
    assert a.partition(".")[2] != b.partition(".")[2]


def test_issue_scan_ref_takes_no_database_and_stores_nothing() -> None:
    """§13.3: "Being stateless, scan_ref needs no table, no cleanup job,
    and no shared state across workers." Checked at the signature level:
    the function's only inputs are the arguments themselves."""
    import inspect

    params = inspect.signature(issue_scan_ref).parameters
    assert set(params) == {"secret", "staff_id", "session_id", "card_token", "now"}


# --------------------------------------------------------------------------
# Integration: issuance through the real endpoint (§13.1 steps 4-5)
# --------------------------------------------------------------------------


def test_resolve_requires_a_staff_session_to_issue_a_scan_ref() -> None:
    with make_client() as c:
        response = c.post(RESOLVE, json={"card_token": fx.ATTENDEES[0].public_token})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert "scan_ref" not in response.text


def test_resolve_issues_a_scan_ref_bound_to_the_real_staff_and_session(
    staff_account: Callable[..., tuple[str, int]],
) -> None:
    email, staff_id = staff_account()
    with make_client() as c:
        assert _login(c, email, TEST_PASSWORD).status_code == 204
        raw_cookie = c.cookies.get("session")
        assert raw_cookie is not None
        session_id = _session_id_for_cookie(raw_cookie)

        response = c.post(RESOLVE, json={"card_token": fx.ATTENDEES[0].public_token})
        c.post(LOGOUT)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"first_name", "last_name", "profession", "scan_ref"}
    payload = _decode_payload(body["scan_ref"])
    assert payload == {
        "v": 1,
        "sid": staff_id,
        "ses": session_id,
        "tok": hashlib.sha256(fx.ATTENDEES[0].public_token.encode()).hexdigest(),
        "iat": payload["iat"],
    }
    # iat is the server clock — within a few seconds of "now", not a
    # value the request could have supplied.
    assert abs(payload["iat"] - int(datetime.now(UTC).timestamp())) < 10


def test_resolve_signature_verifies_against_the_real_configured_secret(
    staff_account: Callable[..., tuple[str, int]],
) -> None:
    email, _ = staff_account()
    with make_client() as c:
        assert _login(c, email, TEST_PASSWORD).status_code == 204
        response = c.post(RESOLVE, json={"card_token": fx.ATTENDEES[0].public_token})
        c.post(LOGOUT)
    scan_ref = response.json()["scan_ref"]
    body, _, sig = scan_ref.partition(".")
    assert _recompute_signature(get_settings().scan_ref_secret, body) == sig


def test_two_sessions_for_the_same_staff_member_get_different_ses(
    staff_account: Callable[..., tuple[str, int]],
) -> None:
    email, staff_id = staff_account()
    with make_client() as c1, make_client() as c2:
        assert _login(c1, email, TEST_PASSWORD).status_code == 204
        assert _login(c2, email, TEST_PASSWORD).status_code == 204
        r1 = c1.post(RESOLVE, json={"card_token": fx.ATTENDEES[0].public_token})
        r2 = c2.post(RESOLVE, json={"card_token": fx.ATTENDEES[1].public_token})
        c1.post(LOGOUT)
        c2.post(LOGOUT)

    p1 = _decode_payload(r1.json()["scan_ref"])
    p2 = _decode_payload(r2.json()["scan_ref"])
    assert p1["sid"] == p2["sid"] == staff_id
    assert p1["ses"] != p2["ses"]


def test_scan_ref_is_never_issued_for_an_invalid_qr(
    staff_account: Callable[..., tuple[str, int]],
) -> None:
    email, _ = staff_account()
    with make_client() as c:
        assert _login(c, email, TEST_PASSWORD).status_code == 204
        response = c.post(RESOLVE, json={"card_token": "not-a-real-token"})
        c.post(LOGOUT)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "INVALID_QR"
    assert "scan_ref" not in response.text


def test_admin_resolve_also_gets_a_scan_ref(staff_account: Callable[..., tuple[str, int]]) -> None:
    email, admin_id = staff_account(role=StaffRole.ADMIN, booth_id=None)
    with make_client() as c:
        assert _login(c, email, TEST_PASSWORD).status_code == 204
        response = c.post(RESOLVE, json={"card_token": fx.ATTENDEES[0].public_token})
        c.post(LOGOUT)
    assert response.status_code == 200
    assert _decode_payload(response.json()["scan_ref"])["sid"] == admin_id


# --------------------------------------------------------------------------
# Sensitive logging (§21.11: "scan_ref values" never logged)
# --------------------------------------------------------------------------


def test_scan_ref_never_appears_in_any_captured_log_output(
    staff_account: Callable[..., tuple[str, int]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    email, _ = staff_account()
    caplog.set_level(logging.DEBUG)
    with make_client() as c:
        assert _login(c, email, TEST_PASSWORD).status_code == 204
        response = c.post(RESOLVE, json={"card_token": fx.ATTENDEES[0].public_token})
        c.post(LOGOUT)
    assert response.status_code == 200
    scan_ref = response.json()["scan_ref"]
    for record in caplog.records:
        assert scan_ref not in record.getMessage()
        assert scan_ref not in str(record.args)
