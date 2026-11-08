# ruff: noqa: E501  (long literals in assertions)
"""Staff and administrator passwords: changing one while signed in, and the
emailed one-time reset (app/modules/staff/password.py).

Runs against the disposable reset-test database (a scratch database created by
this run only, see tests/suite_db.py). No real email is ever sent: the outbox
worker is driven by `FakeProvider`, and `no_real_email` (conftest) makes any
stray `httpx.post` fail the test.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.ratelimit import (
    ADMIN_RESET_START_PER_ADMIN,
    STAFF_PASSWORD_CHANGE_PER_STAFF,
    STAFF_RESET_REQUEST_PER_EMAIL,
    RateLimit,
)
from app.core.security import hash_otp_code, verify_secret
from app.db.session import mutating_transaction
from app.modules.admin import audit as audit_view
from app.modules.attendees.models import OtpPurpose
from app.modules.auth import otp_delivery
from app.modules.auth import otp_outbox as outbox
from app.modules.auth.otp_email import EXPIRY_MINUTES, render_otp_email
from app.modules.staff import password as staff_password
from app.modules.staff.models import StaffRole
from tests.api_rig import Rig
from tests.outbox_support import (
    FakeProvider,
    accepted,
    install_settings,
    outbox_settings,
    retryable,
)
from tests.reset_support import SEED_ADMIN_PASSWORD, SEED_STAFF_PASSWORD

LOGIN = "/api/staff/auth/login"
CHANGE = "/api/staff/auth/password"
RESET_REQUEST = "/api/staff/auth/password-reset/request"
RESET_CONFIRM = "/api/staff/auth/password-reset/confirm"
ME = "/api/staff/me"

STAFF_EMAIL = "aiworks@staff.facemark.demo"
ADMIN_EMAIL = "admin@facemark.demo"
NEW_PASSWORD = "A-brand-new-Passw0rd-9!"  # noqa: S105
OTHER_NEW_PASSWORD = "Another-fresh-Passw0rd-7?"  # noqa: S105
GENERIC = {
    "message": "If this address belongs to an active staff account, a reset code has been sent to it."
}


def _error(response: Any) -> tuple[int, str]:
    return int(response.status_code), str(response.json()["error"]["code"])


def _login(rig: Rig, email: str, password: str) -> Any:
    return rig.client.post(LOGIN, json={"email": email, "password": password})


def _change(
    rig: Rig, current: str, new: str = NEW_PASSWORD, confirmation: str | None = None
) -> Any:
    return rig.client.post(
        CHANGE,
        json={
            "current_password": current,
            "new_password": new,
            "new_password_confirmation": new if confirmation is None else confirmation,
        },
    )


def _sessions(rig: Rig, staff_id: int) -> tuple[int, int]:
    """(live sessions, revoked sessions) of the account."""
    row = rig.sql(
        "SELECT count(*) FILTER (WHERE revoked_at IS NULL), count(*) FILTER (WHERE revoked_at IS NOT NULL) "
        "FROM sessions WHERE staff_account_id = :i",
        i=staff_id,
    )[0]
    return int(row[0]), int(row[1])


def _staff_id(rig: Rig, email: str) -> int:
    return int(rig.scalar("SELECT id FROM staff_accounts WHERE email = :e", e=email))


# --- changing the password while signed in -----------------------------------------


def test_changing_the_password_ends_every_session_and_the_new_password_signs_in(rig: Rig) -> None:
    staff_id = _staff_id(rig, STAFF_EMAIL)
    assert _login(rig, STAFF_EMAIL, SEED_STAFF_PASSWORD).status_code == 204
    rig.session_for(staff_id)  # a second device, still signed in
    old_cookie = rig.client.cookies.get("session")
    assert _sessions(rig, staff_id)[0] >= 2
    old_hash = rig.scalar("SELECT password_hash FROM staff_accounts WHERE id = :i", i=staff_id)

    response = _change(rig, SEED_STAFF_PASSWORD)

    assert response.status_code == 204, response.text
    live, revoked = _sessions(rig, staff_id)
    assert live == 0 and revoked >= 2  # every session, the current one included
    assert "session=" in response.headers.get("set-cookie", "")  # the cookie is cleared
    rig.client.cookies.set("session", old_cookie or "")
    assert rig.client.get(ME).status_code == 401  # the old session no longer opens anything
    rig.client.cookies.clear()
    assert _error(_login(rig, STAFF_EMAIL, SEED_STAFF_PASSWORD)) == (401, "INVALID_CREDENTIALS")
    assert _login(rig, STAFF_EMAIL, NEW_PASSWORD).status_code == 204
    new_hash = rig.scalar("SELECT password_hash FROM staff_accounts WHERE id = :i", i=staff_id)
    assert (
        new_hash != old_hash and new_hash.startswith("$argon2id$") and NEW_PASSWORD not in new_hash
    )
    assert rig.client.get(ME).status_code == 200  # and the fresh session works


def test_an_administrator_changes_their_own_password_the_same_way(rig: Rig) -> None:
    admin_id = _staff_id(rig, ADMIN_EMAIL)
    assert _login(rig, ADMIN_EMAIL, SEED_ADMIN_PASSWORD).status_code == 204
    assert _change(rig, SEED_ADMIN_PASSWORD).status_code == 204
    assert _sessions(rig, admin_id)[0] == 0
    assert _login(rig, ADMIN_EMAIL, NEW_PASSWORD).status_code == 204
    assert rig.client.get("/api/admin/staff").status_code == 200


def test_a_wrong_current_password_changes_nothing_and_counts_like_a_wrong_login(rig: Rig) -> None:
    staff_id = _staff_id(rig, STAFF_EMAIL)
    _login(rig, STAFF_EMAIL, SEED_STAFF_PASSWORD)
    before = rig.scalar("SELECT password_hash FROM staff_accounts WHERE id = :i", i=staff_id)

    response = _change(rig, "not-the-current-password-1")

    assert _error(response) == (400, "CURRENT_PASSWORD_INVALID")
    assert "not-the-current" not in response.text and NEW_PASSWORD not in response.text
    assert (
        rig.scalar("SELECT password_hash FROM staff_accounts WHERE id = :i", i=staff_id) == before
    )
    assert (
        rig.scalar("SELECT failed_login_count FROM staff_accounts WHERE id = :i", i=staff_id) == 1
    )
    assert _sessions(rig, staff_id)[0] == 1  # the session survives a failed attempt
    assert rig.client.get(ME).status_code == 200
    assert rig.audit_rows("staff.password_change") == []


def test_a_locked_account_cannot_change_its_password_and_a_success_clears_the_counter(
    rig: Rig,
) -> None:
    staff_id = _staff_id(rig, STAFF_EMAIL)
    _login(rig, STAFF_EMAIL, SEED_STAFF_PASSWORD)
    rig.sql(
        "UPDATE staff_accounts SET failed_login_count = 10, locked_until = now() + interval '10 minutes' "
        "WHERE id = :i",
        i=staff_id,
    )
    assert _error(_change(rig, SEED_STAFF_PASSWORD)) == (429, "ACCOUNT_LOCKED")

    rig.sql(
        "UPDATE staff_accounts SET locked_until = NULL, failed_login_count = 4 WHERE id = :i",
        i=staff_id,
    )
    assert _change(rig, SEED_STAFF_PASSWORD).status_code == 204
    assert (
        rig.scalar("SELECT failed_login_count FROM staff_accounts WHERE id = :i", i=staff_id) == 0
    )


def test_guessing_the_current_password_is_rate_limited(rig: Rig) -> None:
    _login(rig, STAFF_EMAIL, SEED_STAFF_PASSWORD)
    results = [_change(rig, f"wrong-guess-number-{n}-xx").status_code for n in range(7)]
    assert results[:5] == [400] * 5 and results[5:] == [429, 429]
    assert STAFF_PASSWORD_CHANGE_PER_STAFF.limit == 5


def test_the_new_password_must_differ_follow_the_policy_and_be_typed_twice(rig: Rig) -> None:
    staff_id = _staff_id(rig, STAFF_EMAIL)
    _login(rig, STAFF_EMAIL, SEED_STAFF_PASSWORD)
    same = _change(rig, SEED_STAFF_PASSWORD, new=SEED_STAFF_PASSWORD)
    assert _error(same) == (400, "PASSWORD_UNCHANGED")

    too_short = _change(rig, SEED_STAFF_PASSWORD, new="short-1!")
    assert _error(too_short) == (422, "VALIDATION_ERROR")
    too_long = _change(rig, SEED_STAFF_PASSWORD, new="x" * 257)
    assert _error(too_long) == (422, "VALIDATION_ERROR")
    exactly_twelve = "twelve-chars"
    assert len(exactly_twelve) == 12
    mismatch = _change(rig, SEED_STAFF_PASSWORD, new=NEW_PASSWORD, confirmation=OTHER_NEW_PASSWORD)
    assert _error(mismatch) == (422, "VALIDATION_ERROR")
    for response in (too_short, too_long, mismatch, same):  # no password is ever echoed back
        for secret in (SEED_STAFF_PASSWORD, NEW_PASSWORD, OTHER_NEW_PASSWORD, "short-1!"):
            assert secret not in response.text
    extra = rig.client.post(
        CHANGE,
        json={
            "current_password": SEED_STAFF_PASSWORD,
            "new_password": NEW_PASSWORD,
            "new_password_confirmation": NEW_PASSWORD,
            "role": "ADMIN",
            "email": "x@y.test",
        },
    )
    assert extra.status_code == 422
    assert _sessions(rig, staff_id)[0] == 1  # nothing above ended the session
    # the console's own policy: 12 characters is enough
    assert _change(rig, SEED_STAFF_PASSWORD, new=exactly_twelve).status_code == 204


def test_only_a_signed_in_active_staff_member_may_change_a_password(rig: Rig) -> None:
    rig.sign_out()
    assert _change(rig, SEED_STAFF_PASSWORD).status_code == 401
    user_id = rig.make_user("pw")
    rig.sign_in_attendee(user_id)
    assert _change(rig, SEED_STAFF_PASSWORD).status_code == 401  # an attendee session is not staff

    staff_id = _staff_id(rig, STAFF_EMAIL)
    rig.sign_in_as(staff_id)
    rig.sql("UPDATE staff_accounts SET is_active = false WHERE id = :i", i=staff_id)
    assert _error(_change(rig, SEED_STAFF_PASSWORD)) == (403, "STAFF_NOT_AUTHORIZED")
    assert rig.audit_rows("staff.password_change") == []


def test_a_scan_made_before_the_change_cannot_award_after_it(rig: Rig) -> None:
    booth_id, staff_id = rig.booth_with_staff()
    task_id = rig.make_task(booth_id, points=10, repeatable=False)
    card = "YGNasg-eVR42rUp_bKbockSk2RLE26A2g9bfCUHvXj8"
    assert _login(rig, STAFF_EMAIL, SEED_STAFF_PASSWORD).status_code == 204
    scan = rig.client.post("/api/staff/scan/resolve", json={"card_token": card})
    assert scan.status_code == 200, scan.text
    scan_ref = scan.json()["scan_ref"]
    from app.modules.points.service import compute_rule_revision

    body = {
        "card_token": card,
        "task_id": task_id,
        "rule_revision": compute_rule_revision(task_id=task_id, points=10, repeatable=False),
        "idempotency_key": str(uuid.uuid4()),
        "scan_ref": scan_ref,
    }
    old_cookie = rig.client.cookies.get("session")

    assert _change(rig, SEED_STAFF_PASSWORD).status_code == 204
    assert _login(rig, STAFF_EMAIL, NEW_PASSWORD).status_code == 204  # a NEW session, same person

    refused = rig.client.post("/api/staff/points/award", json=body)
    assert _error(refused) == (403, "STAFF_NOT_AUTHORIZED")  # the scan belonged to the dead session
    rig.client.cookies.set("session", old_cookie or "")
    assert rig.client.post("/api/staff/points/award", json=body).status_code == 401
    assert rig.scalar("SELECT count(*) FROM points_log WHERE task_id = :t", t=task_id) == 0
    assert staff_id == _staff_id(rig, STAFF_EMAIL)


def test_a_staff_session_opens_no_realtime_stream(rig: Rig) -> None:
    """The streams are attendees'; a staff session is not one, so a staff
    password change has no stream to end and no stream accepts a staff cookie."""
    rig.sign_in(StaffRole.BOOTH_STAFF)
    assert rig.client.get("/api/realtime/stream").status_code == 401


def test_the_change_is_audited_without_any_secret(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    staff_id = _staff_id(rig, STAFF_EMAIL)
    _login(rig, STAFF_EMAIL, SEED_STAFF_PASSWORD)
    _change(rig, "wrong-current-password-q")  # a failure too
    assert _change(rig, SEED_STAFF_PASSWORD).status_code == 204

    rows = rig.audit_rows("staff.password_change")
    assert len(rows) == 1
    row = rows[0]
    assert row["staff_account_id"] == staff_id
    assert (row["target_type"], row["target_id"]) == ("staff_account", str(staff_id))
    assert row["before_value"] is None
    assert (
        set(row["after_value"]) == {"id", "sessions_revoked"}
        and row["after_value"]["id"] == staff_id
    )

    everything = "".join(str(r) for r in rig.sql("SELECT * FROM admin_audit"))
    for secret in (SEED_STAFF_PASSWORD, NEW_PASSWORD, "wrong-current-password-q", "$argon2"):
        assert secret not in everything
        assert secret not in caplog.text

    rig.sign_in(StaffRole.ADMIN)
    page = rig.client.get("/api/admin/audit", params={"action": "staff.password_change"}).json()
    event = page["items"][0]
    assert event["details"] == "known" and event["hidden_field_count"] == 0
    assert event["actor_id"] == staff_id and event["after"]["id"] == staff_id


def test_a_failed_change_after_the_hash_is_set_changes_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One transaction: if the audit row cannot be written, the new password,
    the lockout reset and the revoked sessions are rolled back together."""
    staff_id = _staff_id(rig, STAFF_EMAIL)
    _login(rig, STAFF_EMAIL, SEED_STAFF_PASSWORD)
    before = rig.scalar("SELECT password_hash FROM staff_accounts WHERE id = :i", i=staff_id)

    def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("audit down")

    monkeypatch.setattr(staff_password, "_audit", boom)
    with pytest.raises(RuntimeError, match="audit down"):
        _change(rig, SEED_STAFF_PASSWORD)

    assert (
        rig.scalar("SELECT password_hash FROM staff_accounts WHERE id = :i", i=staff_id) == before
    )
    assert _sessions(rig, staff_id) == (1, 0)
    monkeypatch.undo()
    assert _change(rig, SEED_STAFF_PASSWORD).status_code == 204  # and a retry works


