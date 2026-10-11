"""Real OTP email delivery via Resend (app/modules/auth/otp_delivery.py).

`httpx.post` is replaced by a recorder — no test here ever touches the
network or needs a real API key. What these pin down is the contract:
what is sent to whom, that delivery never blocks or fails the request,
how transient failures are retried, that nothing sensitive is logged,
and that real delivery and the DEMO_MODE log are independent paths.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.modules.attendees.models import OtpPurpose
from app.modules.attendees.service import PendingOtpDelivery
from app.modules.auth import otp_delivery
from tests.prod_settings import production_settings

_BASE: dict[str, Any] = {
    "environment": "development",
    "database_url": "postgresql+psycopg://u:p@h/d",
    "app_origin": "http://localhost:3000",
    "scan_ref_secret": "test-secret",
    # Explicit, so the container's own DEMO_MODE / RESEND_API_KEY
    # environment never leaks into what a test thinks it configured.
    "demo_mode": False,
    "resend_api_key": None,
    "otp_from_email": "AI Summit <info@aiworks.az>",
}


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **{**_BASE, **overrides})


def _delivery(purpose: OtpPurpose = OtpPurpose.ACTIVATION) -> PendingOtpDelivery:
    return PendingOtpDelivery(email="attendee@test.facemark.demo", purpose=purpose, code="482913")


class _Recorder:
    """Replays `statuses` in order (the last one repeats)."""

    def __init__(self, *statuses: int | Exception) -> None:
        self.calls: list[dict[str, Any]] = []
        self._statuses = list(statuses) or [200]

    def __call__(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        outcome = self._statuses[min(len(self.calls), len(self._statuses)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, request=httpx.Request("POST", url), json={})


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(otp_delivery, "_BACKOFF_SECONDS", (0.0, 0.0))


def _use(monkeypatch: pytest.MonkeyPatch, settings: Settings, recorder: _Recorder) -> None:
    monkeypatch.setattr(otp_delivery, "get_settings", lambda: settings)
    monkeypatch.setattr(httpx, "post", recorder)


def _deliver(delivery: PendingOtpDelivery | None = None) -> None:
    otp_delivery.deliver_otp(delivery or _delivery())
    otp_delivery.wait_for_pending()


# --- what is sent ----------------------------------------------------------


def test_sends_the_code_to_the_attendee_from_the_configured_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    _use(monkeypatch, _settings(resend_api_key="re_test_key"), recorder)

    _deliver()

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["url"] == "https://api.resend.com/emails"
    assert call["headers"]["Authorization"] == "Bearer re_test_key"
    body = call["json"]
    assert body["from"] == "AI Summit <info@aiworks.az>"
    assert body["to"] == ["attendee@test.facemark.demo"]
    assert "482913" in body["html"]
    # Deliverability: a plain-text alternative, the code up front in the
    # subject, and a real Reply-To.
    assert "482913" in body["text"]
    assert body["subject"].startswith("482913 is ")
    assert body["reply_to"] == "AI Summit <info@aiworks.az>"


def test_the_from_address_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    _use(
        monkeypatch,
        _settings(resend_api_key="k", otp_from_email="Other <noreply@example.org>"),
        recorder,
    )
    _deliver()
    assert recorder.calls[0]["json"]["from"] == "Other <noreply@example.org>"


def test_activation_and_login_codes_get_different_subjects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    _use(monkeypatch, _settings(resend_api_key="k"), recorder)
    _deliver(_delivery(OtpPurpose.ACTIVATION))
    _deliver(_delivery(OtpPurpose.LOGIN))
    subjects = [c["json"]["subject"] for c in recorder.calls]
    assert subjects[0] != subjects[1]


def test_the_code_is_html_escaped_in_the_html_part(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    _use(monkeypatch, _settings(resend_api_key="k"), recorder)
    _deliver(PendingOtpDelivery(email="a@b.demo", purpose=OtpPurpose.LOGIN, code="<b>1</b>"))
    assert "<b>1</b>" not in recorder.calls[0]["json"]["html"]
    assert "&lt;b&gt;1&lt;/b&gt;" in recorder.calls[0]["json"]["html"]


# --- never blocks, never fails the request ---------------------------------


def test_deliver_otp_returns_before_the_provider_call_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow provider must not slow the request — it would also make
    the branches that send an email measurably slower than the ones that
    do not (a register/login enumeration timing oracle)."""
    release = threading.Event()
    started = threading.Event()

    def slow_post(url: str, **kwargs: Any) -> httpx.Response:
        started.set()
        release.wait(timeout=5)
        return httpx.Response(200, request=httpx.Request("POST", url), json={})

    monkeypatch.setattr(otp_delivery, "get_settings", lambda: _settings(resend_api_key="k"))
    monkeypatch.setattr(httpx, "post", slow_post)

    otp_delivery.deliver_otp(_delivery())  # must return immediately
    assert started.wait(timeout=2), "the send never started on the worker"
    assert not release.is_set()
    release.set()
    otp_delivery.wait_for_pending()


