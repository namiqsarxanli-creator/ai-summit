"""The durable OTP delivery outbox and its worker.

Runs against the suite's own throw-away database. The email provider is
`FakeProvider` (tests/outbox_support.py); no test here can reach a real
one.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.core.languages import LANGUAGES
from app.db.session import SessionLocal, mutating_transaction
from app.modules.attendees.models import OtpPurpose
from app.modules.attendees.schemas import RegisterRequest
from app.modules.attendees.service import (
    OTP_EXPIRY_MINUTES,
    issue_otp,
    register_attendee,
)
from app.modules.auth import otp_delivery
from app.modules.auth import otp_outbox as ob
from app.modules.auth.otp_crypto import (
    OtpCryptoError,
    decrypt_code,
    encrypt_code,
    key_fingerprint,
)
from app.modules.auth.otp_email import EXPIRY_MINUTES, render_otp_email
from app.modules.auth.otp_provider import ResendProvider
from app.worker import run_once
from tests.m04_support import fetch_one
from tests.outbox_support import (
    FAKE_API_KEY,
    OTHER_KEY,
    TEST_KEY,
    ExplodingProvider,
    FakeProvider,
    accepted,
    clean_outbox,
    count,
    deliveries_for,
    execute,
    expire_lease,
    install_settings,
    make_delivery,
    make_due,
    new_address,
    outbox_settings,
    peek_code,
    permanent,
    retryable,
    row,
)

REGISTER = "/api/attendees/register"
OTP_REQUEST = "/api/auth/otp/request"
OTP_VERIFY = "/api/auth/otp/verify"


@pytest.fixture(autouse=True)
def _clean_outbox(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The worker side always runs with the outbox key, as in production.
    worker_settings = outbox_settings()
    monkeypatch.setattr("app.modules.auth.otp_outbox.get_settings", lambda: worker_settings)
    clean_outbox()
    yield
    clean_outbox()


@pytest.fixture
def outbox_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    return install_settings(monkeypatch, outbox_settings())


def _register(client: TestClient, email: str, **extra: Any) -> Any:
    return client.post(
        REGISTER,
        json={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": email,
            "phone": "+994500000000",
            "profession": "Engineer",
            "sponsor_consent": False,
            **extra,
        },
    )


def _worker(provider: Any, worker_id: str = "w-test") -> int:
    return run_once(provider=provider, worker_id=worker_id)


# --------------------------------------------------------------------------
# Atomicity: the challenge and its outbox row stand or fall together
# --------------------------------------------------------------------------


def test_registration_writes_challenge_and_outbox_row_together(
    client: TestClient, unique_email: str, outbox_on: Any
) -> None:
    assert _register(client, unique_email).status_code == 200

    rows = deliveries_for(unique_email)
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["purpose"] == "ACTIVATION"
    challenge = fetch_one(
        "SELECT id, expires_at FROM otp_challenges WHERE email = :e", e=unique_email
    )
    assert rows[0]["challenge_id"] == challenge[0]
    assert rows[0]["expires_at"] == challenge[1]
    assert rows[0]["payload_ciphertext"] is not None


def test_a_queued_code_is_not_also_sent_by_the_request_process(
    client: TestClient, unique_email: str, outbox_on: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly one sender: the worker. The web process must not email it
    a second time."""
    posts: list[object] = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: posts.append((a, k)))

    assert _register(client, unique_email).status_code == 200
    otp_delivery.wait_for_pending(2.0)

    assert posts == []


def test_a_rolled_back_registration_leaves_no_challenge_and_no_email(
    outbox_on: Any,
) -> None:
    address = new_address()
    payload = RegisterRequest(
        first_name="Ada",
        last_name="Lovelace",
        email=address,
        phone="+994500000000",
        profession="Engineer",
        sponsor_consent=False,
    )

    class Boom(Exception):
        pass

    db = SessionLocal()
    try:
        with pytest.raises(Boom), mutating_transaction(db) as tx:
            assert register_attendee(tx, payload) is not None
            raise Boom  # e.g. a later step of the same request fails
    finally:
        db.close()

    assert count("users", "email = :e", e=address) == 0
    assert count("otp_challenges", "email = :e", e=address) == 0
    assert deliveries_for(address) == []
    provider = FakeProvider()
    assert _worker(provider) == 0
    assert provider.messages == []