def test_maintenance_mode_refuses_password_operations(rig: Rig) -> None:
    _login(rig, STAFF_EMAIL, SEED_STAFF_PASSWORD)
    rig.sql("UPDATE event_config SET maintenance_mode = true WHERE id = 1")
    assert _error(_change(rig, SEED_STAFF_PASSWORD)) == (503, "MAINTENANCE")
    assert _error(rig.client.post(RESET_REQUEST, json={"email": STAFF_EMAIL})) == (
        503,
        "MAINTENANCE",
    )
    assert _error(
        rig.client.post(
            RESET_CONFIRM,
            json={
                "email": STAFF_EMAIL,
                "code": "123456",
                "new_password": NEW_PASSWORD,
                "new_password_confirmation": NEW_PASSWORD,
            },
        )
    ) == (503, "MAINTENANCE")


# --- reset by email --------------------------------------------------------------------


@pytest.fixture
def mail(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Mail:
    """The outbox switched on (with a fake provider, never a real one) and a
    way to run the worker once and read the code from the email it 'sent'."""
    settings = install_settings(monkeypatch, outbox_settings())
    monkeypatch.setattr("app.modules.staff.password.get_settings", lambda: settings)
    return Mail(rig, settings)


class Mail:
    def __init__(self, rig: Rig, settings: Settings) -> None:
        self.rig = rig
        self.settings = settings
        self.provider = FakeProvider()

    def factory(self) -> Session:
        return Session(self.rig.engine)

    def send(self, provider: FakeProvider | None = None) -> list[str]:
        """Runs the outbox worker once; returns what happened to each row."""
        provider = provider or self.provider
        claimed = outbox.claim_next(self.factory, worker_id="test-worker", limit=20)
        return [
            outbox.process_delivery(
                self.factory, c, provider=provider, worker_id="test-worker", settings=self.settings
            )
            for c in claimed
        ]

    def code(self) -> str:
        return self.provider.code_in()


def _request(rig: Rig, email: str = STAFF_EMAIL, language: str = "en") -> Any:
    return rig.client.post(RESET_REQUEST, json={"email": email, "language": language})


def _confirm(rig: Rig, code: str, email: str = STAFF_EMAIL, new: str = NEW_PASSWORD) -> Any:
    return rig.client.post(
        RESET_CONFIRM,
        json={"email": email, "code": code, "new_password": new, "new_password_confirmation": new},
    )


def _challenges(
    rig: Rig, email: str = STAFF_EMAIL, purpose: str = "STAFF_PASSWORD_RESET"
) -> list[Any]:
    return rig.sql(
        "SELECT id, attempt_count, consumed_at, expires_at, code_hash FROM otp_challenges "
        "WHERE email = :e AND purpose = CAST(:p AS otp_purpose) ORDER BY id",
        e=email,
        p=purpose,
    )


def test_the_full_reset_flow(rig: Rig, mail: Mail) -> None:
    staff_id = _staff_id(rig, STAFF_EMAIL)
    _login(rig, STAFF_EMAIL, SEED_STAFF_PASSWORD)
    rig.session_for(staff_id)
    rig.sql(
        "UPDATE staff_accounts SET failed_login_count = 7, locked_until = now() + interval '5 minutes' "
        "WHERE id = :i",
        i=staff_id,
    )
    rig.sign_out()

    requested = _request(rig, language="en")

    assert requested.status_code == 200 and requested.json() == GENERIC
    assert len(_challenges(rig)) == 1
    assert (
        rig.scalar("SELECT count(*) FROM otp_deliveries WHERE purpose = 'STAFF_PASSWORD_RESET'")
        == 1
    )
    assert mail.send() == ["accepted"]
    message = mail.provider.messages[0]
    assert message.to == STAFF_EMAIL
    assert "password reset" in message.subject and mail.code() in message.subject
    assert "password has not changed" in message.text

    confirmed = _confirm(rig, mail.code())

    assert confirmed.status_code == 204, confirmed.text
    live, revoked = _sessions(rig, staff_id)
    assert live == 0 and revoked >= 2  # every session ended, and the reset made none
    assert "set-cookie" not in confirmed.headers  # no session was created
    assert _error(_login(rig, STAFF_EMAIL, SEED_STAFF_PASSWORD)) == (401, "INVALID_CREDENTIALS")
    assert _login(rig, STAFF_EMAIL, NEW_PASSWORD).status_code == 204
    # the lockout is cleared too: the owner proved the mailbox
    assert (
        rig.scalar("SELECT failed_login_count FROM staff_accounts WHERE id = :i", i=staff_id) == 0
    )
    assert rig.scalar("SELECT locked_until FROM staff_accounts WHERE id = :i", i=staff_id) is None
    # audit: who asked, who finished - the staff member themself, and nothing secret
    asked = rig.audit_rows("staff.password_reset_requested")
    done = rig.audit_rows("staff.password_reset")
    assert [a["after_value"] for a in asked] == [{"id": staff_id, "initiated_by": "self"}]
    assert done[0]["after_value"]["id"] == staff_id and done[0]["staff_account_id"] == staff_id


def test_a_code_is_single_use_and_the_outbox_row_is_wiped_once_sent(rig: Rig, mail: Mail) -> None:
    _request(rig)
    mail.send()
    code = mail.code()
    assert _confirm(rig, code).status_code == 204
    again = _confirm(rig, code, new=OTHER_NEW_PASSWORD)
    assert _error(again) == (400, "OTP_INVALID")
    assert (
        _login(rig, STAFF_EMAIL, NEW_PASSWORD).status_code == 204
    )  # the second attempt changed nothing
    row = rig.sql("SELECT status, payload_ciphertext FROM otp_deliveries")[0]
    assert row[0] == "accepted" and row[1] is None
    assert _challenges(rig)[0][2] is not None  # consumed


def test_every_failure_to_confirm_looks_the_same(rig: Rig, mail: Mail) -> None:
    staff_id = _staff_id(rig, STAFF_EMAIL)
    _request(rig)
    mail.send()
    good = mail.code()
    wrong = "000000" if good != "000000" else "111111"
    old_hash = rig.scalar("SELECT password_hash FROM staff_accounts WHERE id = :i", i=staff_id)

    attempts: dict[str, Any] = {}
    attempts["wrong code"] = _confirm(rig, wrong)
    attempts["unknown address"] = _confirm(rig, good, email="nobody-here@staff.facemark.demo")
    # expired
    rig.sql("UPDATE otp_challenges SET expires_at = now() - interval '1 second'")
    attempts["expired"] = _confirm(rig, good)
    rig.sql(
        "UPDATE otp_challenges SET expires_at = now() + interval '5 minutes', attempt_count = 5"
    )
    attempts["attempts used up"] = _confirm(rig, good)
    rig.sql("UPDATE otp_challenges SET attempt_count = 0")
    # a deactivated account, with the right code
    rig.sql("UPDATE staff_accounts SET is_active = false WHERE id = :i", i=staff_id)
    attempts["deactivated"] = _confirm(rig, good)
    rig.sql("UPDATE staff_accounts SET is_active = true WHERE id = :i", i=staff_id)
    # no challenge at all
    rig.sql("DELETE FROM otp_challenges")
    attempts["no challenge"] = _confirm(rig, good)

    bodies = {
        name: (r.status_code, r.json()["error"]["code"], r.json()["error"]["message"])
        for name, r in attempts.items()
    }
    assert set(bodies.values()) == {(400, "OTP_INVALID", bodies["wrong code"][2])}, bodies
    assert (
        rig.scalar("SELECT password_hash FROM staff_accounts WHERE id = :i", i=staff_id) == old_hash
    )
    assert _login(rig, STAFF_EMAIL, SEED_STAFF_PASSWORD).status_code == 204  # still the old one


def test_five_wrong_codes_use_up_the_challenge(rig: Rig, mail: Mail) -> None:
    _request(rig)
    mail.send()
    good = mail.code()
    wrong = "000000" if good != "000000" else "111111"
    for _ in range(5):
        assert _error(_confirm(rig, wrong)) == (400, "OTP_INVALID")
    assert _challenges(rig)[0][1] == 5  # each wrong guess committed its count
    assert _error(_confirm(rig, good)) == (400, "OTP_INVALID")  # even the right code is now refused
    assert _login(rig, STAFF_EMAIL, SEED_STAFF_PASSWORD).status_code == 204


def test_the_code_expires_after_ten_minutes(rig: Rig, mail: Mail) -> None:
    _request(rig)
    expires, created = rig.sql("SELECT expires_at, created_at FROM otp_challenges")[0]
    assert timedelta(minutes=9, seconds=55) < expires - created <= timedelta(minutes=10, seconds=5)
    assert EXPIRY_MINUTES == 10
    # a delivery whose code expired before it was sent is never sent
    rig.sql("UPDATE otp_challenges SET expires_at = now() - interval '1 second'")
    rig.sql("UPDATE otp_deliveries SET expires_at = now() - interval '1 second'")
    assert mail.send() == []
    assert mail.provider.messages == []


def test_a_bad_new_password_is_refused_before_the_code_is_touched(rig: Rig, mail: Mail) -> None:
    _request(rig)
    mail.send()
    code = mail.code()
    assert _confirm(rig, code, new="short").status_code == 422
    mismatch = rig.client.post(
        RESET_CONFIRM,
        json={
            "email": STAFF_EMAIL,
            "code": code,
            "new_password": NEW_PASSWORD,
            "new_password_confirmation": OTHER_NEW_PASSWORD,
        },
    )
    assert mismatch.status_code == 422 and NEW_PASSWORD not in mismatch.text
    assert _challenges(rig)[0][1:3] == (0, None)  # not counted, not consumed
    assert _confirm(rig, code).status_code == 204  # and the code still works


def test_unknown_and_deactivated_accounts_are_indistinguishable_from_real_ones(
    rig: Rig, mail: Mail, monkeypatch: pytest.MonkeyPatch
) -> None:
    deactivated = _staff_id(rig, "quantumleap-robotics@staff.facemark.demo")
    rig.sql("UPDATE staff_accounts SET is_active = false WHERE id = :i", i=deactivated)
    hashed: list[str] = []
    real_hash = hash_otp_code

    def counting_hash(code: str) -> str:
        hashed.append("hash")
        return real_hash(code)

    monkeypatch.setattr("app.modules.staff.password.hash_otp_code", counting_hash)
    monkeypatch.setattr("app.modules.attendees.service.hash_otp_code", counting_hash)

    known = _request(rig, STAFF_EMAIL)
    known_work = len(hashed)
    hashed.clear()
    unknown = _request(rig, "who-is-this@staff.facemark.demo")
    unknown_work = len(hashed)
    hashed.clear()
    inactive = _request(rig, "quantumleap-robotics@staff.facemark.demo")
    inactive_work = len(hashed)

    for response in (known, unknown, inactive):
        assert response.status_code == 200 and response.json() == GENERIC
    assert known_work == unknown_work == inactive_work == 1  # the same Argon2id work in each case
    # only the real, active account got a challenge and an email
    assert len(_challenges(rig, STAFF_EMAIL)) == 1
    assert _challenges(rig, "who-is-this@staff.facemark.demo") == []
    assert _challenges(rig, "quantumleap-robotics@staff.facemark.demo") == []
    assert rig.scalar("SELECT count(*) FROM otp_deliveries") == 1
    assert len(rig.audit_rows("staff.password_reset_requested")) == 1


def test_confirming_spends_the_same_verification_work_whatever_is_wrong(
    rig: Rig, mail: Mail, monkeypatch: pytest.MonkeyPatch
) -> None:
    _request(rig)
    mail.send()
    wrong = "000000" if mail.code() != "000000" else "111111"
    calls: list[str] = []
    real = verify_secret

    def counting(hash_: str, candidate: str) -> bool:
        calls.append("verify")
        return real(hash_, candidate)

    monkeypatch.setattr("app.modules.staff.password.verify_secret", counting)

    _confirm(rig, wrong)  # a real challenge, wrong code
    with_challenge = len(calls)
    calls.clear()
    _confirm(rig, wrong, email="nobody@staff.facemark.demo")  # nothing real at all
    without = len(calls)
    calls.clear()
    rig.sql("UPDATE otp_challenges SET expires_at = now() - interval '1 second'")
    _confirm(rig, wrong)  # expired
    expired = len(calls)

    assert with_challenge == without == expired == 1


def test_a_code_of_another_purpose_never_resets_a_staff_password_and_vice_versa(
    rig: Rig, mail: Mail
) -> None:
    from app.modules.attendees.service import verify_otp

    # an attendee who happens to use the staff member's address, with live LOGIN
    # and ACTIVATION-style codes of their own
    user_id = rig.make_user("shared")
    rig.sql("UPDATE users SET email = :e WHERE id = :i", e=STAFF_EMAIL, i=user_id)
    login_code = "424242"
    rig.sql(
        "INSERT INTO otp_challenges (email, user_id, purpose, code_hash, expires_at) "
        "VALUES (:e, :u, 'LOGIN', :h, now() + interval '10 minutes')",
        e=STAFF_EMAIL,
        u=user_id,
        h=hash_otp_code(login_code),
    )
    # 1. the attendee's code does not reset the staff password
    assert _error(_confirm(rig, login_code)) == (400, "OTP_INVALID")

    # 2. asking for a staff reset does not touch the attendee's live LOGIN code
    _request(rig)
    mail.send()
    staff_code = mail.code()
    assert len(_challenges(rig, STAFF_EMAIL, "LOGIN")) == 1
    assert _challenges(rig, STAFF_EMAIL, "LOGIN")[0][2] is None  # still live
    assert len(_challenges(rig, STAFF_EMAIL, "STAFF_PASSWORD_RESET")) == 1

    # 3. the staff code is not an attendee code: the attendee verification refuses it
    with Session(rig.engine) as db, mutating_transaction(db) as tx:
        outcome = verify_otp(tx, email=STAFF_EMAIL, code=staff_code)
    assert outcome.ok is False
    # ... and the attendee's own code still works for the attendee
    with Session(rig.engine) as db, mutating_transaction(db) as tx:
        assert verify_otp(tx, email=STAFF_EMAIL, code=login_code).ok is True

    # 4. only the staff flow accepts the staff code
    assert _confirm(rig, staff_code).status_code == 204


def test_a_deactivated_account_is_not_reactivated_by_a_reset(rig: Rig, mail: Mail) -> None:
    staff_id = _staff_id(rig, STAFF_EMAIL)
    _request(rig)
    mail.send()
    code = mail.code()
    rig.sql("UPDATE staff_accounts SET is_active = false WHERE id = :i", i=staff_id)
    hash_before = rig.scalar("SELECT password_hash FROM staff_accounts WHERE id = :i", i=staff_id)

    assert _error(_confirm(rig, code)) == (400, "OTP_INVALID")

    assert rig.scalar("SELECT is_active FROM staff_accounts WHERE id = :i", i=staff_id) is False
    assert (
        rig.scalar("SELECT password_hash FROM staff_accounts WHERE id = :i", i=staff_id)
        == hash_before
    )
    assert _error(_login(rig, STAFF_EMAIL, NEW_PASSWORD)) == (401, "INVALID_CREDENTIALS")
    assert _error(_login(rig, STAFF_EMAIL, SEED_STAFF_PASSWORD)) == (401, "INVALID_CREDENTIALS")


def test_the_request_and_confirmation_are_rate_limited_per_address_known_or_not(
    rig: Rig, mail: Mail
) -> None:
    for address in (STAFF_EMAIL, "ghost@staff.facemark.demo"):
        statuses = [_request(rig, address).status_code for _ in range(5)]
        assert statuses == [200, 200, 200, 429, 429], (
            address
        )  # the same for a real and an unknown address
    assert STAFF_RESET_REQUEST_PER_EMAIL.limit == 3
    results = [
        _confirm(rig, "123456", email="x@staff.facemark.demo").status_code for _ in range(12)
    ]
    assert results[:10] == [400] * 10 and results[10:] == [429, 429]


def test_a_second_request_replaces_the_first_code(rig: Rig, mail: Mail) -> None:
    _request(rig)
    first_provider = FakeProvider()
    mail.send(first_provider)
    first = first_provider.code_in()
    _request(rig)
    second_provider = FakeProvider()
    mail.send(second_provider)
    second = second_provider.code_in()
    assert len(_challenges(rig)) == 2 and sum(1 for c in _challenges(rig) if c[2] is None) == 1
    if first != second:
        assert _error(_confirm(rig, first)) == (400, "OTP_INVALID")
    assert _confirm(rig, second).status_code == 204


def test_without_an_email_outbox_the_public_answer_is_the_same_and_nothing_is_queued(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.core.config import get_settings

    plain = get_settings().model_copy(
        update={"resend_api_key": None, "otp_outbox_encryption_key": None}
    )
    monkeypatch.setattr("app.modules.staff.password.get_settings", lambda: plain)
    caplog.set_level(logging.DEBUG)
    response = _request(rig)
    assert response.status_code == 200 and response.json() == GENERIC
    assert rig.scalar("SELECT count(*) FROM otp_challenges") == 0
    assert STAFF_EMAIL not in caplog.text  # the warning names no address
    rig.sign_in(StaffRole.ADMIN)
    started = rig.client.post(
        f"/api/admin/staff/{_staff_id(rig, STAFF_EMAIL)}/password-reset", json={"language": "en"}
    )
    assert _error(started) == (503, "PASSWORD_RESET_UNAVAILABLE")


def test_the_staff_reset_code_never_reaches_the_demo_log_or_the_demo_codes_panel(
    rig: Rig, mail: Mail, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    demo = outbox_settings(demo_mode=True)
    install_settings(monkeypatch, demo)
    monkeypatch.setattr("app.modules.staff.password.get_settings", lambda: demo)
    caplog.set_level(logging.DEBUG)
    _request(rig)
    mail.settings = demo
    mail.send()
    code = mail.code()
    assert code not in caplog.text
    assert [
        c for c in otp_delivery.recent_demo_otps() if c["purpose"] == "STAFF_PASSWORD_RESET"
    ] == []
    assert all(c["code"] != code for c in otp_delivery.recent_demo_otps())
    assert OtpPurpose.STAFF_PASSWORD_RESET.value == "STAFF_PASSWORD_RESET"


def test_the_outbox_retries_a_failed_send_with_the_same_code_and_keeps_it_secret(
    rig: Rig, mail: Mail, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _request(rig)
    provider = FakeProvider(retryable(), accepted())
    delivery = rig.sql("SELECT id, idempotency_key, payload_ciphertext FROM otp_deliveries")[0]
    ciphertext = bytes(delivery[2])
    code_hash = _challenges(rig)[0][4]
    assert code_hash.startswith("$argon2id$")  # verification uses a hash, never the code

    assert mail.send(provider) == ["retry"]  # a transient failure: kept, retried later
    rig.sql("UPDATE otp_deliveries SET next_attempt_at = now() - interval '1 second'")
    assert mail.send(provider) == ["accepted"]

    code = provider.code_in(0)
    assert code == provider.code_in(1)  # the same code on the retry
    assert [m.idempotency_key for m in provider.messages] == [
        delivery[1]
    ] * 2  # one provider key, so no duplicate email
    assert code.encode() not in ciphertext  # held only as ciphertext while waiting
    final = rig.sql("SELECT status, payload_ciphertext, attempt_count FROM otp_deliveries")[0]
    assert (final[0], final[1], final[2]) == ("accepted", None, 2)
    assert code not in caplog.text and STAFF_EMAIL not in caplog.text
    everything = "".join(str(r) for r in rig.sql("SELECT * FROM otp_deliveries")) + "".join(
        str(r) for r in rig.sql("SELECT * FROM admin_audit")
    )
    assert code not in everything
    assert _confirm(rig, code).status_code == 204


def test_two_confirmations_at_once_use_the_code_once(rig: Rig, mail: Mail) -> None:
    _request(rig)
    mail.send()
    code = mail.code()
    barrier = threading.Barrier(6)
    outcomes: list[bool] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        password = f"Concurrent-Passw0rd-{n}-zz"
        with Session(rig.engine) as db:
            barrier.wait()
            with mutating_transaction(db) as tx:
                result = staff_password.confirm_reset(
                    tx, email=STAFF_EMAIL, code=code, new_password=password
                )
        with lock:
            outcomes.append(result.ok)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(outcomes) == [False] * 5 + [True]
    assert len(rig.audit_rows("staff.password_reset")) == 1


# --- an administrator starts the email -------------------------------------------------


def _start(rig: Rig, staff_id: int, language: str = "en") -> Any:
    return rig.client.post(
        f"/api/admin/staff/{staff_id}/password-reset", json={"language": language}
    )


def test_an_administrator_can_start_a_reset_email_but_never_sees_the_code(
    rig: Rig, mail: Mail, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    target = _staff_id(rig, STAFF_EMAIL)
    admin_id = rig.sign_in(StaffRole.ADMIN)

    response = _start(rig, target, "ru")

    assert response.status_code == 202 and response.json() == {"status": "queued"}
    assert mail.send() == ["accepted"]
    message = mail.provider.messages[0]
    assert message.to == STAFF_EMAIL  # the staff member's own address, never the administrator's
    code = mail.code()
    for visible in (
        response.text,
        caplog.text,
        "".join(str(r) for r in rig.sql("SELECT * FROM admin_audit")),
    ):
        assert code not in visible
    assert "сброса пароля" in message.subject  # in the language asked for
    asked = rig.audit_rows("staff.password_reset_requested")
    assert len(asked) == 1
    assert asked[0]["staff_account_id"] == admin_id  # the administrator is the actor
    assert asked[0]["target_id"] == str(target)
    assert asked[0]["after_value"] == {"id": target, "initiated_by": "admin"}

    # only the staff member can finish it, choosing their own password; nothing signed in changes first
    rig.sign_out()
    assert _confirm(rig, code, new=OTHER_NEW_PASSWORD).status_code == 204
    assert _login(rig, STAFF_EMAIL, OTHER_NEW_PASSWORD).status_code == 204

    rig.sign_in(StaffRole.ADMIN)
    event = rig.client.get(
        "/api/admin/audit", params={"action": "staff.password_reset_requested"}
    ).json()["items"][0]
    assert event["details"] == "known" and event["after"] == {"id": target, "initiated_by": "admin"}
    assert event["actor_id"] == admin_id and event["hidden_field_count"] == 0


def test_the_reset_email_takes_no_password_and_shows_no_code(rig: Rig, mail: Mail) -> None:
    target = _staff_id(rig, STAFF_EMAIL)
    rig.sign_in(StaffRole.ADMIN)
    for forbidden in (
        {"password": NEW_PASSWORD},
        {"new_password": NEW_PASSWORD},
        {"new_password_confirmation": NEW_PASSWORD},
        {"code": "123456"},
        {"email": "elsewhere@mail.test"},
    ):
        assert _error(
            rig.client.post(f"/api/admin/staff/{target}/password-reset", json=forbidden)
        ) == (
            422,
            "VALIDATION_ERROR",
        )
    assert rig.scalar("SELECT count(*) FROM otp_challenges") == 0
    # and the console's other reads never expose a code or a hash
    for path in ("/api/admin/staff", "/api/admin/otp-deliveries", "/api/admin/audit"):
        text = rig.client.get(path).text
        assert "password_hash" not in text and "$argon2" not in text


def test_only_an_administrator_may_start_a_reset_and_only_for_an_active_account(
    rig: Rig, mail: Mail
) -> None:
    target = _staff_id(rig, STAFF_EMAIL)
    rig.sign_out()
    assert _start(rig, target).status_code == 401
    rig.sign_in(StaffRole.BOOTH_STAFF)
    assert _error(_start(rig, target)) == (403, "STAFF_NOT_AUTHORIZED")
    rig.sign_in(StaffRole.ADMIN)
    assert _error(_start(rig, 9_999_999)) == (404, "ADMIN_NOT_FOUND")
    for bad in ("0", "-1", "abc"):
        assert rig.client.post(f"/api/admin/staff/{bad}/password-reset", json={}).status_code == 422
    rig.sql("UPDATE staff_accounts SET is_active = false WHERE id = :i", i=target)
    assert _error(_start(rig, target)) == (400, "ADMIN_INVALID")
    assert rig.scalar("SELECT count(*) FROM otp_challenges") == 0
    assert rig.audit_rows("staff.password_reset_requested") == []


def test_an_administrator_cannot_flood_one_inbox_or_start_unlimited_resets(
    rig: Rig, mail: Mail, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _staff_id(rig, STAFF_EMAIL)
    rig.sign_in(StaffRole.ADMIN)
    assert [_start(rig, target).status_code for _ in range(5)] == [
        202,
        202,
        202,
        429,
        429,
    ]  # per address
    assert ADMIN_RESET_START_PER_ADMIN.limit == 20
    monkeypatch.setattr(
        "app.modules.staff.password.ADMIN_RESET_START_PER_ADMIN",
        RateLimit("admin_reset_start:test", 2, 60),
    )
    other = _staff_id(rig, "securestack@staff.facemark.demo")
    third = _staff_id(rig, "finpulse@staff.facemark.demo")
    codes = [_start(rig, other).status_code, _start(rig, third).status_code]
    assert 202 in codes
    assert _start(rig, other).status_code == 429 or _start(rig, third).status_code == 429


# --- the audit allowlist and the email copy ----------------------------------------------


def test_every_password_audit_action_is_on_the_allowlist() -> None:
    for action in (
        staff_password.AUDIT_PASSWORD_CHANGE,
        staff_password.AUDIT_RESET_REQUESTED,
        staff_password.AUDIT_RESET_DONE,
    ):
        assert action in audit_view.FIELD_ALLOWLISTS


@pytest.mark.parametrize("language", ["en", "az", "ru"])
def test_the_reset_email_is_its_own_text_in_every_language(language: str) -> None:
    reset = render_otp_email(language, OtpPurpose.STAFF_PASSWORD_RESET, "654321")
    login = render_otp_email(language, OtpPurpose.LOGIN, "654321")
    activation = render_otp_email(language, OtpPurpose.ACTIVATION, "654321")
    assert reset.subject.startswith("654321")
    assert len({reset.subject, login.subject, activation.subject}) == 3
    assert reset.text != login.text and "654321" in reset.text and "654321" in reset.html
    wording = {"en": "password", "az": "şifrə", "ru": "пароль"}[language]
    assert wording in reset.subject.lower() or wording in reset.text.lower()
    assert "10" in reset.text  # the validity, spelled for each language (pinned to EXPIRY_MINUTES)


def test_the_admin_email_delivery_list_shows_a_reset_email_without_its_address_or_code(
    rig: Rig, mail: Mail
) -> None:
    _request(rig)
    mail.send()
    code = mail.code()
    rig.sign_in(StaffRole.ADMIN)

    response = rig.client.get("/api/admin/otp-deliveries")

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["purpose"] == "STAFF_PASSWORD_RESET" and item["status"] == "accepted"
    assert item["email_masked"] == "a***@staff.facemark.demo"
    assert STAFF_EMAIL not in response.text and code not in response.text


# --- an administrator sets the password, no email (8 Nov 2026) ------------------


def test_an_administrator_sets_a_staff_password_without_email(rig: Rig) -> None:
    target = _staff_id(rig, STAFF_EMAIL)
    assert _login(rig, STAFF_EMAIL, SEED_STAFF_PASSWORD).status_code == 204
    assert _sessions(rig, target)[0] >= 1
    rig.sign_in(StaffRole.ADMIN)
    body = {"new_password": NEW_PASSWORD, "new_password_confirmation": NEW_PASSWORD}
    url = f"/api/admin/staff/{target}/password"
    assert rig.client.post(url, json=body).status_code == 204
    assert _sessions(rig, target)[0] == 0  # every session of the account ended
    assert rig.scalar("SELECT count(*) FROM otp_challenges") == 0  # no email, no code
    row = rig.sql(
        "SELECT after_value::text FROM admin_audit WHERE action = 'staff.password_admin_set'"
    )[0]
    assert NEW_PASSWORD not in row[0] and "argon2" not in row[0]
    assert _login(rig, STAFF_EMAIL, SEED_STAFF_PASSWORD).status_code == 401
    assert _login(rig, STAFF_EMAIL, NEW_PASSWORD).status_code == 204


def test_setting_a_password_is_for_administrators_on_active_non_admin_accounts(
    rig: Rig,
) -> None:
    target = _staff_id(rig, STAFF_EMAIL)
    admin = _staff_id(rig, ADMIN_EMAIL)
    body = {"new_password": NEW_PASSWORD, "new_password_confirmation": NEW_PASSWORD}
    rig.sign_in(StaffRole.BOOTH_STAFF)
    assert rig.client.post(f"/api/admin/staff/{target}/password", json=body).status_code == 403
    rig.sign_in(StaffRole.ADMIN)
    assert _error(rig.client.post(f"/api/admin/staff/{admin}/password", json=body))[0] == 400
    short = {"new_password": "short", "new_password_confirmation": "short"}
    assert _error(rig.client.post(f"/api/admin/staff/{target}/password", json=short)) == (
        422,
        "VALIDATION_ERROR",
    )
    mismatch = {"new_password": NEW_PASSWORD, "new_password_confirmation": OTHER_NEW_PASSWORD}
    assert rig.client.post(f"/api/admin/staff/{target}/password", json=mismatch).status_code == 422
    assert rig.client.post("/api/admin/staff/999999/password", json=body).status_code == 404
    rig.sql("UPDATE staff_accounts SET is_active = false WHERE id = :s", s=target)
    assert _error(rig.client.post(f"/api/admin/staff/{target}/password", json=body))[0] == 400