def test_a_provider_failure_never_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _use(monkeypatch, _settings(resend_api_key="k"), _Recorder(403))
    with caplog.at_level(logging.ERROR, logger=otp_delivery.logger.name):
        _deliver()  # must not raise
    assert any("OTP email delivery failed" in r.getMessage() for r in caplog.records)


def test_the_failure_log_contains_neither_the_address_nor_the_code(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ARCHITECTURE.md §21.11: no OTP code, and no full email address,
    in logs outside the explicit DEMO_MODE exception."""
    _use(monkeypatch, _settings(resend_api_key="secret_key_value"), _Recorder(500))
    with caplog.at_level(logging.DEBUG):
        _deliver()
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "OTP email delivery failed" in logged
    assert "482913" not in logged
    assert "attendee@test.facemark.demo" not in logged
    assert "secret_key_value" not in logged


# --- retries ----------------------------------------------------------------


def test_a_transient_failure_is_retried_and_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder(503, httpx.ConnectTimeout("t"), 200)
    _use(monkeypatch, _settings(resend_api_key="k"), recorder)
    _deliver()
    assert len(recorder.calls) == 3


def test_rate_limiting_by_the_provider_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(429, 200)
    _use(monkeypatch, _settings(resend_api_key="k"), recorder)
    _deliver()
    assert len(recorder.calls) == 2


def test_a_client_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx (bad key, unverified domain, invalid address) will not fix
    itself; retrying only delays the log line and burns provider quota."""
    recorder = _Recorder(403)
    _use(monkeypatch, _settings(resend_api_key="k"), recorder)
    _deliver()
    assert len(recorder.calls) == 1


def test_retries_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(500)
    _use(monkeypatch, _settings(resend_api_key="k"), recorder)
    _deliver()
    assert len(recorder.calls) == otp_delivery._MAX_ATTEMPTS


def test_every_attempt_of_one_delivery_shares_one_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry after an ambiguous timeout must never email the code twice."""
    recorder = _Recorder(500, 500, 200)
    _use(monkeypatch, _settings(resend_api_key="k"), recorder)
    _deliver()
    keys = {c["headers"]["Idempotency-Key"] for c in recorder.calls}
    assert len(recorder.calls) == 3 and len(keys) == 1


def test_two_different_deliveries_get_different_idempotency_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    _use(monkeypatch, _settings(resend_api_key="k"), recorder)
    _deliver()
    _deliver()
    keys = [c["headers"]["Idempotency-Key"] for c in recorder.calls]
    assert keys[0] != keys[1]


# --- the two paths are independent ------------------------------------------


def test_demo_mode_without_a_key_sends_no_email(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    _use(monkeypatch, _settings(demo_mode=True), recorder)
    monkeypatch.setattr("app.modules.realtime.service.current_dataset_generation", lambda: 1)
    _deliver()
    assert recorder.calls == []


def test_demo_mode_with_a_key_does_both(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    _use(monkeypatch, _settings(demo_mode=True, resend_api_key="k"), recorder)
    # The admin "recent OTPs" panel keeps working alongside real email.
    monkeypatch.setattr("app.modules.realtime.service.current_dataset_generation", lambda: 1)
    _deliver()
    assert len(recorder.calls) == 1
    codes = [c["code"] for c in otp_delivery.recent_demo_otps()]
    assert "482913" in codes


def test_neither_path_configured_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A misconfiguration, not a runtime hiccup — must be loud."""
    recorder = _Recorder()
    _use(monkeypatch, _settings(), recorder)
    with pytest.raises(RuntimeError, match="no OTP delivery"):
        otp_delivery.deliver_otp(_delivery())
    assert recorder.calls == []


# --- configuration ----------------------------------------------------------


def test_production_requires_a_resend_key() -> None:
    with pytest.raises(ValidationError, match="RESEND_API_KEY"):
        production_settings(resend_api_key=None)


def test_production_with_a_key_is_valid() -> None:
    assert production_settings().is_production


@pytest.mark.parametrize("bad", ["", "not-an-address", "AI Summit <info@localhost>", "<@x.org>"])
def test_a_malformed_from_address_fails_at_startup(bad: str) -> None:
    with pytest.raises(ValidationError, match="OTP_FROM_EMAIL"):
        _settings(otp_from_email=bad)


@pytest.mark.parametrize("good", ["AI Summit <info@aiworks.az>", "info@aiworks.az"])
def test_valid_from_addresses_are_accepted(good: str) -> None:
    assert _settings(otp_from_email=good).otp_from_email == good