def test_issue_otp_rollback_removes_the_outbox_row(outbox_on: Any) -> None:
    address = new_address()
    db = SessionLocal()
    try:
        pending = issue_otp(db, email=address, user_id=None, purpose=OtpPurpose.LOGIN)
        assert pending.queued is True
        db.rollback()
    finally:
        db.close()
    assert count("otp_challenges", "email = :e", e=address) == 0
    assert deliveries_for(address) == []


def test_without_an_encryption_key_the_legacy_sender_is_used_and_no_row_is_written(
    client: TestClient,
    unique_email: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_settings(monkeypatch, outbox_settings(otp_outbox_encryption_key=None))
    sent: list[Any] = []

    def record(*a: Any, **k: Any) -> _OkResponse:
        sent.append(k)
        return _OkResponse()

    monkeypatch.setattr(httpx, "post", record)

    assert _register(client, unique_email).status_code == 200
    otp_delivery.wait_for_pending(5.0)

    assert deliveries_for(unique_email) == []
    assert len(sent) == 1


class _OkResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None


def test_login_request_queues_a_login_code_and_unknown_emails_queue_nothing(
    client: TestClient, unique_email: str, outbox_on: Any
) -> None:
    assert _register(client, unique_email).status_code == 200
    # Activate the account so a later request is a LOGIN one.
    first = deliveries_for(unique_email)[0]
    code = peek_code(first["id"])
    assert client.post(OTP_VERIFY, json={"email": unique_email, "code": code}).status_code == 200

    known = client.post(OTP_REQUEST, json={"email": unique_email})
    unknown = client.post(OTP_REQUEST, json={"email": new_address()})

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()  # no membership oracle
    rows = deliveries_for(unique_email)
    assert [r["purpose"] for r in rows] == ["ACTIVATION", "LOGIN"]
    assert count("otp_deliveries") == 2  # nothing queued for the unknown address


# --------------------------------------------------------------------------
# The worker: accepted, retry, failure, idempotency
# --------------------------------------------------------------------------


def test_worker_sends_and_records_provider_acceptance_not_delivery() -> None:
    _, delivery_id, address = make_delivery(code="482913")
    provider = FakeProvider(accepted("msg_abc123"))

    assert _worker(provider) == 1

    r = row(delivery_id)
    assert r["status"] == "accepted"
    assert r["provider_message_id"] == "msg_abc123"
    assert r["attempt_count"] == 1
    assert r["accepted_at"] is not None and r["finished_at"] is not None
    assert r["payload_ciphertext"] is None and r["payload_key_id"] is None
    sent = provider.messages[0]
    assert sent.to == address
    assert provider.code_in() == "482913"
    assert sent.idempotency_key == r["idempotency_key"]
    assert _worker(provider) == 0  # finished rows are never picked up again


def test_transient_failures_are_retried_with_one_stable_idempotency_key() -> None:
    _, delivery_id, _ = make_delivery()
    provider = FakeProvider(retryable(), retryable("provider_rate_limited", 429), accepted())

    assert _worker(provider) == 1
    r = row(delivery_id)
    assert r["status"] == "pending" and r["reason_code"] == "provider_unavailable"
    assert r["payload_ciphertext"] is not None  # still needed for the next attempt
    assert r["next_attempt_at"] > datetime.now(UTC)  # backed off
    assert _worker(provider) == 0  # not due yet

    make_due(delivery_id)
    assert _worker(provider) == 1
    assert row(delivery_id)["status"] == "pending"
    make_due(delivery_id)
    assert _worker(provider) == 1

    r = row(delivery_id)
    assert r["status"] == "accepted" and r["attempt_count"] == 3
    keys = {m.idempotency_key for m in provider.messages}
    assert keys == {r["idempotency_key"]} and len(provider.messages) == 3


def test_a_permanent_provider_rejection_fails_at_once_and_wipes_the_code() -> None:
    _, delivery_id, _ = make_delivery()
    provider = FakeProvider(permanent("provider_auth_rejected", 403))

    _worker(provider)

    r = row(delivery_id)
    assert r["status"] == "failed" and r["reason_code"] == "provider_auth_rejected"
    assert r["last_http_status"] == 403 and r["attempt_count"] == 1
    assert r["payload_ciphertext"] is None
    assert len(provider.messages) == 1  # no retry of something that cannot succeed


def test_retries_are_bounded() -> None:
    _, delivery_id, _ = make_delivery()
    provider = FakeProvider(retryable())

    for _ in range(ob.MAX_ATTEMPTS + 3):
        make_due(delivery_id)
        _worker(provider)

    r = row(delivery_id)
    assert r["status"] == "failed" and r["reason_code"] == "provider_unavailable"
    assert r["attempt_count"] == ob.MAX_ATTEMPTS == len(provider.messages)
    assert r["payload_ciphertext"] is None


def test_no_retry_is_scheduled_past_the_code_lifetime() -> None:
    _, delivery_id, _ = make_delivery(ttl=timedelta(seconds=2))  # shorter than any backoff
    provider = FakeProvider(retryable())

    _worker(provider)

    r = row(delivery_id)
    assert r["status"] == "expired" and r["reason_code"] == ob.REASON_OTP_EXPIRED
    assert r["payload_ciphertext"] is None


# --------------------------------------------------------------------------
# Restart, leases and parallel workers
# --------------------------------------------------------------------------


def test_queued_deliveries_survive_a_restart_and_are_sent_by_a_new_worker() -> None:
    ids = [make_delivery()[1] for _ in range(3)]
    # No worker ran at all (web process kept going, worker was down/restarting).
    assert all(row(i)["status"] == "pending" for i in ids)

    provider = FakeProvider()
    sent = 0
    while (n := _worker(provider, worker_id="after-restart")) > 0:
        sent += n

    assert sent == 3
    assert {row(i)["status"] for i in ids} == {"accepted"}


def test_a_crashed_worker_lease_is_recovered_and_the_late_worker_cannot_overwrite() -> None:
    _, delivery_id, _ = make_delivery()
    first = ob.claim_next(SessionLocal, worker_id="worker-A", limit=5)
    assert [c.id for c in first] == [delivery_id]
    assert row(delivery_id)["status"] == "processing"

    # A live lease is respected: nobody else may take it.
    assert ob.claim_next(SessionLocal, worker_id="worker-B", limit=5) == []

    # Worker A dies; its lease runs out; worker B takes over.
    expire_lease(delivery_id)
    second = ob.claim_next(SessionLocal, worker_id="worker-B", limit=5)
    assert [c.id for c in second] == [delivery_id]
    assert second[0].attempt_count == 2
    assert second[0].idempotency_key == first[0].idempotency_key

    # Worker A was only presumed dead. It wakes up and finishes: its result
    # must be discarded, and the key it sent is the same one B will send.
    late = FakeProvider()
    assert ob.process_delivery(SessionLocal, first[0], provider=late, worker_id="worker-A") == (
        "lease_lost"
    )
    assert row(delivery_id)["status"] == "processing"
    assert row(delivery_id)["locked_by"] == "worker-B"

    winner = FakeProvider()
    assert ob.process_delivery(SessionLocal, second[0], provider=winner, worker_id="worker-B") == (
        "accepted"
    )
    assert late.messages[0].idempotency_key == winner.messages[0].idempotency_key


def test_a_row_whose_workers_keep_dying_is_given_up_on() -> None:
    _, delivery_id, _ = make_delivery()
    execute(
        "UPDATE otp_deliveries SET status = 'processing', attempt_count = :n, "
        "locked_by = 'ghost', locked_until = now() - interval '1 second' WHERE id = :i",
        n=ob.MAX_ATTEMPTS,
        i=delivery_id,
    )

    provider = FakeProvider()
    assert _worker(provider) == 0

    r = row(delivery_id)
    assert r["status"] == "failed" and r["reason_code"] == ob.REASON_WORKER_LOST
    assert r["payload_ciphertext"] is None and provider.messages == []


def test_parallel_workers_never_claim_the_same_row() -> None:
    ids = {make_delivery()[1] for _ in range(24)}
    barrier = threading.Barrier(6)

    def claim(n: int) -> list[int]:
        barrier.wait()
        claimed: list[int] = []
        while batch := ob.claim_next(SessionLocal, worker_id=f"w{n}", limit=3):
            claimed.extend(c.id for c in batch)
        return claimed

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(claim, range(6)))

    flat = [i for r in results for i in r]
    assert len(flat) == len(set(flat)) == 24  # each row exactly once
    assert set(flat) == ids
    assert {row(i)["attempt_count"] for i in ids} == {1}


