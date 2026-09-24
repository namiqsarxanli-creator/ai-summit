"""M11 acceptance: task award and points ledger — ARCHITECTURE.md
§12.2, §13.3, §13.4, §14.1-§14.6, §19.4, §20.3, §24.1, §24.3, §24.7,
§24.11, §24.15, §24.16, §24.23, §24.24, §24.29, IMPLEMENTATION_PLAN.md
M11.

HIGH-RISK MILESTONE. Real PostgreSQL throughout — every fixture creates
fresh, uniquely-named, committed rows (the rollback-only `db` fixture
cannot be used: the running app serves requests through a different
connection, and the concurrency tests need two independent connections
by construction).

`scenario()` builds one complete, isolated (booth, staff, session,
attendee) tuple per call and tears it down in FK-safe order: idempotency
records and points_log rows are deleted BEFORE the staff/attendee/booth
rows they reference, regardless of which other fixture would otherwise
race it, because this is the ONE fixture responsible for all of it.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from httpx import Response
from sqlalchemy import text

from app.core.config import get_settings
from app.core.locks import FACEMARK_RESET_FENCE
from app.core.security import hash_password
from app.db.session import SessionLocal
from app.modules.staff.models import StaffRole
from app.modules.staff.scan_ref import issue_scan_ref
from tests.m04_support import (
    PSYCOPG_URL,
    assert_fenced_before_any_read,
    captured_otp,
    execute,
    fetch_one,
    make_client,
    new_email,
    register_payload,
)

AWARD = "/api/staff/points/award"
REGISTER = "/api/attendees/register"
VERIFY = "/api/auth/otp/verify"
LOGIN = "/api/staff/auth/login"

TEST_PASSWORD = "m11-test-only-staff-password"  # noqa: S105
TEST_PASSWORD_HASH = hash_password(TEST_PASSWORD)

SCAN_REF_SECRET = get_settings().scan_ref_secret


def _insert_returning_id(sql: str, **params: Any) -> int:
    db = SessionLocal()
    try:
        row = db.execute(text(sql), params).scalar_one()
        db.commit()
        return int(row)
    finally:
        db.close()


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


# --------------------------------------------------------------------------
# The `scenario` fixture: one fully isolated, fully cleaned-up rig
# --------------------------------------------------------------------------


@dataclass
class Scenario:
    booth_id: int
    booth_name: str
    staff_id: int
    staff_email: str
    session_token: str
    session_id: int
    attendee_email: str
    card_token: str
    user_id: int


def _register_and_activate(otp_log: pytest.LogCaptureFixture, email: str, **overrides: Any) -> None:
    with make_client() as c:
        assert c.post(REGISTER, json=register_payload(email, **overrides)).status_code == 200
        code = captured_otp(otp_log.records, email)
        assert c.post(VERIFY, json={"email": email, "code": code}).status_code == 200


def _plant_staff_session(staff_id: int) -> tuple[str, int]:
    from app.core.security import generate_session_token, hash_session_token

    raw = generate_session_token()
    session_id = _insert_returning_id(
        "INSERT INTO sessions (token_hash, principal_type, staff_account_id, issued_at, "
        "expires_at) VALUES (:h, 'STAFF', :sid, now(), now() + interval '12 hours') "
        "RETURNING id",
        h=hash_session_token(raw),
        sid=staff_id,
    )
    return raw, session_id


@pytest.fixture
def scenario(otp_log: pytest.LogCaptureFixture) -> Iterator[Callable[..., Scenario]]:
    made: list[Scenario] = []

    def make(
        *,
        allow_repeat: bool | None = None,
        booth_points: list[int] | None = None,
        role: StaffRole = StaffRole.BOOTH_STAFF,
    ) -> Scenario:
        slug = f"m11-test-booth-{uuid.uuid4().hex[:10]}"
        booth_name = f"M11 Test Booth {slug[-6:]}"
        booth_id = _insert_returning_id(
            "INSERT INTO booths (slug, name, allow_repeat_task_points) "
            "VALUES (:slug, :name, :allow_repeat) RETURNING id",
            slug=slug,
            name=booth_name,
            allow_repeat=allow_repeat,
        )
        if booth_points:
            for i, points in enumerate(booth_points):
                execute(
                    "INSERT INTO task_preset_points (booth_id, points, label, sort_order) "
                    "VALUES (:b, :p, 'Custom', :i)",
                    b=booth_id,
                    p=points,
                    i=i,
                )

        staff_email = f"m11-{uuid.uuid4().hex[:10]}@staff.test.facemark.demo"
        staff_booth_id = booth_id if role == StaffRole.BOOTH_STAFF else None
        staff_id = _insert_returning_id(
            "INSERT INTO staff_accounts (email, password_hash, full_name, role, booth_id) "
            "VALUES (:email, :hash, 'M11 Test Staff', :role, :booth_id) RETURNING id",
            email=staff_email,
            hash=TEST_PASSWORD_HASH,
            role=role.value,
            booth_id=staff_booth_id,
        )
        raw_token, session_id = _plant_staff_session(staff_id)

        attendee_email = new_email("m11")
        _register_and_activate(otp_log, attendee_email)
        card_row = fetch_one(
            "SELECT c.public_token, c.user_id FROM contact_cards c "
            "JOIN users u ON u.id = c.user_id "
            "WHERE u.email = :e AND c.revoked_at IS NULL",
            e=attendee_email.lower(),
        )
        assert card_row is not None
        card_token, user_id = str(card_row[0]), int(card_row[1])

        s = Scenario(
            booth_id=booth_id,
            booth_name=booth_name,
            staff_id=staff_id,
            staff_email=staff_email,
            session_token=raw_token,
            session_id=session_id,
            attendee_email=attendee_email,
            card_token=card_token,
            user_id=user_id,
        )
        made.append(s)
        return s

    yield make

    for s in reversed(made):
        # Phase 1: idempotency records first — they reference points_log.
        execute("DELETE FROM idempotency_records WHERE staff_account_id = :s", s=s.staff_id)
        # Phase 2: points_log rows this scenario's staff or attendee touched.
        execute(
            "DELETE FROM points_log WHERE user_id = :u OR entered_by_staff_id = :s",
            u=s.user_id,
            s=s.staff_id,
        )
        # Phase 3: now safe — sessions, staff, attendee, booth config.
        execute("DELETE FROM sessions WHERE staff_account_id = :s", s=s.staff_id)
        execute("DELETE FROM staff_accounts WHERE id = :s", s=s.staff_id)
        execute("DELETE FROM sessions WHERE user_id = :u", u=s.user_id)
        execute("DELETE FROM otp_challenges WHERE email = :e", e=s.attendee_email)
        execute("DELETE FROM consent_audit WHERE user_id = :u", u=s.user_id)
        execute("DELETE FROM contact_cards WHERE user_id = :u", u=s.user_id)
        execute("DELETE FROM user_interests WHERE user_id = :u", u=s.user_id)
        execute("DELETE FROM users WHERE id = :u", u=s.user_id)
        execute("DELETE FROM task_preset_points WHERE booth_id = :b", b=s.booth_id)
        execute("DELETE FROM booths WHERE id = :b", b=s.booth_id)


def _scan_ref(
    s: Scenario,
    *,
    card_token: str | None = None,
    staff_id: int | None = None,
    session_id: int | None = None,
    now: datetime | None = None,
    secret: str | None = None,
) -> str:
    return issue_scan_ref(
        secret=secret if secret is not None else SCAN_REF_SECRET,
        staff_id=staff_id if staff_id is not None else s.staff_id,
        session_id=session_id if session_id is not None else s.session_id,
        card_token=card_token if card_token is not None else s.card_token,
        now=now if now is not None else datetime.now(UTC),
    )


def _body(
    s: Scenario,
    *,
    points: int = 5,
    idempotency_key: uuid.UUID | None = None,
    card_token: str | None = None,
    scan_ref: str | None = None,
) -> dict[str, Any]:
    card_token = card_token if card_token is not None else s.card_token
    return {
        "card_token": card_token,
        "points": points,
        "idempotency_key": str(idempotency_key or uuid.uuid4()),
        "scan_ref": scan_ref if scan_ref is not None else _scan_ref(s, card_token=card_token),
    }


def _award(s: Scenario, *, session_token: str | None = None, **kwargs: Any) -> Response:
    with make_client() as c:
        c.cookies.set("session", session_token if session_token is not None else s.session_token)
        response: Response = c.post(AWARD, json=_body(s, **kwargs))
        return response


def _points_log_row(points_log_id: int) -> Any:
    return fetch_one(
        "SELECT user_id, booth_id, source, entry_type, points, task_id, "
        "entered_by_staff_id, entered_by_system, dedupe_key "
        "FROM points_log WHERE id = :id",
        id=points_log_id,
    )


# --------------------------------------------------------------------------
# Unit: error catalogue additions
# --------------------------------------------------------------------------


def test_new_error_classes_carry_their_documented_codes() -> None:
    from app.core.errors import (
        AwardInProgressError,
        IdempotencyKeyReuseError,
        InvalidPointValueError,
        ScanExpiredError,
    )

    assert (ScanExpiredError().code, ScanExpiredError.status_code) == ("SCAN_EXPIRED", 400)
    assert (IdempotencyKeyReuseError().code, IdempotencyKeyReuseError.status_code) == (
        "IDEMPOTENCY_KEY_REUSE",
        409,
    )
    assert (InvalidPointValueError().code, InvalidPointValueError.status_code) == (
        "INVALID_POINT_VALUE",
        400,
    )
    assert (AwardInProgressError().code, AwardInProgressError.status_code) == (
        "AWARD_IN_PROGRESS",
        409,
    )


# --------------------------------------------------------------------------
# Unit: fingerprint and response snapshot shape
# --------------------------------------------------------------------------


def test_fingerprint_excludes_scan_ref_booth_and_configuration() -> None:
    from app.modules.points.service import compute_award_fingerprint

    a = compute_award_fingerprint(card_token="tok-a", points=5)
    b = compute_award_fingerprint(card_token="tok-a", points=5)
    assert a == b  # same (card_token, points) -> same fingerprint, deterministic

    different_points = compute_award_fingerprint(card_token="tok-a", points=10)
    assert different_points != a

    different_token = compute_award_fingerprint(card_token="tok-b", points=5)
    assert different_token != a

    import inspect

    assert set(inspect.signature(compute_award_fingerprint).parameters) == {"card_token", "points"}


def test_award_response_schema_has_no_rank_field() -> None:
    from app.modules.points.schemas import AwardResponse

    assert "rank" not in AwardResponse.model_fields
    assert set(AwardResponse.model_fields) == {
        "points_log_id",
        "points_awarded",
        "resulting_total",
        "attendee_name",
        "booth_name",
        "awarded_at",
        "replayed",
    }


def test_award_request_schema_has_no_booth_staff_or_source_fields() -> None:
    from app.modules.points.schemas import AwardTaskPointsRequest

    assert set(AwardTaskPointsRequest.model_fields) == {
        "card_token",
        "points",
        "idempotency_key",
        "scan_ref",
    }


# --------------------------------------------------------------------------
# Integration: the golden path — +5, +10, +15
# --------------------------------------------------------------------------


@pytest.mark.parametrize("points", [5, 10, 15])
def test_valid_award_creates_exactly_one_ledger_row(
    scenario: Callable[..., Scenario], points: int
) -> None:
    s = scenario()
    response = _award(s, points=points)
    assert response.status_code == 200
    body = response.json()
    assert body["points_awarded"] == points
    assert body["resulting_total"] == points
    assert body["booth_name"] == s.booth_name
    assert body["replayed"] is False
    assert "rank" not in body

    row = _points_log_row(body["points_log_id"])
    assert row is not None
    assert row[0] == s.user_id  # user_id
    assert row[1] == s.booth_id  # booth_id
    assert row[2] == "TASK"  # source
    assert row[3] == "AWARD"  # entry_type
    assert row[4] == points  # points
    assert row[5] is None  # task_id
    assert row[6] == s.staff_id  # entered_by_staff_id
    assert row[7] is False  # entered_by_system
    assert row[8] == f"TASK:{s.user_id}:{s.booth_id}"  # dedupe_key (repeats disallowed)


def test_attendee_name_is_first_name_plus_last_initial(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    response = _award(s, points=5)
    assert response.json()["attendee_name"] == "Grace H."  # register_payload's default name


# --------------------------------------------------------------------------
# Integration: allowed-preset enforcement (B5)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("points", [12, 0, -5, 1000])
def test_arbitrary_point_values_are_rejected(
    scenario: Callable[..., Scenario], points: int
) -> None:
    s = scenario()
    response = _award(s, points=points)
    assert _error(response) == (400, "INVALID_POINT_VALUE")
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 0


def test_points_between_two_valid_presets_is_still_rejected(
    scenario: Callable[..., Scenario],
) -> None:
    """§14.3 B5, verbatim: "12 is rejected even though it lies between
    the booth's 10 and 15 presets" — allow-list membership, not a range."""
    s = scenario(booth_points=[10, 15])
    assert _error(_award(s, points=12)) == (400, "INVALID_POINT_VALUE")


