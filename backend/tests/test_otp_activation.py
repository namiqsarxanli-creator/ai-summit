"""M04 acceptance: OTP issuance/verification, the activation
transaction, the reset fence on every M04 mutation, and
purge-provisional — ARCHITECTURE.md §8.1, §8.2, §8.3, §19.4.

Real PostgreSQL throughout; the concurrency tests use genuinely
separate connections on separate threads, because row-lock
serialisation is a multi-connection phenomenon that one session cannot
fake.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from app import cli as app_cli
from app.core.config import Settings, get_settings
from app.core.locks import FACEMARK_RESET_FENCE
from app.db.session import SessionLocal, mutating_transaction
from app.modules.attendees import service as attendees_service
from app.modules.attendees.models import OtpPurpose
from app.modules.attendees.service import (
    OTP_MAX_ATTEMPTS,
    PendingOtpDelivery,
    VerifyOutcome,
    verify_activation_otp,
)
from app.modules.auth import otp_delivery
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
OTP_REQUEST = "/api/auth/otp/request"
VERIFY = "/api/auth/otp/verify"

_PSYCOPG_URL = get_settings().database_url.replace("postgresql+psycopg://", "postgresql://")


def _register(client: TestClient, email: str, **overrides: Any) -> None:
    assert client.post(REGISTER, json=register_payload(email, **overrides)).status_code == 200


def _challenge(email: str, purpose: str = "ACTIVATION") -> Any:
    return fetch_one(
        "SELECT id, user_id, code_hash, attempt_count, consumed_at, expires_at, created_at "
        "FROM otp_challenges WHERE email = :e AND purpose = :p ORDER BY id DESC LIMIT 1",
        e=email.lower(),
        p=purpose,
    )


def _user(email: str) -> Any:
    return fetch_one("SELECT id, status, activated_at FROM users WHERE email = :e", e=email.lower())


def _sessions(user_id: int) -> list[Any]:
    return fetch_all(
        "SELECT token_hash, principal_type, issued_at, expires_at, revoked_at "
        "FROM sessions WHERE user_id = :u",
        u=user_id,
    )


def _error(response: Response) -> tuple[int, str, str]:
    """Everything a client sees about a failure, minus the per-request
    correlation_id."""
    err = response.json()["error"]
    return response.status_code, err["code"], err["message"]


# --------------------------------------------------------------------------
# Challenge storage: hash only, purpose-scoped, 10-minute expiry
# --------------------------------------------------------------------------


def test_registration_issues_a_hashed_activation_challenge(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    _register(client, unique_email)
    code = captured_otp(otp_log.records, unique_email)
    challenge = _challenge(unique_email)
    user = _user(unique_email)

    assert len(code) == 6 and code.isdigit()
    assert challenge.user_id == user.id
    assert challenge.attempt_count == 0
    assert challenge.consumed_at is None
    assert challenge.code_hash.startswith("$argon2id$")
    assert code not in challenge.code_hash
    expiry = challenge.expires_at - challenge.created_at
    assert timedelta(minutes=9, seconds=55) < expiry <= timedelta(minutes=10, seconds=5)


def test_plaintext_otp_appears_nowhere_in_the_challenge_row(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """IMPLEMENTATION_PLAN.md M04 acceptance #3: "OTP row stores only a
    hash; the plaintext code appears nowhere in the database." Checked
    across every non-timestamp column of the challenge row."""
    _register(client, unique_email)
    code = captured_otp(otp_log.records, unique_email)
    # Every text-bearing column. Timestamps are excluded deliberately:
    # their 6-digit microsecond fields could coincide with the code by
    # pure chance and make this test flaky without testing anything.
    row = fetch_one(
        "SELECT concat_ws('|', purpose::text, code_hash, request_ip, user_id::text, "
        "attempt_count::text) FROM otp_challenges WHERE email = :e",
        e=unique_email,
    )
    assert code not in row[0]


def test_otp_is_not_logged_outside_demo_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARCHITECTURE.md §21.11: codes are never logged — DEMO_MODE is the
    single documented exception. With it off, the adapter refuses
    outright instead of logging or silently dropping the code."""
    monkeypatch.setattr(
        otp_delivery,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            environment="development",
            database_url="postgresql+psycopg://u:p@h/d",
            demo_mode=False,
        ),
    )
    with pytest.raises(RuntimeError, match="DEMO_MODE"):
        otp_delivery.deliver_otp(
            PendingOtpDelivery(
                email="x@test.facemark.demo", purpose=OtpPurpose.LOGIN, code="123456"
            )
        )