# --------------------------------------------------------------------------
# Expired, consumed and superseded codes are never sent late
# --------------------------------------------------------------------------


def test_an_expired_code_is_never_sent_and_its_payload_is_wiped() -> None:
    _, delivery_id, _ = make_delivery()
    execute(
        "UPDATE otp_deliveries SET expires_at = now() - interval '1 minute' WHERE id = :i",
        i=delivery_id,
    )
    provider = FakeProvider()

    _worker(provider)

    r = row(delivery_id)
    assert r["status"] == "expired" and r["reason_code"] == ob.REASON_OTP_EXPIRED
    assert r["payload_ciphertext"] is None and r["finished_at"] is not None
    assert provider.messages == []


def test_a_challenge_that_lapsed_is_not_sent_even_if_the_row_looks_live() -> None:
    challenge_id, delivery_id, _ = make_delivery()
    execute(
        "UPDATE otp_challenges SET expires_at = now() - interval '1 second' WHERE id = :c",
        c=challenge_id,
    )
    provider = FakeProvider()

    _worker(provider)

    assert row(delivery_id)["status"] == "expired"
    assert provider.messages == []


def test_a_used_code_is_not_sent_afterwards() -> None:
    challenge_id, delivery_id, _ = make_delivery()
    execute("UPDATE otp_challenges SET consumed_at = now() WHERE id = :c", c=challenge_id)
    provider = FakeProvider()

    _worker(provider)

    r = row(delivery_id)
    assert r["status"] == "expired" and r["reason_code"] == ob.REASON_CHALLENGE_CONSUMED
    assert r["payload_ciphertext"] is None and provider.messages == []