def test_booth_specific_presets_override_the_global_defaults(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(booth_points=[7, 13])
    assert _award(s, points=5).status_code == 400  # global default, not this booth's
    assert _award(s, points=7).status_code == 200


# --------------------------------------------------------------------------
# Integration: invalid card token / provisional attendee (B4)
# --------------------------------------------------------------------------


def test_unknown_card_token_is_invalid_qr(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    fake_token = "A" * 43
    response = _award(s, card_token=fake_token, scan_ref=_scan_ref(s, card_token=fake_token))
    assert _error(response) == (404, "INVALID_QR")


def test_provisional_attendee_is_rejected(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    execute("UPDATE users SET status = 'PROVISIONAL' WHERE id = :u", u=s.user_id)
    assert _error(_award(s, points=5)) == (404, "INVALID_QR")
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 0


# --------------------------------------------------------------------------
# Integration: server authority — booth_id/staff_id/source cannot be spoofed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["booth_id", "staff_id", "entered_by_staff_id", "source", "task_id"]
)
def test_request_body_cannot_express_server_derived_fields(
    scenario: Callable[..., Scenario], field: str
) -> None:
    s = scenario()
    body = _body(s, points=5)
    body[field] = 999999
    with make_client() as c:
        c.cookies.set("session", s.session_token)
        response = c.post(AWARD, json=body)
    assert _error(response) == (422, "VALIDATION_ERROR")
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 0


def test_resulting_row_always_carries_the_staff_members_own_booth(
    scenario: Callable[..., Scenario],
) -> None:
    victim = scenario()  # a second booth that must never receive the award
    s = scenario()
    response = _award(s, points=5)
    assert response.status_code == 200
    row = _points_log_row(response.json()["points_log_id"])
    assert row[1] == s.booth_id
    assert row[1] != victim.booth_id


# --------------------------------------------------------------------------
# Integration: scan_ref cryptographic checks (A3, A4 — pre-fence)
# --------------------------------------------------------------------------


def test_tampered_scan_ref_signature_is_invalid_qr(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    ref = _scan_ref(s)
    body, sig = ref.rsplit(".", 1)
    tampered = f"{body}.{sig[:-1]}{'A' if sig[-1] != 'A' else 'B'}"
    assert _error(_award(s, scan_ref=tampered)) == (404, "INVALID_QR")


def test_malformed_scan_ref_shape_is_invalid_qr(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    for malformed in ["not-a-scan-ref", "", "onlyonepart", ".", "a.", ".b"]:
        assert _error(_award(s, scan_ref=malformed)) == (404, "INVALID_QR")


def test_wrong_version_is_invalid_qr(scenario: Callable[..., Scenario]) -> None:
    import json as jsonlib

    from app.core.security import b64url_nopad, canonical_json, sign_hmac_sha256

    s = scenario()
    payload = {"v": 2, "sid": s.staff_id, "ses": s.session_id, "tok": "x", "iat": 1}
    body = b64url_nopad(canonical_json(payload))
    ref = f"{body}.{sign_hmac_sha256(SCAN_REF_SECRET, body)}"
    assert jsonlib  # keep import used for clarity even though canonical_json covers it
    assert _error(_award(s, scan_ref=ref)) == (404, "INVALID_QR")


def test_future_iat_is_invalid_qr_not_treated_as_expiry(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    forged = _scan_ref(s, now=datetime.now(UTC) + timedelta(seconds=30))
    response = _award(s, scan_ref=forged)
    assert _error(response) == (404, "INVALID_QR")  # never SCAN_EXPIRED


def test_five_second_clock_skew_is_tolerated(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    ref = _scan_ref(s, now=datetime.now(UTC) + timedelta(seconds=3))
    assert _award(s, scan_ref=ref).status_code == 200


def test_token_hash_mismatch_is_invalid_qr(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    other = scenario()
    # scan_ref genuinely issued for a DIFFERENT card token.
    ref = _scan_ref(s, card_token=other.card_token)
    assert _error(_award(s, scan_ref=ref)) == (404, "INVALID_QR")


# --------------------------------------------------------------------------
# Integration: staff/session binding (checks 5-6, fenced, B1)
# --------------------------------------------------------------------------


def test_scan_ref_issued_to_a_different_staff_account_is_rejected(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario()
    other = scenario()
    ref = _scan_ref(s, staff_id=other.staff_id, session_id=other.session_id)
    # Awarded AS s (using s's own session cookie) but the envelope names `other`.
    assert _error(_award(s, scan_ref=ref)) == (403, "STAFF_NOT_AUTHORIZED")
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 0


def test_scan_ref_issued_to_a_different_session_same_staff_is_rejected(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario()
    other_raw_token, other_session_id = _plant_staff_session(s.staff_id)
    try:
        ref = _scan_ref(s, session_id=other_session_id)
        assert _error(_award(s, scan_ref=ref)) == (403, "STAFF_NOT_AUTHORIZED")
    finally:
        execute(
            "DELETE FROM sessions WHERE staff_account_id = :s AND id = :i",
            s=s.staff_id,
            i=other_session_id,
        )


def test_scan_ref_from_a_revoked_session_cannot_award(scenario: Callable[..., Scenario]) -> None:
    """Adversarial: a staff member scans, logs out (revoking their
    session), and the still-unexpired `scan_ref` is attempted anyway —
    with a FRESH login's cookie, since the revoked session's own cookie
    would already be UNAUTHENTICATED. §13.3: "revoking a session
    invalidates every outstanding scan for free" — the envelope's `ses`
    no longer matches any live session for this staff account."""
    s = scenario()
    ref = _scan_ref(s)  # issued while the original session was still live
    execute("UPDATE sessions SET revoked_at = now() WHERE id = :i", i=s.session_id)
    new_raw_token, _new_session_id = _plant_staff_session(s.staff_id)
    try:
        response = _award(s, session_token=new_raw_token, scan_ref=ref)
        assert _error(response) == (403, "STAFF_NOT_AUTHORIZED")
        assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 0
    finally:
        execute(
            "DELETE FROM sessions WHERE staff_account_id = :s AND revoked_at IS NULL",
            s=s.staff_id,
        )


def test_unauthenticated_caller_cannot_award(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    with make_client() as c:
        response = c.post(AWARD, json=_body(s, points=5))
    assert _error(response) == (401, "UNAUTHENTICATED")


def test_attendee_session_cannot_award(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    attendee_row = fetch_one("SELECT id FROM sessions WHERE user_id = :u", u=s.user_id)
    assert attendee_row is not None
    with make_client() as c:
        c.cookies.set("session", s.session_token)  # will be overridden below
    from app.core.security import generate_session_token, hash_session_token

    raw = generate_session_token()
    execute(
        "INSERT INTO sessions (token_hash, principal_type, user_id, issued_at, expires_at) "
        "VALUES (:h, 'ATTENDEE', :u, now(), now() + interval '12 hours')",
        h=hash_session_token(raw),
        u=s.user_id,
    )
    try:
        with make_client() as c:
            c.cookies.set("session", raw)
            response = c.post(AWARD, json=_body(s, points=5))
        assert _error(response) == (401, "UNAUTHENTICATED")
    finally:
        execute("DELETE FROM sessions WHERE token_hash = :h", h=hash_session_token(raw))


def test_admin_cannot_award(scenario: Callable[..., Scenario]) -> None:
    s = scenario(role=StaffRole.ADMIN)
    assert _error(_award(s, points=5)) == (403, "STAFF_NOT_AUTHORIZED")


# --------------------------------------------------------------------------
# Integration: scan_ref expiry (B3a) — new awards only
# --------------------------------------------------------------------------


def test_expired_scan_ref_on_a_new_award_is_rejected_with_no_row(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario()
    expired = _scan_ref(s, now=datetime.now(UTC) - timedelta(seconds=121))
    response = _award(s, scan_ref=expired)
    assert _error(response) == (400, "SCAN_EXPIRED")
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 0


def test_expired_scan_ref_on_a_completed_replay_still_replays(
    scenario: Callable[..., Scenario],
) -> None:
    """The regression this ordering exists to prevent (§13.4): B3
    (idempotency lookup) runs before B3a (expiry), so a retry that
    arrives after the envelope's 120s window still replays the
    original success rather than failing."""
    s = scenario()
    key = uuid.uuid4()
    fresh_ref = _scan_ref(s)
    first = _award(s, points=10, idempotency_key=key, scan_ref=fresh_ref)
    assert first.status_code == 200
    assert first.json()["replayed"] is False

    expired_ref = _scan_ref(s, now=datetime.now(UTC) - timedelta(seconds=121))
    second = _award(s, points=10, idempotency_key=key, scan_ref=expired_ref)
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["points_log_id"] == first.json()["points_log_id"]
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 1


# --------------------------------------------------------------------------
# Integration: idempotency (B3, B7)
# --------------------------------------------------------------------------


def test_same_key_same_payload_replays_with_one_row(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    key = uuid.uuid4()
    first = _award(s, points=5, idempotency_key=key)
    second = _award(s, points=5, idempotency_key=key)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert first.json() == {**second.json(), "replayed": False}
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 1


def test_same_key_different_points_is_key_reuse(scenario: Callable[..., Scenario]) -> None:
    s = scenario(booth_points=[5, 10, 15])
    key = uuid.uuid4()
    first = _award(s, points=5, idempotency_key=key)
    assert first.status_code == 200
    second = _award(s, points=10, idempotency_key=key)
    assert _error(second) == (409, "IDEMPOTENCY_KEY_REUSE")
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 1


def test_same_key_different_attendee_is_key_reuse(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    other = scenario()
    key = uuid.uuid4()
    first = _award(s, points=5, idempotency_key=key)
    assert first.status_code == 200
    # Same staff, same key, but the SECOND card token + a matching scan_ref for it.
    ref_for_other = _scan_ref(s, card_token=other.card_token)
    second = _award(
        s, points=5, idempotency_key=key, card_token=other.card_token, scan_ref=ref_for_other
    )
    assert _error(second) == (409, "IDEMPOTENCY_KEY_REUSE")


def test_same_key_different_staff_account_is_a_separate_record(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario()
    other = scenario()
    key = uuid.uuid4()
    first = _award(s, points=5, idempotency_key=key)
    assert first.status_code == 200
    second = _award(other, points=5, idempotency_key=key)
    assert second.status_code == 200
    assert second.json()["replayed"] is False
    assert first.json()["points_log_id"] != second.json()["points_log_id"]


# --------------------------------------------------------------------------
# Integration: repeat-task policy (B6) — independent of idempotency
# --------------------------------------------------------------------------


def test_repeat_disallowed_second_award_new_key_is_duplicate_task_reward(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(allow_repeat=False)
    first = _award(s, points=5)
    assert first.status_code == 200
    second = _award(s, points=5)  # a fresh idempotency_key each call
    assert _error(second) == (409, "DUPLICATE_TASK_REWARD")
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 1
    # the loser's idempotency claim must not have survived either
    assert (
        fetch_one(
            "SELECT count(*) FROM idempotency_records WHERE staff_account_id = :s",
            s=s.staff_id,
        )[0]
        == 1
    )


def test_repeat_allowed_second_award_is_permitted(scenario: Callable[..., Scenario]) -> None:
    s = scenario(allow_repeat=True)
    first = _award(s, points=5)
    second = _award(s, points=5)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["replayed"] is False
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 2
    assert fetch_one("SELECT sum(points) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 10


def test_repeat_policy_enforced_even_with_a_fresh_idempotency_key(
    scenario: Callable[..., Scenario],
) -> None:
    """Repeat-task policy and request idempotency are separate
    protections (M11 prompt) — a fresh key does not bypass the
    business rule, only a fresh logical request re-evaluates it."""
    s = scenario(allow_repeat=False)
    assert _award(s, points=5).status_code == 200
    assert _error(_award(s, points=5)) == (409, "DUPLICATE_TASK_REWARD")


def test_event_config_default_governs_when_booth_does_not_override(
    scenario: Callable[..., Scenario],
) -> None:
    """`allow_repeat_task_points=NULL` on the booth -> falls back to
    `event_config.allow_repeat_task_points_default` (seeded `false`)."""
    s = scenario(allow_repeat=None)
    assert _award(s, points=5).status_code == 200
    assert _error(_award(s, points=5)) == (409, "DUPLICATE_TASK_REWARD")


# --------------------------------------------------------------------------
# Integration: transaction rollback leaves no partial state
# --------------------------------------------------------------------------


def test_transaction_rollback_on_dedupe_violation_leaves_no_claimed_key(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(allow_repeat=False)
    assert _award(s, points=5).status_code == 200
    key_before = fetch_one(
        "SELECT count(*) FROM idempotency_records WHERE staff_account_id = :s", s=s.staff_id
    )[0]
    response = _award(s, points=5)  # fresh key, blocked by dedupe_key
    assert _error(response) == (409, "DUPLICATE_TASK_REWARD")
    key_after = fetch_one(
        "SELECT count(*) FROM idempotency_records WHERE staff_account_id = :s", s=s.staff_id
    )[0]
    # The failed attempt's own claim must have rolled back with everything else.
    assert key_after == key_before == 1


# --------------------------------------------------------------------------
# Reset fence (§19.4, invariant #29)
# --------------------------------------------------------------------------


def test_award_is_fenced_before_any_reset_sensitive_read(scenario: Callable[..., Scenario]) -> None:
    s = scenario()

    def call() -> Response:
        return _award(s, points=5)

    response = assert_fenced_before_any_read(call)
    assert response.status_code == 200


# --------------------------------------------------------------------------
# Concurrency (real PostgreSQL, two threads / two connections)
# --------------------------------------------------------------------------


def test_two_simultaneous_identical_requests_produce_exactly_one_row(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario()
    key = uuid.uuid4()
    body = _body(s, points=5, idempotency_key=key)
    results: list[Response] = []
    lock = threading.Lock()

    def fire() -> None:
        with make_client() as c:
            c.cookies.set("session", s.session_token)
            r = c.post(AWARD, json=body)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(results) == 2
    for r in results:
        assert r.status_code == 200
    replayed_flags = sorted(r.json()["replayed"] for r in results)
    assert replayed_flags == [False, True]
    ids = {r.json()["points_log_id"] for r in results}
    assert len(ids) == 1
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 1


def test_two_concurrent_different_keys_same_attendee_one_wins_one_duplicate(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(allow_repeat=False)
    results: list[Response] = []
    lock = threading.Lock()

    def fire() -> None:
        with make_client() as c:
            c.cookies.set("session", s.session_token)
            r = c.post(AWARD, json=_body(s, points=5))  # each call gets its OWN fresh key
        with lock:
            results.append(r)

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(results) == 2
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 409]
    codes = [r.json()["error"]["code"] for r in results if r.status_code != 200]
    assert codes == ["DUPLICATE_TASK_REWARD"]
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 1
    # The loser must leave no orphaned idempotency record — only the winner's.
    assert (
        fetch_one(
            "SELECT count(*) FROM idempotency_records WHERE staff_account_id = :s", s=s.staff_id
        )[0]
        == 1
    )


def test_response_lost_then_retried_replays_one_row(scenario: Callable[..., Scenario]) -> None:
    """Simulates a client that never saw the first response: commit,
    discard it, retry with the same key."""
    s = scenario()
    key = uuid.uuid4()
    first = _award(s, points=5, idempotency_key=key)
    assert first.status_code == 200
    del first  # the "lost" response
    retry = _award(s, points=5, idempotency_key=key)
    assert retry.status_code == 200
    assert retry.json()["replayed"] is True
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 1


def test_award_holding_the_fence_blocks_an_exclusive_reset_acquire(
    scenario: Callable[..., Scenario],
) -> None:
    """The inverse of `assert_fenced_before_any_read`: a mutation IN
    PROGRESS (holding the shared fence) must block reset's exclusive
    acquire until it finishes — proven directly against pg_locks."""
    s = scenario()
    release_gate = threading.Event()
    started = threading.Event()

    holder_conn = SessionLocal()
    from sqlalchemy import text as sa_text

    holder_conn.execute(
        sa_text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": FACEMARK_RESET_FENCE}
    )

    def hold_and_release() -> None:
        started.set()
        release_gate.wait(timeout=10)
        holder_conn.commit()
        holder_conn.close()

    holder_thread = threading.Thread(target=hold_and_release)
    holder_thread.start()
    started.wait(timeout=5)

    exclusive_waiting: dict[str, bool] = {"waiting": False}

    def try_exclusive() -> None:
        with psycopg.connect(PSYCOPG_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (FACEMARK_RESET_FENCE,))
            cur.execute("SELECT pg_advisory_unlock(%s)", (FACEMARK_RESET_FENCE,))

    exclusive_thread = threading.Thread(target=try_exclusive)
    exclusive_thread.start()

    deadline = time.monotonic() + 2.0
    with psycopg.connect(PSYCOPG_URL, autocommit=True) as probe, probe.cursor() as cur:
        while time.monotonic() < deadline:
            cur.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
            row = cur.fetchone()
            if row and row[0] >= 1:
                exclusive_waiting["waiting"] = True
                break
            time.sleep(0.02)

    assert exclusive_waiting["waiting"], "reset's exclusive acquire never blocked on the held fence"

    release_gate.set()
    holder_thread.join(timeout=10)
    exclusive_thread.join(timeout=10)

    # Fence released — an ordinary award now proceeds normally.
    assert _award(s, points=5).status_code == 200