# --------------------------------------------------------------------------
# Activation — correct code
# --------------------------------------------------------------------------


def test_correct_otp_activates_and_creates_the_session(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """ARCHITECTURE.md §8.1 Step 3: "consume the challenge, set status =
    ACTIVE and activated_at, activate the contact card, and create the
    session" — and §8.3's exact cookie attributes."""
    _register(client, unique_email)
    code = captured_otp(otp_log.records, unique_email)

    response = client.post(VERIFY, json={"email": unique_email, "code": code})
    assert response.status_code == 200
    assert response.json() == {"status": "ACTIVE"}

    user = _user(unique_email)
    assert user.status == "ACTIVE"
    assert user.activated_at is not None  # ck_users_status_requires_activated_at holds
    assert _challenge(unique_email).consumed_at is not None

    cookie_header = response.headers["set-cookie"]
    attributes = {part.strip().split("=")[0].lower() for part in cookie_header.split(";")}
    assert {"httponly", "path", "max-age", "samesite"} <= attributes
    assert "Max-Age=43200" in cookie_header  # 12h
    assert "SameSite=lax" in cookie_header
    assert "secure" not in attributes  # plain-HTTP test server; Secure only over HTTPS

    token = cookie_header.split(";")[0].split("=", 1)[1]
    sessions = _sessions(user.id)
    assert len(sessions) == 1
    session = sessions[0]
    assert session.principal_type == "ATTENDEE"
    assert session.revoked_at is None
    # Only the SHA-256 hash is stored (§8.3); the raw token is not.
    assert session.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert session.token_hash != token
    lifetime = session.expires_at - session.issued_at
    assert lifetime == timedelta(hours=12)


def test_activation_keeps_the_registered_profile_interests_and_card(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    _register(client, unique_email, sponsor_consent=True, interest_slugs=["robotics"])
    user_id = _user(unique_email).id
    token_before = fetch_one("SELECT public_token FROM contact_cards WHERE user_id = :u", u=user_id)
    code = captured_otp(otp_log.records, unique_email)

    client.post(VERIFY, json={"email": unique_email, "code": code})

    after = fetch_one("SELECT first_name, sponsor_consent FROM users WHERE id = :u", u=user_id)
    assert (after.first_name, after.sponsor_consent) == ("Grace", True)
    slugs = fetch_all(
        "SELECT i.slug FROM user_interests ui JOIN interests i ON i.id = ui.interest_id "
        "WHERE ui.user_id = :u",
        u=user_id,
    )
    assert [s.slug for s in slugs] == ["robotics"]
    token_after = fetch_one("SELECT public_token FROM contact_cards WHERE user_id = :u", u=user_id)
    assert token_after == token_before
    audit = fetch_all("SELECT granted FROM consent_audit WHERE user_id = :u", u=user_id)
    assert [a.granted for a in audit] == [True]


def test_verify_accepts_a_differently_cased_email(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    _register(client, unique_email)
    code = captured_otp(otp_log.records, unique_email)
    response = client.post(VERIFY, json={"email": unique_email.upper(), "code": code})
    assert response.status_code == 200


# --------------------------------------------------------------------------
# Rejection — wrong, exhausted, expired, consumed: one indistinguishable code
# --------------------------------------------------------------------------


def _wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


def test_incorrect_otp_is_rejected_and_counted(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """A failed activation leaves `status` unchanged and no session
    (IMPLEMENTATION_PLAN.md M04 integration test), while the failed
    attempt itself IS durably counted."""
    _register(client, unique_email)
    code = captured_otp(otp_log.records, unique_email)

    response = client.post(VERIFY, json={"email": unique_email, "code": _wrong(code)})
    assert _error(response)[:2] == (400, "OTP_INVALID")
    assert "set-cookie" not in response.headers

    user = _user(unique_email)
    assert user.status == "PROVISIONAL"
    assert user.activated_at is None
    assert _sessions(user.id) == []
    challenge = _challenge(unique_email)
    assert challenge.attempt_count == 1
    assert challenge.consumed_at is None


def test_attempt_cap_invalidates_the_challenge(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """§8.2: "5 failed attempts invalidates the challenge" — after
    that, even the CORRECT code is refused."""
    _register(client, unique_email)
    code = captured_otp(otp_log.records, unique_email)
    for _ in range(OTP_MAX_ATTEMPTS):
        client.post(VERIFY, json={"email": unique_email, "code": _wrong(code)})
    assert _challenge(unique_email).attempt_count == OTP_MAX_ATTEMPTS

    response = client.post(VERIFY, json={"email": unique_email, "code": code})
    assert _error(response)[:2] == (400, "OTP_INVALID")
    assert _user(unique_email).status == "PROVISIONAL"
    assert _challenge(unique_email).attempt_count == OTP_MAX_ATTEMPTS  # no further increments


def test_expired_otp_is_rejected(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """§8.2: expiry "enforced in SQL ... never by a client clock"."""
    _register(client, unique_email)
    code = captured_otp(otp_log.records, unique_email)
    execute(
        "UPDATE otp_challenges SET expires_at = now() - interval '1 second' WHERE email = :e",
        e=unique_email,
    )
    response = client.post(VERIFY, json={"email": unique_email, "code": code})
    assert _error(response)[:2] == (400, "OTP_INVALID")
    assert _user(unique_email).status == "PROVISIONAL"


def test_consumed_otp_cannot_be_reused(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    _register(client, unique_email)
    code = captured_otp(otp_log.records, unique_email)
    assert client.post(VERIFY, json={"email": unique_email, "code": code}).status_code == 200

    replay = client.post(VERIFY, json={"email": unique_email, "code": code})
    assert _error(replay)[:2] == (400, "OTP_INVALID")
    assert len(_sessions(_user(unique_email).id)) == 1  # no second session


def test_all_otp_failure_causes_are_indistinguishable(
    client: TestClient, otp_log: pytest.LogCaptureFixture
) -> None:
    """IMPLEMENTATION_PLAN.md M04: "expired, consumed and
    attempt-exhausted codes all return the same OTP_INVALID" — plus a
    plain wrong guess and an address that never registered. Same
    status, code and message; only the correlation id differs."""
    emails = [new_email() for _ in range(4)]
    wrong_e, exhausted_e, expired_e, consumed_e = emails
    never_registered = new_email()
    try:
        for e in emails:
            _register(client, e)
        codes = {e: captured_otp(otp_log.records, e) for e in emails}

        for _ in range(OTP_MAX_ATTEMPTS):
            client.post(VERIFY, json={"email": exhausted_e, "code": _wrong(codes[exhausted_e])})
        execute(
            "UPDATE otp_challenges SET expires_at = now() - interval '1 second' WHERE email = :e",
            e=expired_e,
        )
        client.post(VERIFY, json={"email": consumed_e, "code": codes[consumed_e]})

        outcomes = {
            _error(client.post(VERIFY, json={"email": wrong_e, "code": _wrong(codes[wrong_e])})),
            _error(client.post(VERIFY, json={"email": exhausted_e, "code": codes[exhausted_e]})),
            _error(client.post(VERIFY, json={"email": expired_e, "code": codes[expired_e]})),
            _error(client.post(VERIFY, json={"email": consumed_e, "code": codes[consumed_e]})),
            _error(client.post(VERIFY, json={"email": never_registered, "code": "123456"})),
        }
        assert len(outcomes) == 1
        assert next(iter(outcomes))[:2] == (400, "OTP_INVALID")
    finally:
        for e in emails:
            cleanup_email(e)


@pytest.mark.parametrize("bad_code", ["12345", "1234567", "abcdef", ""])
def test_malformed_code_is_a_validation_error(
    client: TestClient, unique_email: str, bad_code: str
) -> None:
    response = client.post(VERIFY, json={"email": unique_email, "code": bad_code})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    if bad_code:  # submitted values are never echoed back in validation details
        assert bad_code not in str(response.json()["error"]["details"])


def test_login_code_cannot_activate_through_this_endpoint(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """§8.2: "purpose exists so an activation code cannot be replayed as
    a login code, or vice versa". Completing a LOGIN-purpose code is
    M05 scope; M04's verify only ever consumes ACTIVATION challenges."""
    _register(client, unique_email)
    code = captured_otp(otp_log.records, unique_email)
    client.post(VERIFY, json={"email": unique_email, "code": code})
    _register(client, unique_email)  # ACTIVE branch -> LOGIN code
    login_code = captured_otp(otp_log.records, unique_email, purpose="LOGIN")

    response = client.post(VERIFY, json={"email": unique_email, "code": login_code})
    assert _error(response)[:2] == (400, "OTP_INVALID")
    assert _challenge(unique_email, purpose="LOGIN").consumed_at is None
    assert len(_sessions(_user(unique_email).id)) == 1  # only the activation session


# --------------------------------------------------------------------------
# POST /api/auth/otp/request — non-enumerating re-send
# --------------------------------------------------------------------------


def test_otp_request_is_non_enumerating(
    client: TestClient, otp_log: pytest.LogCaptureFixture
) -> None:
    """§8.2: "returns the same response whether or not the email is
    registered". Unknown -> nothing issued; PROVISIONAL -> fresh
    ACTIVATION code; ACTIVE -> LOGIN code."""
    unknown, provisional, active = new_email(), new_email(), new_email()
    try:
        _register(client, provisional)
        _register(client, active)
        client.post(VERIFY, json={"email": active, "code": captured_otp(otp_log.records, active)})

        responses = [
            client.post(OTP_REQUEST, json={"email": e}) for e in (unknown, provisional, active)
        ]
        assert {r.status_code for r in responses} == {200}
        assert responses[0].content == responses[1].content == responses[2].content

        assert fetch_all("SELECT id FROM otp_challenges WHERE email = :e", e=unknown) == []
        live_provisional = fetch_all(
            "SELECT purpose FROM otp_challenges WHERE email = :e AND consumed_at IS NULL",
            e=provisional,
        )
        assert [r.purpose for r in live_provisional] == ["ACTIVATION"]
        live_active = fetch_all(
            "SELECT purpose FROM otp_challenges WHERE email = :e AND consumed_at IS NULL",
            e=active,
        )
        assert [r.purpose for r in live_active] == ["LOGIN"]
    finally:
        for e in (unknown, provisional, active):
            cleanup_email(e)


def test_requested_code_supersedes_the_previous_one(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """§8.2: "Requesting a new code marks prior unconsumed challenges
    for that email as consumed." """
    _register(client, unique_email)
    old = captured_otp(otp_log.records, unique_email)
    client.post(OTP_REQUEST, json={"email": unique_email})
    new = captured_otp(otp_log.records, unique_email)

    if old != new:
        assert client.post(VERIFY, json={"email": unique_email, "code": old}).status_code == 400
    assert client.post(VERIFY, json={"email": unique_email, "code": new}).status_code == 200


# --------------------------------------------------------------------------
# Concurrency against real PostgreSQL (§8.2 row-lock semantics)
# --------------------------------------------------------------------------


def _verify_in_own_transaction(email: str, code: str, barrier: threading.Barrier) -> VerifyOutcome:
    session = SessionLocal()
    try:
        barrier.wait()
        with mutating_transaction(session) as tx:
            return verify_activation_otp(tx, email=email, code=code)
    finally:
        session.close()


def _run_concurrently(calls: list[Callable[[], VerifyOutcome]]) -> list[VerifyOutcome]:
    results: list[VerifyOutcome | BaseException] = [RuntimeError("not run")] * len(calls)

    def runner(i: int, call: Callable[[], VerifyOutcome]) -> None:
        try:
            results[i] = call()
        except BaseException as exc:  # noqa: BLE001 — surfaced by the assert below
            results[i] = exc

    threads = [threading.Thread(target=runner, args=(i, c)) for i, c in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    for r in results:
        if isinstance(r, BaseException):
            raise r
    return [r for r in results if isinstance(r, VerifyOutcome)]


def test_two_simultaneous_correct_submissions_activate_exactly_once(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """IMPLEMENTATION_PLAN.md M04: "two simultaneous correct OTP
    submissions for one challenge — exactly one activates, the other
    gets OTP_INVALID." Two real connections, released together by a
    barrier; `SELECT ... FOR UPDATE` serialises them."""
    _register(client, unique_email)
    code = captured_otp(otp_log.records, unique_email)
    barrier = threading.Barrier(2, timeout=10)

    outcomes = _run_concurrently(
        [lambda: _verify_in_own_transaction(unique_email, code, barrier)] * 2
    )

    assert sorted(o.ok for o in outcomes) == [False, True]
    user = _user(unique_email)
    assert user.status == "ACTIVE"
    assert len(_sessions(user.id)) == 1


def test_parallel_wrong_guesses_cannot_race_past_the_attempt_cap(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """IMPLEMENTATION_PLAN.md M04: "five parallel wrong guesses do not
    exceed the attempt cap by racing on a stale counter." Eight here —
    more than the cap — so both failure modes would show: a lost update
    would leave the count below 5, a stale read would push it above."""
    _register(client, unique_email)
    code = captured_otp(otp_log.records, unique_email)
    wrong = _wrong(code)
    barrier = threading.Barrier(8, timeout=10)

    outcomes = _run_concurrently(
        [lambda: _verify_in_own_transaction(unique_email, wrong, barrier)] * 8
    )

    assert not any(o.ok for o in outcomes)
    assert _challenge(unique_email).attempt_count == OTP_MAX_ATTEMPTS
    # ...and the cap genuinely holds for the right code afterwards.
    response = client.post(VERIFY, json={"email": unique_email, "code": code})
    assert response.status_code == 400
    assert _user(unique_email).status == "PROVISIONAL"


# --------------------------------------------------------------------------
# Reset fence: acquired before the first reset-sensitive read (§19.4, #29)
# --------------------------------------------------------------------------

_APP_TABLE_LOCKS_WHILE_WAITING_ON_FENCE = """
SELECT count(*) FROM pg_locks rel
JOIN pg_locks adv ON adv.pid = rel.pid AND adv.locktype = 'advisory' AND NOT adv.granted
WHERE rel.locktype = 'relation' AND rel.granted
  AND rel.relation IN (
      'users'::regclass, 'otp_challenges'::regclass, 'interests'::regclass,
      'user_interests'::regclass, 'contact_cards'::regclass, 'consent_audit'::regclass,
      'sessions'::regclass
  )
"""


def _assert_fenced_before_any_read(call: Callable[[], Response]) -> Response:
    """Holds the EXCLUSIVE side of the reset fence (as demo reset will,
    M15), fires the request, and — while it is blocked — inspects
    pg_locks: the blocked backend must hold no lock on ANY application
    table. Merely blocking would prove the fence exists; holding zero
    relation locks while blocked proves it comes BEFORE the first
    reset-sensitive read, which is exactly invariant #29. The lock is
    then released and the request must complete normally."""
    holder = psycopg.connect(_PSYCOPG_URL)
    holder.autocommit = True
    with holder.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (FACEMARK_RESET_FENCE,))

    result: dict[str, Response] = {}
    worker = threading.Thread(target=lambda: result.setdefault("r", call()))
    try:
        worker.start()
        with psycopg.connect(_PSYCOPG_URL, autocommit=True) as probe, probe.cursor() as cur:
            deadline = time.monotonic() + 1.5
            waiting = 0
            while time.monotonic() < deadline and not waiting:
                cur.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                )
                row = cur.fetchone()
                waiting = row[0] if row else 0
                time.sleep(0.02)
            assert waiting >= 1, "request never reached the fence"
            cur.execute(_APP_TABLE_LOCKS_WHILE_WAITING_ON_FENCE)
            row = cur.fetchone()
            assert row is not None and row[0] == 0, (
                "a reset-sensitive table was read before the fence was acquired"
            )
    finally:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (FACEMARK_RESET_FENCE,))
        holder.close()
        worker.join(timeout=15)
    return result["r"]


def test_register_acquires_the_fence_before_any_read(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    response = _assert_fenced_before_any_read(
        lambda: client.post(REGISTER, json=register_payload(unique_email))
    )
    assert response.status_code == 200
    assert _user(unique_email).status == "PROVISIONAL"


def test_otp_request_acquires_the_fence_before_any_read(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    _register(client, unique_email)
    response = _assert_fenced_before_any_read(
        lambda: client.post(OTP_REQUEST, json={"email": unique_email})
    )
    assert response.status_code == 200


def test_otp_verify_acquires_the_fence_before_any_read(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    _register(client, unique_email)
    code = captured_otp(otp_log.records, unique_email)
    response = _assert_fenced_before_any_read(
        lambda: client.post(VERIFY, json={"email": unique_email, "code": code})
    )
    assert response.status_code == 200
    assert _user(unique_email).status == "ACTIVE"


def test_fence_timeout_maps_to_maintenance_and_writes_nothing(
    client: TestClient, unique_email: str
) -> None:
    """§19.4: a mutation whose lock_timeout (2 s) expires while reset
    holds the fence "returns MAINTENANCE" — and has written nothing."""
    holder = psycopg.connect(_PSYCOPG_URL)
    holder.autocommit = True
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (FACEMARK_RESET_FENCE,))
        response = client.post(REGISTER, json=register_payload(unique_email))
    finally:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (FACEMARK_RESET_FENCE,))
        holder.close()

    assert _error(response)[:2] == (503, "MAINTENANCE")
    assert _user(unique_email) is None


# --------------------------------------------------------------------------
# Atomicity: a failure anywhere rolls back everything
# --------------------------------------------------------------------------


def test_registration_rolls_back_completely_on_failure(
    unique_email: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User, interests, consent audit and card are one transaction
    (§8.1) — a failure while issuing the OTP must leave none of them
    behind, and the client sees only the generic INTERNAL_ERROR."""
    from app.main import app

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated failure after the user row was written")

    monkeypatch.setattr(attendees_service, "issue_otp", boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        response = c.post(REGISTER, json=register_payload(unique_email, sponsor_consent=True))

    assert _error(response) == (500, "INTERNAL_ERROR", "An unexpected error occurred.")
    assert "simulated" not in response.text and "Traceback" not in response.text
    assert _user(unique_email) is None
    assert (
        fetch_all(
            "SELECT a.id FROM consent_audit a JOIN users u ON u.id = a.user_id WHERE u.email = :e",
            e=unique_email,
        )
        == []
    )
    assert fetch_all("SELECT id FROM otp_challenges WHERE email = :e", e=unique_email) == []


def test_activation_rolls_back_completely_on_failure(
    client: TestClient,
    unique_email: str,
    otp_log: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§8.1 Step 3: "Either everything commits or nothing does" — a
    failure while creating the session must leave the challenge
    unconsumed and the attendee PROVISIONAL."""
    from app.main import app

    _register(client, unique_email)
    code = captured_otp(otp_log.records, unique_email)

    def boom() -> str:
        raise RuntimeError("simulated failure while creating the session")

    monkeypatch.setattr(attendees_service, "generate_session_token", boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        response = c.post(VERIFY, json={"email": unique_email, "code": code})

    assert response.status_code == 500
    assert "set-cookie" not in response.headers
    user = _user(unique_email)
    assert user.status == "PROVISIONAL"
    assert user.activated_at is None
    assert _challenge(unique_email).consumed_at is None
    assert _sessions(user.id) == []


# --------------------------------------------------------------------------
# purge-provisional (§8.1 "Abandoned registrations")
# --------------------------------------------------------------------------


def test_purge_provisional_frees_the_email_for_reregistration(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    _register(client, unique_email)
    user_id = _user(unique_email).id
    execute("UPDATE users SET created_at = now() - interval '25 hours' WHERE id = :u", u=user_id)

    app_cli.main(["purge-provisional"])

    assert _user(unique_email) is None
    for table in ("user_interests", "contact_cards", "consent_audit"):
        assert fetch_all(f"SELECT 1 FROM {table} WHERE user_id = :u", u=user_id) == []
    assert fetch_all("SELECT id FROM otp_challenges WHERE user_id = :u", u=user_id) == []

    _register(client, unique_email)
    fresh = _user(unique_email)
    assert fresh.status == "PROVISIONAL"
    assert fresh.id != user_id


def test_purge_provisional_spares_recent_and_active_attendees(
    client: TestClient, otp_log: pytest.LogCaptureFixture
) -> None:
    recent, old_active = new_email(), new_email()
    try:
        _register(client, recent)
        _register(client, old_active)
        client.post(
            VERIFY, json={"email": old_active, "code": captured_otp(otp_log.records, old_active)}
        )
        execute(
            "UPDATE users SET created_at = now() - interval '30 days' WHERE email = :e",
            e=old_active,
        )

        app_cli.main(["purge-provisional"])
        app_cli.main(["purge-provisional"])  # idempotent: a second run is harmless

        assert _user(recent).status == "PROVISIONAL"
        assert _user(old_active).status == "ACTIVE"
    finally:
        cleanup_email(recent)
        cleanup_email(old_active)


def test_purge_provisional_goes_through_the_fence() -> None:
    """Run standalone, purge can race live registrations, so it takes
    the shared fence too: with reset holding it, purge times out
    rather than deleting anything."""
    holder = psycopg.connect(_PSYCOPG_URL)
    holder.autocommit = True
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (FACEMARK_RESET_FENCE,))
        with pytest.raises(OperationalError, match="lock timeout"):
            app_cli.main(["purge-provisional"])
    finally:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (FACEMARK_RESET_FENCE,))
        holder.close()


# --------------------------------------------------------------------------
# DEMO_MODE configuration guard (§8.2)
# --------------------------------------------------------------------------


def _settings(**env: Any) -> Settings:
    return Settings(_env_file=None, **env)


def test_demo_mode_defaults_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEMO_MODE", raising=False)
    settings = _settings(environment="development", database_url="postgresql+psycopg://u:p@h/d")
    assert settings.demo_mode is False


def test_demo_mode_is_allowed_outside_production() -> None:
    settings = _settings(
        environment="development", database_url="postgresql+psycopg://u:p@h/d", demo_mode=True
    )
    assert settings.demo_mode is True


def test_demo_mode_is_refused_in_production() -> None:
    """ARCHITECTURE.md §8.2: "config.py raises at startup if
    DEMO_MODE=true while ENVIRONMENT=production." """
    with pytest.raises(ValidationError, match="DEMO_MODE"):
        _settings(
            environment="production", database_url="postgresql+psycopg://u:p@h/d", demo_mode=True
        )


def test_activation_timestamps_are_timezone_aware(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    _register(client, unique_email)
    client.post(
        VERIFY, json={"email": unique_email, "code": captured_otp(otp_log.records, unique_email)}
    )
    activated_at = _user(unique_email).activated_at
    assert activated_at.tzinfo is not None
    assert abs(datetime.now(UTC) - activated_at) < timedelta(minutes=1)