def test_a_code_consumed_between_claim_and_send_is_not_sent() -> None:
    challenge_id, delivery_id, _ = make_delivery()
    claimed = ob.claim_next(SessionLocal, worker_id="w", limit=1)
    execute("UPDATE otp_challenges SET consumed_at = now() WHERE id = :c", c=challenge_id)
    provider = FakeProvider()

    assert ob.process_delivery(SessionLocal, claimed[0], provider=provider, worker_id="w") == (
        "expired"
    )
    assert provider.messages == [] and row(delivery_id)["payload_ciphertext"] is None


def test_a_superseded_code_is_not_sent_but_the_newer_one_is(
    client: TestClient, unique_email: str, outbox_on: Any
) -> None:
    assert _register(client, unique_email).status_code == 200
    assert client.post(OTP_REQUEST, json={"email": unique_email}).status_code == 200  # newer code
    old, new = deliveries_for(unique_email)
    new_code = peek_code(new["id"])

    provider = FakeProvider()
    while _worker(provider):
        pass

    assert row(old["id"])["status"] == "expired"
    assert row(old["id"])["reason_code"] == ob.REASON_CHALLENGE_CONSUMED
    assert row(new["id"])["status"] == "accepted"
    assert len(provider.messages) == 1
    assert provider.code_in() == new_code
    assert row(old["id"])["payload_ciphertext"] is None


# --------------------------------------------------------------------------
# Verification behaviour is unchanged (hash, expiry, single use, limits)
# --------------------------------------------------------------------------


def test_the_emailed_code_logs_in_once_and_only_once(
    client: TestClient, unique_email: str, outbox_on: Any
) -> None:
    assert _register(client, unique_email).status_code == 200
    provider = FakeProvider()
    _worker(provider)
    code = provider.code_in()

    wrong = "000000" if code != "000000" else "111111"
    assert client.post(OTP_VERIFY, json={"email": unique_email, "code": wrong}).status_code == 400
    ok = client.post(OTP_VERIFY, json={"email": unique_email, "code": code})
    assert ok.status_code == 200 and "session" in ok.cookies
    replay = client.post(OTP_VERIFY, json={"email": unique_email, "code": code})
    assert replay.status_code == 400

    # Only the Argon2id hash is stored for verification; the outbox never
    # held the code in the clear.
    stored = fetch_one("SELECT code_hash FROM otp_challenges WHERE email = :e", e=unique_email)
    assert stored[0].startswith("$argon2id$") and code not in stored[0]


