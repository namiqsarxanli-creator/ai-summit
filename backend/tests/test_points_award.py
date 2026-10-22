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
from app.core.errors import TaskRuleChangedError
from app.core.locks import FACEMARK_RESET_FENCE
from app.core.security import hash_password
from app.db.session import SessionLocal
from app.modules.points.schemas import AwardTaskPointsRequest
from app.modules.points.service import _replay_or_raise, compute_award_fingerprint
from app.modules.staff.models import StaffRole
from app.modules.staff.scan_ref import issue_scan_ref
from tests.m04_support import (
    ADVISORY_LOCKS_HERE,
    PSYCOPG_URL,
    assert_fenced_before_any_read,
    captured_otp,
    execute,
    fetch_one,
    make_client,
    new_email,
    register_payload,
    rule_revision_in_force,
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
        row: Any = db.execute(text(sql), params).scalar_one()
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
    task_id: int  # the booth's main activity (points = task_points)
    second_task_id: int  # another active activity of the same booth, worth 15


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
        task_points: int = 10,
        task_repeat: bool | None = None,
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

        task_id = _insert_returning_id(
            "INSERT INTO tasks (booth_id, title, points, allow_repeat_points, sort_order) "
            "VALUES (:b, 'Main activity', :p, :r, 0) RETURNING id",
            b=booth_id,
            p=task_points,
            r=task_repeat,
        )
        second_task_id = _insert_returning_id(
            "INSERT INTO tasks (booth_id, title, points, sort_order) "
            "VALUES (:b, 'Second activity', 15, 1) RETURNING id",
            b=booth_id,
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
            task_id=task_id,
            second_task_id=second_task_id,
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
        execute("DELETE FROM tasks WHERE booth_id = :b", b=s.booth_id)
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
    task_id: int | None = None,
    idempotency_key: uuid.UUID | None = None,
    card_token: str | None = None,
    scan_ref: str | None = None,
    rule_revision: str | None = None,
) -> dict[str, Any]:
    """The request a current scanner sends. `rule_revision` defaults to the
    rule in force right now (what the scanner would have listed); pass one
    captured earlier to model a screen confirmed BEFORE the rule changed."""
    card_token = card_token if card_token is not None else s.card_token
    task_id = task_id if task_id is not None else s.task_id
    return {
        "card_token": card_token,
        "task_id": task_id,
        "rule_revision": (
            rule_revision if rule_revision is not None else rule_revision_in_force(task_id)
        ),
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

    a = compute_award_fingerprint(card_token="tok-a", task_id=5)
    b = compute_award_fingerprint(card_token="tok-a", task_id=5)
    assert a == b  # same (card_token, activity) -> same fingerprint, deterministic

    assert compute_award_fingerprint(card_token="tok-a", task_id=6) != a  # another activity
    assert compute_award_fingerprint(card_token="tok-b", task_id=5) != a  # another attendee

    import inspect

    assert set(inspect.signature(compute_award_fingerprint).parameters) == {"card_token", "task_id"}


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
        "task_id",
        "task_title",
    }


def test_award_request_schema_has_no_booth_staff_or_source_fields() -> None:
    from app.modules.points.schemas import AwardTaskPointsRequest

    assert set(AwardTaskPointsRequest.model_fields) == {
        "card_token",
        "task_id",
        "rule_revision",
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
    s = scenario(task_points=points)
    response = _award(s)
    assert response.status_code == 200
    body = response.json()
    assert body["points_awarded"] == points  # the activity's rule, not a client value
    assert body["resulting_total"] == points
    assert body["booth_name"] == s.booth_name
    assert body["task_id"] == s.task_id and body["task_title"] == "Main activity"
    assert body["replayed"] is False
    assert "rank" not in body

    row = _points_log_row(body["points_log_id"])
    assert row is not None
    assert row[0] == s.user_id  # user_id
    assert row[1] == s.booth_id  # booth_id
    assert row[2] == "TASK"  # source
    assert row[3] == "AWARD"  # entry_type
    assert row[4] == points  # points
    assert row[5] == s.task_id  # task_id - every new award names its activity
    assert row[6] == s.staff_id  # entered_by_staff_id
    assert row[7] is False  # entered_by_system
    assert row[8] == f"TASK:{s.user_id}:task:{s.task_id}"  # dedupe_key (repeats disallowed)


def test_attendee_name_is_first_name_plus_last_initial(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    response = _award(s)
    assert response.json()["attendee_name"] == "Grace H."  # register_payload's default name


# --------------------------------------------------------------------------
# Integration: allowed-preset enforcement (B5)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("points", [12, 0, -5, 1000, 10])
def test_the_client_cannot_send_a_point_value_at_all(
    scenario: Callable[..., Scenario], points: int
) -> None:
    """The value is the activity's server-side rule. A `points` field -
    even one equal to the real value - marks an out-of-date page and is
    refused as an outdated page; nothing is awarded."""
    s = scenario()
    body = _body(s)
    body["points"] = points
    with make_client() as c:
        c.cookies.set("session", s.session_token)
        response = c.post(AWARD, json=body)
    assert _error(response) == (403, "CSRF_REJECTED")
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 0


def test_legacy_booth_presets_no_longer_influence_awards(
    scenario: Callable[..., Scenario],
) -> None:
    """Booth presets (7 and 13) are leftovers of the old flow: the award is
    worth exactly what the activity says."""
    s = scenario(booth_points=[7, 13], task_points=20)
    response = _award(s)
    assert response.status_code == 200
    assert response.json()["points_awarded"] == 20


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
    assert _error(_award(s)) == (404, "INVALID_QR")
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 0


# --------------------------------------------------------------------------
# Integration: server authority — booth_id/staff_id/source cannot be spoofed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["booth_id", "staff_id", "entered_by_staff_id", "source", "created_at"]
)
def test_request_body_cannot_express_server_derived_fields(
    scenario: Callable[..., Scenario], field: str
) -> None:
    s = scenario()
    body = _body(s)
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
    response = _award(s)
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
        response = c.post(AWARD, json=_body(s))
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
            response = c.post(AWARD, json=_body(s))
        assert _error(response) == (401, "UNAUTHENTICATED")
    finally:
        execute("DELETE FROM sessions WHERE token_hash = :h", h=hash_session_token(raw))


def test_admin_cannot_award(scenario: Callable[..., Scenario]) -> None:
    s = scenario(role=StaffRole.ADMIN)
    assert _error(_award(s)) == (403, "STAFF_NOT_AUTHORIZED")


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
    first = _award(s, idempotency_key=key, scan_ref=fresh_ref)
    assert first.status_code == 200
    assert first.json()["replayed"] is False

    expired_ref = _scan_ref(s, now=datetime.now(UTC) - timedelta(seconds=121))
    second = _award(s, idempotency_key=key, scan_ref=expired_ref)
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
    first = _award(s, idempotency_key=key)
    second = _award(s, idempotency_key=key)
    assert first.status_code == 200, first.text
    assert second.status_code == 200
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert first.json() == {**second.json(), "replayed": False}
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 1


def test_same_key_different_activity_is_key_reuse(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    key = uuid.uuid4()
    first = _award(s, idempotency_key=key)
    assert first.status_code == 200
    second = _award(s, idempotency_key=key, task_id=s.second_task_id)
    assert _error(second) == (409, "IDEMPOTENCY_KEY_REUSE")
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 1


def test_same_key_different_attendee_is_key_reuse(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    other = scenario()
    key = uuid.uuid4()
    first = _award(s, idempotency_key=key)
    assert first.status_code == 200
    # Same staff, same key, but the SECOND card token + a matching scan_ref for it.
    ref_for_other = _scan_ref(s, card_token=other.card_token)
    second = _award(s, idempotency_key=key, card_token=other.card_token, scan_ref=ref_for_other)
    assert _error(second) == (409, "IDEMPOTENCY_KEY_REUSE")


def test_same_key_different_staff_account_is_a_separate_record(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario()
    other = scenario()
    key = uuid.uuid4()
    first = _award(s, idempotency_key=key)
    assert first.status_code == 200
    second = _award(other, idempotency_key=key)
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
    first = _award(s)
    assert first.status_code == 200
    second = _award(s)  # a fresh idempotency_key each call
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
    first = _award(s)
    second = _award(s)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["replayed"] is False
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 2
    assert fetch_one("SELECT sum(points) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 20


def test_repeat_policy_enforced_even_with_a_fresh_idempotency_key(
    scenario: Callable[..., Scenario],
) -> None:
    """Repeat-task policy and request idempotency are separate
    protections (M11 prompt) — a fresh key does not bypass the
    business rule, only a fresh logical request re-evaluates it."""
    s = scenario(allow_repeat=False)
    assert _award(s).status_code == 200
    assert _error(_award(s)) == (409, "DUPLICATE_TASK_REWARD")


def test_event_config_default_governs_when_booth_does_not_override(
    scenario: Callable[..., Scenario],
) -> None:
    """`allow_repeat_task_points=NULL` on the booth -> falls back to
    `event_config.allow_repeat_task_points_default` (seeded `false`)."""
    s = scenario(allow_repeat=None)
    assert _award(s).status_code == 200
    assert _error(_award(s)) == (409, "DUPLICATE_TASK_REWARD")


# --------------------------------------------------------------------------
# Integration: transaction rollback leaves no partial state
# --------------------------------------------------------------------------


def test_transaction_rollback_on_dedupe_violation_leaves_no_claimed_key(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(allow_repeat=False)
    assert _award(s).status_code == 200
    key_before = fetch_one(
        "SELECT count(*) FROM idempotency_records WHERE staff_account_id = :s", s=s.staff_id
    )[0]
    response = _award(s)  # fresh key, blocked by dedupe_key
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
        return _award(s)

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
    body = _body(s, idempotency_key=key)
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
            r = c.post(AWARD, json=_body(s))  # each call gets its OWN fresh key
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
    first = _award(s, idempotency_key=key)
    assert first.status_code == 200
    del first  # the "lost" response
    retry = _award(s, idempotency_key=key)
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
            cur.execute(
                f"SELECT count(*) FROM pg_locks WHERE {ADVISORY_LOCKS_HERE} AND NOT granted"
            )
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
    assert _award(s).status_code == 200


# ==========================================================================
# Activities (tasks) as the unit of award: server-side point rule, repeat
# policy per (attendee, activity), idempotency, concurrency, legacy rows
# ==========================================================================


def _ledger(s: Scenario) -> list[tuple[int | None, int, str | None]]:
    """(task_id, points, dedupe_key) of every row for the scenario's attendee."""
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                "SELECT task_id, points, dedupe_key FROM points_log WHERE user_id = :u ORDER BY id"
            ),
            {"u": s.user_id},
        ).all()
        return [(r[0], r[1], r[2]) for r in rows]
    finally:
        db.close()


def test_two_different_activities_of_one_booth_both_award(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(task_points=10)
    first = _award(s)
    second = _award(s, task_id=s.second_task_id)

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["points_awarded"] == 10
    assert second.json()["points_awarded"] == 15  # the second activity's own rule
    assert second.json()["resulting_total"] == 25
    assert second.json()["task_title"] == "Second activity"
    assert _ledger(s) == [
        (s.task_id, 10, f"TASK:{s.user_id}:task:{s.task_id}"),
        (s.second_task_id, 15, f"TASK:{s.user_id}:task:{s.second_task_id}"),
    ]


def test_the_same_activity_cannot_be_awarded_twice_by_default(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario()  # nothing overrides the repeat policy anywhere
    assert _award(s).status_code == 200
    again = _award(s)  # a brand-new idempotency key: a genuinely new request
    assert _error(again) == (409, "DUPLICATE_TASK_REWARD")
    assert len(_ledger(s)) == 1
    # ...while the booth's OTHER activity is still open to the same attendee.
    assert _award(s, task_id=s.second_task_id).status_code == 200


def test_an_activity_that_allows_repeats_can_be_earned_again(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(task_repeat=True, task_points=7)
    assert _award(s).status_code == 200
    assert _award(s).status_code == 200
    assert _award(s).status_code == 200
    assert [row[1] for row in _ledger(s)] == [7, 7, 7]
    assert all(row[2] is None for row in _ledger(s))  # no dedupe key => repeats are legal


def test_the_activity_setting_beats_the_booth_setting_both_ways(
    scenario: Callable[..., Scenario],
) -> None:
    # booth allows repeats, but this activity forbids them
    forbids = scenario(allow_repeat=True, task_repeat=False)
    assert _award(forbids).status_code == 200
    assert _error(_award(forbids)) == (409, "DUPLICATE_TASK_REWARD")
    # booth forbids repeats, but this activity allows them
    allows = scenario(allow_repeat=False, task_repeat=True)
    assert _award(allows).status_code == 200
    assert _award(allows).status_code == 200
    # activity inherits the booth's setting
    inherits = scenario(allow_repeat=True, task_repeat=None)
    assert _award(inherits).status_code == 200
    assert _award(inherits).status_code == 200


def test_the_event_default_applies_when_neither_activity_nor_booth_decide(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(allow_repeat=None, task_repeat=None)
    execute("UPDATE event_config SET allow_repeat_task_points_default = true WHERE id = 1")
    try:
        assert _award(s).status_code == 200
        assert _award(s).status_code == 200
    finally:
        execute("UPDATE event_config SET allow_repeat_task_points_default = false WHERE id = 1")
    # back to the built-in default (once): the earlier repeats are history, a third is refused
    assert _error(_award(s)) == (409, "DUPLICATE_TASK_REWARD")


def test_tightening_the_policy_stops_further_repeats_without_touching_old_rows(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(task_repeat=True)
    assert _award(s).status_code == 200
    assert _award(s).status_code == 200
    before = _ledger(s)

    execute("UPDATE tasks SET allow_repeat_points = false WHERE id = :t", t=s.task_id)

    assert _error(_award(s)) == (409, "DUPLICATE_TASK_REWARD")
    assert _ledger(s) == before  # the two earlier awards are untouched


def test_another_booths_activity_is_refused_exactly_like_a_missing_one(
    scenario: Callable[..., Scenario],
) -> None:
    mine = scenario()
    theirs = scenario()
    foreign = _award(mine, task_id=theirs.task_id)
    missing = _award(mine, task_id=999_999_999)

    assert _error(foreign) == (404, "TASK_NOT_FOUND")
    assert _error(missing) == (404, "TASK_NOT_FOUND")  # no way to probe other booths
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=mine.user_id)[0] == 0
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=theirs.user_id)[0] == 0


def test_a_deactivated_activity_cannot_be_awarded(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    execute("UPDATE tasks SET is_active = false WHERE id = :t", t=s.task_id)
    assert _error(_award(s)) == (409, "TASK_INACTIVE")
    assert _ledger(s) == []
    assert _award(s, task_id=s.second_task_id).status_code == 200  # others are unaffected


def test_an_activity_without_a_point_value_cannot_be_awarded(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario()
    execute("UPDATE tasks SET points = NULL WHERE id = :t", t=s.task_id)
    assert _error(_award(s)) == (409, "TASK_NOT_CONFIGURED")
    assert _ledger(s) == []


@pytest.mark.parametrize("bad", [0, -1, "abc", 1.5])
def test_a_malformed_activity_id_is_a_validation_error(
    scenario: Callable[..., Scenario], bad: Any
) -> None:
    s = scenario()
    body = _body(s)
    body["task_id"] = bad
    with make_client() as c:
        c.cookies.set("session", s.session_token)
        response = c.post(AWARD, json=body)
    assert _error(response) == (422, "VALIDATION_ERROR")
    assert _ledger(s) == []


def test_a_missing_activity_id_is_a_validation_error(scenario: Callable[..., Scenario]) -> None:
    """The old request shape (a point value, no activity) is not accepted
    and is recognised as an out-of-date page."""
    s = scenario()
    body = _body(s)
    del body["task_id"]
    del body["rule_revision"]
    body["points"] = 10
    with make_client() as c:
        c.cookies.set("session", s.session_token)
        assert _error(c.post(AWARD, json=body)) == (403, "CSRF_REJECTED")
    assert _ledger(s) == []


def test_the_award_value_follows_the_activity_rule_at_award_time(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(task_points=10)
    first = _award(s)
    execute("UPDATE tasks SET points = 40, allow_repeat_points = true WHERE id = :t", t=s.task_id)
    second = _award(s)

    assert (first.json()["points_awarded"], second.json()["points_awarded"]) == (10, 40)
    assert [row[1] for row in _ledger(s)] == [10, 40]  # the first row did not change


# --- idempotency with activities ---------------------------------------------


def test_replay_after_the_activity_was_deactivated_returns_the_original_result(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(task_points=10)
    key = uuid.uuid4()
    first = _award(s, idempotency_key=key)
    assert first.status_code == 200

    execute("UPDATE tasks SET is_active = false, points = 99 WHERE id = :t", t=s.task_id)
    replay = _award(s, idempotency_key=key)

    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["points_awarded"] == 10  # the original, not the edited rule
    assert replay.json()["task_id"] == s.task_id
    assert {**replay.json(), "replayed": False} == first.json()
    assert len(_ledger(s)) == 1


def test_same_key_and_activity_but_a_different_attendee_is_key_reuse(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario()
    other = scenario()
    key = uuid.uuid4()
    assert _award(s, idempotency_key=key).status_code == 200
    reused = _award(
        s,
        idempotency_key=key,
        card_token=other.card_token,
        scan_ref=_scan_ref(s, card_token=other.card_token),
    )
    assert _error(reused) == (409, "IDEMPOTENCY_KEY_REUSE")


def test_a_failed_attempt_does_not_burn_its_key(scenario: Callable[..., Scenario]) -> None:
    """A rejected award (activity switched off) leaves no idempotency record,
    so the same key succeeds once the activity is back."""
    s = scenario()
    key = uuid.uuid4()
    execute("UPDATE tasks SET is_active = false WHERE id = :t", t=s.task_id)
    assert _error(_award(s, idempotency_key=key)) == (409, "TASK_INACTIVE")
    execute("UPDATE tasks SET is_active = true WHERE id = :t", t=s.task_id)
    retry = _award(s, idempotency_key=key)
    assert retry.status_code == 200 and retry.json()["replayed"] is False


# --- concurrency ----------------------------------------------------------------


def _fire_all(s: Scenario, bodies: list[dict[str, Any]]) -> list[Response]:
    results: list[Response] = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(bodies))

    def fire(body: dict[str, Any]) -> None:
        with make_client() as c:
            c.cookies.set("session", s.session_token)
            barrier.wait(timeout=10)
            r = c.post(AWARD, json=body)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=fire, args=(b,)) for b in bodies]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == len(bodies)
    return results


def test_many_parallel_awards_of_one_activity_pay_out_exactly_once(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario()
    results = _fire_all(s, [_body(s) for _ in range(8)])  # 8 different keys, same activity

    statuses = sorted(r.status_code for r in results)
    assert statuses == [200] + [409] * 7
    assert {r.json()["error"]["code"] for r in results if r.status_code == 409} == {
        "DUPLICATE_TASK_REWARD"
    }
    assert len(_ledger(s)) == 1
    assert fetch_one("SELECT sum(points) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 10
    assert (
        fetch_one(
            "SELECT count(*) FROM idempotency_records WHERE staff_account_id = :s", s=s.staff_id
        )[0]
        == 1
    )


def test_parallel_awards_of_different_activities_all_succeed(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario()
    results = _fire_all(s, [_body(s), _body(s, task_id=s.second_task_id)])

    assert [r.status_code for r in results] == [200, 200]
    assert sorted(row[0] or 0 for row in _ledger(s)) == sorted([s.task_id, s.second_task_id])
    assert fetch_one("SELECT sum(points) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 25


def test_parallel_requests_with_one_key_produce_one_row(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    body = _body(s)
    results = _fire_all(s, [body] * 6)

    assert [r.status_code for r in results] == [200] * 6, [
        (r.status_code, r.text[:160]) for r in results if r.status_code != 200
    ]
    assert sorted(r.json()["replayed"] for r in results) == [False] + [True] * 5
    assert len({r.json()["points_log_id"] for r in results}) == 1
    assert len(_ledger(s)) == 1


def test_an_award_racing_a_deactivation_is_all_or_nothing(
    scenario: Callable[..., Scenario],
) -> None:
    """The activity row is read FOR SHARE: an admin's concurrent edit waits
    for the award's transaction, so an award can never pay a value the
    activity no longer has, and a deactivation never lands half-way."""
    s = scenario()
    outcome: dict[str, Any] = {}

    def award() -> None:
        outcome["response"] = _award(s)

    def deactivate() -> None:
        execute("UPDATE tasks SET is_active = false, points = 3 WHERE id = :t", t=s.task_id)

    threads = [threading.Thread(target=award), threading.Thread(target=deactivate)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    response = outcome["response"]
    if response.status_code == 200:
        assert response.json()["points_awarded"] == 10  # the pre-edit rule, in full
        assert len(_ledger(s)) == 1
    else:
        assert _error(response) in {(409, "TASK_INACTIVE")}
        assert _ledger(s) == []


# --- database-level guarantees ---------------------------------------------------


def _try_insert_points(
    user_id: int, booth_id: int, task_id: int | None, dedupe: str | None
) -> None:
    execute(
        "INSERT INTO points_log (user_id, booth_id, source, points, task_id, "
        "entered_by_staff_id, dedupe_key) "
        "VALUES (:u, :b, 'TASK', 5, :t, (SELECT id FROM staff_accounts WHERE booth_id = :b "
        "LIMIT 1), :d)",
        u=user_id,
        b=booth_id,
        t=task_id,
        d=dedupe,
    )


def test_the_database_itself_refuses_a_second_once_only_award(
    scenario: Callable[..., Scenario],
) -> None:
    from sqlalchemy.exc import IntegrityError

    s = scenario()
    _try_insert_points(s.user_id, s.booth_id, s.task_id, "k-one")
    # A DIFFERENTLY built dedupe key still collides: the (attendee, activity)
    # index does not depend on how the string was constructed.
    with pytest.raises(IntegrityError, match="uq_points_task_user_once"):
        _try_insert_points(s.user_id, s.booth_id, s.task_id, "some-other-string")
    # Repeat-allowed rows (no dedupe key) and other activities are not blocked.
    _try_insert_points(s.user_id, s.booth_id, s.task_id, None)
    _try_insert_points(s.user_id, s.booth_id, s.second_task_id, "k-two")
    # Legacy-style rows with no activity are outside the rule entirely.
    _try_insert_points(s.user_id, s.booth_id, None, None)
    _try_insert_points(s.user_id, s.booth_id, None, None)


def test_the_database_refuses_an_award_tied_to_another_booths_activity(
    scenario: Callable[..., Scenario],
) -> None:
    from sqlalchemy.exc import IntegrityError

    s = scenario()
    other = scenario()
    with pytest.raises(IntegrityError, match="fk_points_task_booth"):
        _try_insert_points(s.user_id, s.booth_id, other.task_id, "x")


@pytest.mark.parametrize("bad", [0, -3, 101])
def test_the_database_refuses_an_out_of_range_activity_value(
    scenario: Callable[..., Scenario], bad: int
) -> None:
    from sqlalchemy.exc import IntegrityError

    s = scenario()
    with pytest.raises(IntegrityError, match="ck_tasks_points_range"):
        execute("UPDATE tasks SET points = :p WHERE id = :t", p=bad, t=s.task_id)


# --- legacy rows -------------------------------------------------------------------


def test_awards_made_before_activities_are_preserved_and_never_attached_to_one(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario()
    # A row exactly as the previous award flow wrote it: no task, booth-level key.
    legacy_id = _insert_returning_id(
        "INSERT INTO points_log (user_id, booth_id, source, points, entered_by_staff_id, "
        "dedupe_key) VALUES (:u, :b, 'TASK', 5, :s, :k) RETURNING id",
        u=s.user_id,
        b=s.booth_id,
        s=s.staff_id,
        k=f"TASK:{s.user_id}:{s.booth_id}",
    )

    fresh = _award(s)

    assert fresh.status_code == 200  # a legacy booth-level row does not block an activity
    assert fresh.json()["resulting_total"] == 15  # 5 legacy + 10 new
    legacy = _points_log_row(legacy_id)
    assert (legacy[4], legacy[5]) == (5, None)  # points unchanged, task_id still NULL
    assert _ledger(s)[0] == (None, 5, f"TASK:{s.user_id}:{s.booth_id}")


# --- what staff see -------------------------------------------------------------------


def _staff_tasks(s: Scenario, session_token: str | None = None) -> Response:
    with make_client() as c:
        c.cookies.set("session", session_token or s.session_token)
        response: Response = c.get("/api/staff/tasks")
        return response


def test_staff_see_only_their_own_booths_active_awardable_activities(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(task_points=10, task_repeat=True)
    other = scenario()
    inactive = _insert_returning_id(
        "INSERT INTO tasks (booth_id, title, points, is_active, sort_order) "
        "VALUES (:b, 'Switched off', 5, false, 2) RETURNING id",
        b=s.booth_id,
    )
    unpriced = _insert_returning_id(
        "INSERT INTO tasks (booth_id, title, sort_order) VALUES (:b, 'No value yet', 3) "
        "RETURNING id",
        b=s.booth_id,
    )

    response = _staff_tasks(s)

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": s.task_id,
            "title": "Main activity",
            "description": None,
            "points": 10,
            "repeatable": True,
            "rule_revision": rule_revision_in_force(s.task_id),
        },
        {
            "id": s.second_task_id,
            "title": "Second activity",
            "description": None,
            "points": 15,
            "repeatable": False,
            "rule_revision": rule_revision_in_force(s.second_task_id),
        },
    ]
    ids = {t["id"] for t in response.json()}
    assert ids.isdisjoint({inactive, unpriced, other.task_id})


def test_the_activity_list_is_for_booth_staff_only(scenario: Callable[..., Scenario]) -> None:
    admin = scenario(role=StaffRole.ADMIN)
    with make_client() as c:
        assert c.get("/api/staff/tasks").status_code == 401
    assert _staff_tasks(admin, session_token=admin.session_token).status_code == 403


# --- what attendees and admins see in the history ---------------------------------------


def test_recent_awards_name_the_activity_and_fall_back_for_old_rows(
    scenario: Callable[..., Scenario],
) -> None:
    from app.core.sessions import create_attendee_session

    s = scenario()
    _insert_returning_id(
        "INSERT INTO points_log (user_id, booth_id, source, points, entered_by_staff_id, "
        "dedupe_key) VALUES (:u, :b, 'TASK', 5, :s, :k) RETURNING id",
        u=s.user_id,
        b=s.booth_id,
        s=s.staff_id,
        k=f"TASK:{s.user_id}:{s.booth_id}",
    )
    assert _award(s).status_code == 200

    db = SessionLocal()
    try:
        token = create_attendee_session(db, user_id=s.user_id, now=datetime.now(UTC))
        db.commit()
    finally:
        db.close()
    with make_client() as c:
        c.cookies.set("session", token)
        snapshot = c.get("/api/attendees/me/score")

    assert snapshot.status_code == 200
    awards = snapshot.json()["recent_awards"]
    assert [(a["points"], a["task_title"]) for a in awards] == [(10, "Main activity"), (5, None)]
    assert snapshot.json()["total_points"] == 15


# --------------------------------------------------------------------------
# Rule revision: what the staff member confirmed must be what is paid
# --------------------------------------------------------------------------


def _key_records(s: Scenario) -> int:
    return int(
        fetch_one(
            "SELECT count(*) FROM idempotency_records WHERE staff_account_id = :s", s=s.staff_id
        )[0]
    )


def _rule_changed_details(response: Response) -> dict[str, Any]:
    assert _error(response) == (409, "TASK_RULE_CHANGED")
    details: dict[str, Any] = response.json()["error"]["details"]
    return details


def test_points_changed_after_confirmation_awards_nothing(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(task_points=10)
    confirmed = rule_revision_in_force(s.task_id)  # what the staff screen showed
    execute("UPDATE tasks SET points = 25 WHERE id = :t", t=s.task_id)

    response = _award(s, rule_revision=confirmed)

    details = _rule_changed_details(response)
    assert details["points"] == 25  # the CURRENT rule, for a fresh confirmation
    assert details["task_id"] == s.task_id
    assert details["task_title"] == "Main activity"
    assert details["repeatable"] is False
    assert details["rule_revision"] == rule_revision_in_force(s.task_id) != confirmed
    assert _ledger(s) == []  # nothing was paid, at neither value
    assert _key_records(s) == 0  # and the refusal did not claim the key


def test_the_new_confirmation_with_a_new_key_pays_the_new_rule(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(task_points=10)
    stale = rule_revision_in_force(s.task_id)
    execute("UPDATE tasks SET points = 25 WHERE id = :t", t=s.task_id)
    refused = _award(s, rule_revision=stale)
    fresh = _rule_changed_details(refused)["rule_revision"]

    again = _award(s, rule_revision=fresh)  # staff re-confirmed; a new key

    assert again.status_code == 200
    assert again.json()["points_awarded"] == 25
    assert [r[1] for r in _ledger(s)] == [25]


def test_the_repeat_policy_changing_requires_a_new_confirmation(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(task_repeat=False)
    confirmed = rule_revision_in_force(s.task_id)
    execute("UPDATE tasks SET allow_repeat_points = true WHERE id = :t", t=s.task_id)

    details = _rule_changed_details(_award(s, rule_revision=confirmed))

    assert details["repeatable"] is True
    assert details["points"] == 10  # same points: the policy alone is enough
    assert _ledger(s) == []


def test_a_booth_or_event_level_policy_change_also_requires_a_new_confirmation(
    scenario: Callable[..., Scenario],
) -> None:
    # the activity inherits - so the booth flag and the event default ARE its policy
    s = scenario(allow_repeat=None, task_repeat=None)
    confirmed = rule_revision_in_force(s.task_id)

    execute("UPDATE booths SET allow_repeat_task_points = true WHERE id = :b", b=s.booth_id)
    assert _rule_changed_details(_award(s, rule_revision=confirmed))["repeatable"] is True

    # back to inheriting everything (effective: once), confirmed there; now the
    # EVENT default moves
    execute("UPDATE booths SET allow_repeat_task_points = NULL WHERE id = :b", b=s.booth_id)
    confirmed = rule_revision_in_force(s.task_id)
    execute("UPDATE event_config SET allow_repeat_task_points_default = true WHERE id = 1")
    try:
        assert _rule_changed_details(_award(s, rule_revision=confirmed))["repeatable"] is True
    finally:
        execute("UPDATE event_config SET allow_repeat_task_points_default = false WHERE id = 1")
    assert _ledger(s) == []


def test_an_unchanged_rule_still_awards_whatever_else_was_edited(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(task_points=10, task_repeat=None)
    confirmed = rule_revision_in_force(s.task_id)
    # edits that do not touch what is paid or who may earn it again
    execute(
        "UPDATE tasks SET title = 'Renamed', description = 'New text', sort_order = 9 "
        "WHERE id = :t",
        t=s.task_id,
    )
    execute("UPDATE tasks SET allow_repeat_points = false WHERE id = :t", t=s.task_id)
    # ... the explicit `false` equals the inherited default, so the EFFECTIVE rule is the same

    response = _award(s, rule_revision=confirmed)

    assert response.status_code == 200
    assert response.json()["points_awarded"] == 10
    assert [r[1] for r in _ledger(s)] == [10]


def test_the_revision_confirms_a_rule_it_never_sets_one(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(task_points=10)
    other_activity = rule_revision_in_force(s.second_task_id)
    for forged in ("x", "A" * 22, other_activity, "0" * 64):
        details = _rule_changed_details(_award(s, rule_revision=forged))
        assert details["points"] == 10
    assert _ledger(s) == []


def test_a_completed_award_replays_after_the_rule_changed(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(task_points=10, task_repeat=False)
    confirmed = rule_revision_in_force(s.task_id)
    key = uuid.uuid4()
    first = _award(s, idempotency_key=key, rule_revision=confirmed)
    assert first.status_code == 200

    # the activity is re-priced, made repeatable, then switched off
    execute(
        "UPDATE tasks SET points = 40, allow_repeat_points = true, is_active = false WHERE id = :t",
        t=s.task_id,
    )
    replay = _award(s, idempotency_key=key, rule_revision=confirmed)

    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    original = first.json()
    assert {k: v for k, v in replay.json().items() if k != "replayed"} == {
        k: v for k, v in original.items() if k != "replayed"
    }
    assert replay.json()["points_awarded"] == 10  # the original, not 40
    assert len(_ledger(s)) == 1


def test_a_replay_is_not_blocked_by_a_revision_that_no_longer_matches(
    scenario: Callable[..., Scenario],
) -> None:
    """The revision check never stands between a finished operation and its
    replay - whatever revision the retry carries, current, stale or absent
    from the server's point of view."""
    s = scenario(task_points=10)
    key = uuid.uuid4()
    assert _award(s, idempotency_key=key).status_code == 200
    execute("UPDATE tasks SET points = 99 WHERE id = :t", t=s.task_id)

    for revision in (rule_revision_in_force(s.task_id), "stale-revision"):
        replay = _award(s, idempotency_key=key, rule_revision=revision)
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True
        assert replay.json()["points_awarded"] == 10
    assert len(_ledger(s)) == 1


def test_a_network_retry_with_the_same_key_never_pays_twice(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(task_points=10)
    body = _body(s)  # one confirmation: one key, one revision, one request
    with make_client() as c:
        c.cookies.set("session", s.session_token)
        first = c.post(AWARD, json=body)
        retry_1 = c.post(AWARD, json=body)
        retry_2 = c.post(AWARD, json=body)
    assert [r.status_code for r in (first, retry_1, retry_2)] == [200, 200, 200]
    assert [r.json()["replayed"] for r in (first, retry_1, retry_2)] == [False, True, True]
    assert len(_ledger(s)) == 1
    assert fetch_one("SELECT sum(points) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 10


def test_a_retry_lost_to_a_rule_change_still_does_not_double_pay(
    scenario: Callable[..., Scenario],
) -> None:
    """The first request was paid but its answer was lost; the rule changes;
    the scanner retries with the SAME key. It gets the original result - not a
    rule-changed prompt that would lead to a second payment under a new key."""
    s = scenario(task_points=10)
    body = _body(s)
    with make_client() as c:
        c.cookies.set("session", s.session_token)
        assert c.post(AWARD, json=body).status_code == 200  # the response is "lost"
        execute("UPDATE tasks SET points = 30 WHERE id = :t", t=s.task_id)
        retry = c.post(AWARD, json=body)
    assert retry.status_code == 200
    assert retry.json()["replayed"] is True
    assert retry.json()["points_awarded"] == 10
    assert len(_ledger(s)) == 1


def test_a_same_key_retry_that_loses_the_race_to_a_rule_change_is_replayed(
    scenario: Callable[..., Scenario],
) -> None:
    """The narrow window: the original commits and the rule changes AFTER the
    retry looked for its record but BEFORE it read the activity. Exercised at
    the helper that closes the window."""
    s = scenario(task_points=10)
    key = uuid.uuid4()
    assert _award(s, idempotency_key=key).status_code == 200
    request = AwardTaskPointsRequest(
        card_token=s.card_token,
        task_id=s.task_id,
        rule_revision="stale",
        idempotency_key=key,
        scan_ref="unused",
    )
    fingerprint = compute_award_fingerprint(card_token=s.card_token, task_id=s.task_id)

    db = SessionLocal()
    try:
        outcome = _replay_or_raise(
            db,
            staff_id=s.staff_id,
            request=request,
            fingerprint=fingerprint,
            error=TaskRuleChangedError({}),
        )
        assert outcome.response.replayed is True
        assert outcome.response.points_awarded == 10
        assert outcome.newly_awarded_user_id is None  # nothing new: no realtime signal

        unknown = request.model_copy(update={"idempotency_key": uuid.uuid4()})
        with pytest.raises(TaskRuleChangedError):
            _replay_or_raise(
                db,
                staff_id=s.staff_id,
                request=unknown,
                fingerprint=fingerprint,
                error=TaskRuleChangedError({}),
            )
    finally:
        db.close()


def test_the_listed_revision_moves_with_the_rule_and_is_per_activity(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(task_points=10)

    def listed() -> dict[int, str]:
        rows = _staff_tasks(s).json()
        return {row["id"]: row["rule_revision"] for row in rows}

    before = listed()
    assert listed() == before  # stable while nothing changes
    assert before[s.task_id] != before[s.second_task_id]

    execute("UPDATE tasks SET points = 11 WHERE id = :t", t=s.task_id)
    after = listed()
    assert after[s.task_id] != before[s.task_id]
    assert after[s.second_task_id] == before[s.second_task_id]  # untouched activity

    execute("UPDATE tasks SET points = 10 WHERE id = :t", t=s.task_id)
    assert listed() == before  # the same rule is the same revision

    # and the listed revision is exactly what an award accepts
    assert _award(s, rule_revision=before[s.task_id]).status_code == 200


def test_a_page_that_sends_no_confirmed_revision_is_told_to_reload(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario(task_points=10)
    body = _body(s)
    del body["rule_revision"]  # the previous scanner: an activity, but no confirmed rule
    with make_client() as c:
        c.cookies.set("session", s.session_token)
        response = c.post(AWARD, json=body)

    assert _error(response) == (403, "CSRF_REJECTED")
    assert response.json()["error"]["details"] == {"reason": "client_outdated"}
    assert "reload" in response.json()["error"]["message"].lower()
    assert "no points were awarded" in response.json()["error"]["message"]
    assert _ledger(s) == []
    assert _key_records(s) == 0


@pytest.mark.parametrize(
    "legacy",
    [
        # the very first contract: a staff-chosen preset value, no activity
        lambda s: {"card_token": s.card_token, "points": 10},
        # an activity AND a client-chosen value
        lambda s: {"task_id": 1, "points": 999},
    ],
)
def test_the_oldest_award_payloads_create_no_points(
    scenario: Callable[..., Scenario], legacy: Callable[[Scenario], dict[str, Any]]
) -> None:
    s = scenario(task_points=10)
    body = _body(s)
    body.pop("task_id")
    body.pop("rule_revision")
    body.update(legacy(s))
    with make_client() as c:
        c.cookies.set("session", s.session_token)
        response = c.post(AWARD, json=body)
    assert _error(response) == (403, "CSRF_REJECTED")
    assert _ledger(s) == []
    assert _key_records(s) == 0


def test_an_outdated_page_is_refused_before_any_database_work(
    scenario: Callable[..., Scenario],
) -> None:
    """Not even the auth session is needed: the verdict is about the page, so
    it works for a logged-out cached tab too - and costs the server nothing."""
    s = scenario()
    body = _body(s)
    body["points"] = 10
    with make_client() as c:  # no session cookie at all
        response = c.post(AWARD, json=body)
    assert _error(response) == (403, "CSRF_REJECTED")


def test_a_deactivated_booth_cannot_award_even_its_active_activities(
    scenario: Callable[..., Scenario],
) -> None:
    """Security audit (run-1): deactivating a booth must stop its staff from
    awarding, like deactivating one of its activities does."""
    s = scenario()
    execute("UPDATE booths SET is_active = false WHERE id = :b", b=s.booth_id)
    try:
        assert _error(_award(s)) == (409, "TASK_INACTIVE")
        assert _error(_award(s, task_id=s.second_task_id)) == (409, "TASK_INACTIVE")
        assert _ledger(s) == []
    finally:
        execute("UPDATE booths SET is_active = true WHERE id = :b", b=s.booth_id)
    assert _award(s).status_code == 200