def test_attempt_limit_and_expiry_still_apply_to_outbox_codes(
    client: TestClient, unique_email: str, outbox_on: Any
) -> None:
    assert _register(client, unique_email).status_code == 200
    delivery = deliveries_for(unique_email)[0]
    code = peek_code(delivery["id"])
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        client.post(OTP_VERIFY, json={"email": unique_email, "code": wrong})
    assert client.post(OTP_VERIFY, json={"email": unique_email, "code": code}).status_code == 400

    execute("UPDATE otp_challenges SET attempt_count = 0 WHERE email = :e", e=unique_email)
    execute(
        "UPDATE otp_challenges SET expires_at = now() - interval '1 second' WHERE email = :e",
        e=unique_email,
    )
    assert client.post(OTP_VERIFY, json={"email": unique_email, "code": code}).status_code == 400


# --------------------------------------------------------------------------
# Encryption and payload cleanup
# --------------------------------------------------------------------------


def test_the_code_is_not_stored_in_the_clear() -> None:
    _, delivery_id, _ = make_delivery(code="739104")
    blob = bytes(row(delivery_id)["payload_ciphertext"])
    assert b"739104" not in blob and blob[:1] == b"\x01"
    found = fetch_one(
        "SELECT count(*) FROM otp_deliveries "
        "WHERE position('739104'::bytea in payload_ciphertext) > 0"
    )
    assert found[0] == 0
    assert peek_code(delivery_id) == "739104"


def test_ciphertexts_are_unique_and_bound_to_their_row() -> None:
    settings = outbox_settings()
    a1, kid = encrypt_code(
        "123456", idempotency_key="k1", email="a@x.test", purpose="LOGIN", settings=settings
    )
    a2, _ = encrypt_code(
        "123456", idempotency_key="k1", email="a@x.test", purpose="LOGIN", settings=settings
    )
    assert a1 != a2  # fresh nonce each time
    assert kid == key_fingerprint(__import__("base64").urlsafe_b64decode(TEST_KEY))

    def read(ct: bytes, **over: str) -> str:
        args = {"idempotency_key": "k1", "email": "a@x.test", "purpose": "LOGIN", **over}
        return decrypt_code(ct, kid, settings=settings, **args)

    assert read(a1) == "123456"
    for tampered in (
        {"idempotency_key": "k2"},
        {"email": "b@x.test"},
        {"purpose": "ACTIVATION"},
    ):
        with pytest.raises(OtpCryptoError):
            read(a1, **tampered)
    flipped = bytearray(a1)
    flipped[-1] ^= 1
    with pytest.raises(OtpCryptoError):
        read(bytes(flipped))


def test_wrong_or_missing_keys_cannot_read_a_payload_and_rotation_works() -> None:
    old = outbox_settings()
    ct, kid = encrypt_code(
        "246810", idempotency_key="k", email="a@x.test", purpose="LOGIN", settings=old
    )
    kwargs = {"idempotency_key": "k", "email": "a@x.test", "purpose": "LOGIN"}

    other = outbox_settings(otp_outbox_encryption_key=OTHER_KEY)
    with pytest.raises(OtpCryptoError):
        decrypt_code(ct, kid, settings=other, **kwargs)
    with pytest.raises(OtpCryptoError):
        decrypt_code(ct, kid, settings=outbox_settings(otp_outbox_encryption_key=None), **kwargs)

    rotated = outbox_settings(
        otp_outbox_encryption_key=OTHER_KEY, otp_outbox_encryption_key_previous=TEST_KEY
    )
    assert decrypt_code(ct, kid, settings=rotated, **kwargs) == "246810"
    with pytest.raises(OtpCryptoError):
        encrypt_code(
            "1",
            idempotency_key="k",
            email="a",
            purpose="LOGIN",
            settings=outbox_settings(otp_outbox_encryption_key=None),
        )


def test_a_payload_that_cannot_be_decrypted_fails_safely_without_sending() -> None:
    _, delivery_id, _ = make_delivery()
    execute(
        "UPDATE otp_deliveries SET payload_key_id = 'unknown-key-id' WHERE id = :i", i=delivery_id
    )
    provider = FakeProvider()

    _worker(provider)

    r = row(delivery_id)
    assert r["status"] == "failed" and r["reason_code"] == ob.REASON_PAYLOAD_UNREADABLE
    assert provider.messages == [] and r["payload_ciphertext"] is None


@pytest.mark.parametrize("final", ["accepted", "failed", "expired"])
def test_the_database_refuses_a_ciphertext_on_a_finished_row(final: str) -> None:
    _, delivery_id, _ = make_delivery()
    with pytest.raises(IntegrityError, match="payload_only_while_live"):
        execute(
            "UPDATE otp_deliveries SET status = :s, finished_at = now(), "
            "accepted_at = CASE WHEN :s = 'accepted' THEN now() END WHERE id = :i",
            s=final,
            i=delivery_id,
        )


def test_every_terminal_path_leaves_no_payload() -> None:
    cases = [
        (FakeProvider(accepted()), "accepted"),
        (FakeProvider(permanent()), "failed"),
    ]
    for provider, expected in cases:
        _, delivery_id, _ = make_delivery()
        _worker(provider)
        r = row(delivery_id)
        assert r["status"] == expected
        assert r["payload_ciphertext"] is None and r["payload_key_id"] is None
    assert count("otp_deliveries", "payload_ciphertext IS NOT NULL") == 0


# --------------------------------------------------------------------------
# Languages
# --------------------------------------------------------------------------

_LANGUAGE_MARKERS = {
    "az": ("təsdiq", "dəqiqə"),
    "en": ("verification code", "10 minutes"),
    "ru": ("код подтверждения", "10 минут"),
}


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_email_is_written_in_the_language_requested_at_registration(
    client: TestClient, unique_email: str, outbox_on: Any, language: str
) -> None:
    assert _register(client, unique_email, language=language).status_code == 200
    assert deliveries_for(unique_email)[0]["language"] == language

    provider = FakeProvider()
    _worker(provider)

    message = provider.messages[0]
    code = provider.code_in()
    subject_marker, body_marker = _LANGUAGE_MARKERS[language]
    assert subject_marker in message.subject and message.subject.startswith(code)
    assert body_marker in message.text and body_marker in message.html
    assert f'lang="{language}"' in message.html


def test_login_requests_carry_their_own_language(
    client: TestClient, unique_email: str, outbox_on: Any
) -> None:
    assert _register(client, unique_email, language="en").status_code == 200
    code = peek_code(deliveries_for(unique_email)[0]["id"])
    assert client.post(OTP_VERIFY, json={"email": unique_email, "code": code}).status_code == 200

    assert (
        client.post(OTP_REQUEST, json={"email": unique_email, "language": "ru"}).status_code == 200
    )

    languages = [d["language"] for d in deliveries_for(unique_email)]
    assert languages == ["en", "ru"]
    provider = FakeProvider()
    while _worker(provider):
        pass
    assert any("код для входа" in m.subject for m in provider.messages)


def test_old_clients_without_a_language_get_the_default_and_bad_values_are_rejected(
    client: TestClient, unique_email: str, outbox_on: Any
) -> None:
    assert _register(client, unique_email).status_code == 200  # no `language` sent
    assert deliveries_for(unique_email)[0]["language"] == "en"

    assert (
        client.post(OTP_REQUEST, json={"email": unique_email, "language": " AZ "}).status_code
        == 200
    )
    assert deliveries_for(unique_email)[-1]["language"] == "az"  # case/whitespace normalised

    before = count("otp_deliveries")
    for bad in ("de", "az-AZ", "", "english", 7, None):
        r = client.post(OTP_REQUEST, json={"email": unique_email, "language": bad})
        assert r.status_code == 422, bad
        assert _register(client, new_address(), language=bad).status_code == 422, bad
    assert count("otp_deliveries") == before  # nothing queued for a rejected request


def test_templates_escape_html_and_match_the_otp_lifetime() -> None:
    assert EXPIRY_MINUTES == OTP_EXPIRY_MINUTES == 10
    for language in LANGUAGES:
        content = render_otp_email(language, OtpPurpose.LOGIN, "<b>1</b>")
        assert "<b>1</b>" not in content.html and "&lt;b&gt;1&lt;/b&gt;" in content.html
    assert render_otp_email("xx", OtpPurpose.LOGIN, "123456").subject.endswith("sign-in code")


# --------------------------------------------------------------------------
# The real Resend adapter: classification and what it never reveals
# --------------------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, body: Any = None, headers: dict[str, str] | None = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("not json")
        return self._body


@pytest.mark.parametrize(
    ("response", "accepted_", "reason", "retryable_", "retry_after"),
    [
        (_Resp(200, {"id": "re_msg-1"}), True, None, False, None),
        (_Resp(200), True, None, False, None),  # success with an unreadable body
        (_Resp(401, {"message": "bad key"}), False, "provider_auth_rejected", False, None),
        (_Resp(403), False, "provider_auth_rejected", False, None),
        (_Resp(422, {"message": "invalid to"}), False, "provider_rejected_message", False, None),
        (_Resp(400), False, "provider_rejected_message", False, None),
        (_Resp(409), False, "provider_conflict", True, None),
        (_Resp(429, headers={"retry-after": "7"}), False, "provider_rate_limited", True, 7.0),
        (_Resp(500), False, "provider_unavailable", True, None),
        (_Resp(503), False, "provider_unavailable", True, None),
    ],
)
def test_resend_adapter_classifies_responses(
    monkeypatch: pytest.MonkeyPatch,
    response: _Resp,
    accepted_: bool,
    reason: str | None,
    retryable_: bool,
    retry_after: float | None,
) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _Resp:
        captured.update(url=url, **kwargs)
        return response

    monkeypatch.setattr(httpx, "post", fake_post)
    _, delivery_id, address = make_delivery()
    claimed = ob.claim_next(SessionLocal, worker_id="w", limit=1)[0]

    result = ResendProvider(FAKE_API_KEY).send(
        __import__("app.modules.auth.otp_provider", fromlist=["EmailMessage"]).EmailMessage(
            from_email="FACEMARK <info@facemark-tests.net>",
            to=address,
            subject="s",
            text="t",
            html="h",
            idempotency_key=claimed.idempotency_key,
        )
    )

    assert (result.accepted, result.reason_code, result.retryable) == (
        accepted_,
        reason,
        retryable_,
    )
    assert result.retry_after_seconds == retry_after
    assert captured["headers"]["Idempotency-Key"] == claimed.idempotency_key
    assert captured["headers"]["Authorization"] == f"Bearer {FAKE_API_KEY}"
    assert captured["json"]["to"] == [address]
    assert not hasattr(result, "body")


def test_resend_adapter_network_failures_are_retryable_and_unexpected_ones_are_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.auth.otp_provider import EmailMessage

    message = EmailMessage("f@x.test", "a@x.test", "s", "t", "h", "k")

    def timeout(*a: Any, **k: Any) -> None:
        raise httpx.ConnectTimeout("secret a@x.test 123456")

    monkeypatch.setattr(httpx, "post", timeout)
    r = ResendProvider(FAKE_API_KEY).send(message)
    assert (r.accepted, r.reason_code, r.retryable) == (False, "network_error", True)

    def odd(*a: Any, **k: Any) -> None:
        raise ValueError("secret a@x.test 123456")

    monkeypatch.setattr(httpx, "post", odd)
    r = ResendProvider(FAKE_API_KEY).send(message)
    assert (r.accepted, r.reason_code, r.retryable) == (False, "unexpected_error", False)


# --------------------------------------------------------------------------
# Nothing sensitive reaches the logs
# --------------------------------------------------------------------------


def test_worker_logs_never_contain_addresses_codes_keys_or_provider_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    leaks = "SECRET-PROVIDER-TEXT"
    _, d1, address1 = make_delivery(code="918273")

    # d1: the real adapter, and a provider body that quotes everything sensitive.
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _Resp(422, {"message": f"{leaks} {address1} 918273 {FAKE_API_KEY}"}),
    )
    _worker(_ProviderViaHttpx())

    # d2: a provider that blows up with a message full of secrets.
    _, d2, address2 = make_delivery(code="555666")
    claimed = ob.claim_next(SessionLocal, worker_id="x", limit=1)[0]
    outcome = ob.process_delivery(
        SessionLocal,
        claimed,
        provider=ExplodingProvider(f"{leaks} {address2} 555666 {FAKE_API_KEY}"),
        worker_id="x",
    )

    assert outcome == "error"
    for secret in (leaks, address1, address2, "918273", "555666", FAKE_API_KEY):
        assert secret not in caplog.text, secret
    assert "OTP delivery" in caplog.text  # it did log - just safely
    stored = str(dict(row(d1)))
    assert row(d1)["status"] == "failed" and row(d1)["reason_code"] == "provider_rejected_message"
    assert leaks not in stored and address1 not in stored.replace(f"'email': '{address1}'", "")
    assert row(d2)["status"] == "processing"  # left for lease recovery, not lost


class _ProviderViaHttpx:
    """The real Resend adapter, with httpx.post already monkeypatched."""

    def send(self, message: Any) -> Any:
        return ResendProvider(FAKE_API_KEY).send(message)


def test_claimed_delivery_repr_hides_the_address_and_ciphertext() -> None:
    make_delivery(code="135790")
    claimed = ob.claim_next(SessionLocal, worker_id="w", limit=1)[0]
    shown = repr(claimed) + str(claimed)
    assert claimed.email not in shown and "135790" not in shown and "payload" not in shown


def test_requests_in_outbox_mode_do_not_log_the_code_or_address_without_demo_mode(
    client: TestClient,
    unique_email: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    install_settings(monkeypatch, outbox_settings(demo_mode=False))
    caplog.set_level(logging.DEBUG)

    assert _register(client, unique_email).status_code == 200
    code = peek_code(deliveries_for(unique_email)[0]["id"])
    provider = FakeProvider()
    _worker(provider)
    assert client.post(OTP_VERIFY, json={"email": unique_email, "code": code}).status_code == 200

    assert code not in caplog.text and unique_email not in caplog.text
    assert "[DEMO_MODE]" not in caplog.text


def test_demo_mode_still_logs_and_lists_the_code_alongside_the_outbox(
    client: TestClient,
    unique_email: str,
    monkeypatch: pytest.MonkeyPatch,
    otp_log: pytest.LogCaptureFixture,
) -> None:
    install_settings(monkeypatch, outbox_settings(demo_mode=True))
    assert _register(client, unique_email).status_code == 200

    shown = {d["email"]: d["code"] for d in otp_delivery.recent_demo_otps()}
    assert shown.get(unique_email) == peek_code(deliveries_for(unique_email)[0]["id"])
    assert any("[DEMO_MODE] OTP for" in r.getMessage() for r in otp_log.records)


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


def test_purge_removes_only_old_finished_rows() -> None:
    old_done = make_delivery()[1]
    recent_done = make_delivery()[1]
    live = make_delivery()[1]
    _worker(FakeProvider())  # all three accepted... then rewind two and re-open one
    execute(
        "UPDATE otp_deliveries SET finished_at = now() - interval '40 days' WHERE id = :i",
        i=old_done,
    )
    execute(
        "UPDATE otp_deliveries SET status = 'pending', accepted_at = NULL, finished_at = NULL, "
        "next_attempt_at = now() + interval '1 hour', payload_ciphertext = '\\x01', "
        "payload_key_id = 'k' WHERE id = :i",
        i=live,
    )

    db = SessionLocal()
    try:
        with mutating_transaction(db) as tx:
            assert ob.purge_finished(tx, older_than_days=30) == 1
    finally:
        db.close()

    assert count("otp_deliveries", "id = :i", i=old_done) == 0
    assert count("otp_deliveries", "id = :i", i=recent_done) == 1
    assert count("otp_deliveries", "id = :i", i=live) == 1
    with pytest.raises(ValueError):
        ob.purge_finished(SessionLocal(), older_than_days=0)
