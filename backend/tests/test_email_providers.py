"""Amazon SES, ZeptoMail and the failover chain (app/modules/auth/email_providers.py).

Nothing is sent: `httpx.post` is replaced in every test that reaches a provider.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.modules.auth import email_providers as ep
from app.modules.auth.otp_provider import EmailMessage, ResendProvider, SendResult

MESSAGE = EmailMessage(
    "AI Summit <info@aiworks.az>", "someone@x.test", "Your code", "123456", "<p>123456</p>", "k-1"
)


class _Resp:
    def __init__(
        self, status: int, body: Any = None, headers: dict[str, str] | None = None, text: str = ""
    ) -> None:
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = httpx.Headers(headers or {})
        self.text = text

    def json(self) -> Any:
        return self._body


def _capture(monkeypatch: pytest.MonkeyPatch, response: _Resp | Exception) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _Resp:
        seen.update(url=url, **kwargs)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(httpx, "post", fake_post)
    return seen


# --- SES -----------------------------------------------------------------------


def test_the_sigv4_signing_key_matches_awss_published_example() -> None:
    # AWS docs, "Deriving a signing key": 20120215 / us-east-1 / iam.
    key = ep.signing_key("wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "20120215", "us-east-1", "iam")
    assert key.hex() == "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d"


def test_a_ses_request_is_signed_and_carries_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, _Resp(200, {"MessageId": "0102-abc"}))
    at = datetime(2026, 11, 13, 8, 30, tzinfo=UTC)
    result = ep.SesProvider("AKIDEXAMPLE", "secret", "eu-central-1", clock=lambda: at).send(MESSAGE)
    assert (result.accepted, result.provider_message_id) == (True, "0102-abc")
    assert seen["url"] == "https://email.eu-central-1.amazonaws.com/v2/email/outbound-emails"
    auth = seen["headers"]["authorization"]
    assert auth.startswith(
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261113/eu-central-1/ses/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date, Signature="
    )
    assert seen["headers"]["x-amz-date"] == "20261113T083000Z"
    assert b'"ToAddresses": ["someone@x.test"]' in seen["content"]
    # The same request at the same moment signs the same way (nothing random).
    again = ep.sigv4_headers(
        access_key="AKIDEXAMPLE",
        secret="secret",
        region="eu-central-1",
        host="email.eu-central-1.amazonaws.com",
        path="/v2/email/outbound-emails",
        body=seen["content"],
        now=at,
    )
    assert again["authorization"] == auth


@pytest.mark.parametrize(
    ("response", "reason", "retryable"),
    [
        (_Resp(429, headers={"retry-after": "5"}), "provider_rate_limited", True),
        (
            _Resp(400, headers={"x-amzn-ErrorType": "LimitExceededException"}),
            "provider_rate_limited",
            True,
        ),
        (
            _Resp(400, headers={"x-amzn-ErrorType": "SendingPausedException"}),
            "provider_rate_limited",
            True,
        ),
        (
            _Resp(400, headers={"x-amzn-ErrorType": "MessageRejected"}),
            "provider_rejected_message",
            False,
        ),
        (_Resp(403), "provider_auth_rejected", False),
        (_Resp(503), "provider_unavailable", True),
    ],
)
def test_ses_answers_are_classified(
    monkeypatch: pytest.MonkeyPatch, response: _Resp, reason: str, retryable: bool
) -> None:
    _capture(monkeypatch, response)
    r = ep.SesProvider("A", "s", "eu-central-1").send(MESSAGE)
    assert (r.accepted, r.reason_code, r.retryable) == (False, reason, retryable)


def test_ses_network_failure_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch, httpx.ConnectTimeout("someone@x.test 123456"))
    r = ep.SesProvider("A", "s", "eu-central-1").send(MESSAGE)
    assert (r.reason_code, r.retryable) == ("network_error", True)


# --- ZeptoMail -------------------------------------------------------------------


def test_a_zeptomail_request_names_sender_and_recipient(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, _Resp(201, {"request_id": "2d6f.1a"}))
    r = ep.ZeptoMailProvider("tok123").send(MESSAGE)
    assert (r.accepted, r.provider_message_id) == (True, "2d6f.1a")
    assert seen["headers"]["Authorization"] == "Zoho-enczapikey tok123"
    assert seen["json"]["from"] == {"address": "info@aiworks.az", "name": "AI Summit"}
    assert seen["json"]["to"] == [{"email_address": {"address": "someone@x.test"}}]
    assert ep.ZeptoMailProvider("Zoho-enczapikey tok").send(MESSAGE).accepted
    assert seen["headers"]["Authorization"] == "Zoho-enczapikey tok"


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (_Resp(429), "provider_rate_limited"),
        (_Resp(400, text='{"message":"No credits available"}'), "provider_rate_limited"),
        (_Resp(401), "provider_auth_rejected"),
        (_Resp(422, text="invalid address"), "provider_rejected_message"),
        (_Resp(500), "provider_unavailable"),
    ],
)
def test_zeptomail_answers_are_classified(
    monkeypatch: pytest.MonkeyPatch, response: _Resp, reason: str
) -> None:
    _capture(monkeypatch, response)
    assert ep.ZeptoMailProvider("t").send(MESSAGE).reason_code == reason


# --- the chain -----------------------------------------------------------------------


class _Fake:
    def __init__(self, *answers: SendResult) -> None:
        self.answers = list(answers)
        self.calls = 0

    def send(self, message: EmailMessage) -> SendResult:
        self.calls += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


OK = SendResult(True, provider_message_id="m-1")
LIMIT = SendResult(False, reason_code="provider_rate_limited", retryable=True)
DOWN = SendResult(False, reason_code="provider_unavailable", retryable=True)
REFUSED = SendResult(False, reason_code="provider_rejected_message")
CONFLICT = SendResult(False, reason_code="provider_conflict", retryable=True)


class _Clock:
    t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_the_next_provider_sends_when_the_first_is_out_of_quota() -> None:
    clock = _Clock()
    ses, zepto, resend = _Fake(LIMIT), _Fake(OK), _Fake(OK)
    chain = ep.FailoverProvider(
        [("ses", ses), ("zeptomail", zepto), ("resend", resend)], clock=clock
    )
    r = chain.send(MESSAGE)
    assert (r.accepted, r.provider_message_id) == (True, "zeptomail:m-1")
    assert (ses.calls, zepto.calls, resend.calls) == (1, 1, 0)
    # SES rests (60 s): the next message goes straight to ZeptoMail ...
    chain.send(MESSAGE)
    assert (ses.calls, zepto.calls) == (1, 2)
    assert chain.state()["ses"] > 0
    # ... and once the rest is over SES is tried first again.
    clock.t += 61
    ses.answers = [OK]
    assert chain.send(MESSAGE).provider_message_id == "ses:m-1"


def test_a_refused_message_moves_on_without_setting_the_provider_aside() -> None:
    ses, resend = _Fake(REFUSED, OK), _Fake(OK)
    chain = ep.FailoverProvider([("ses", ses), ("resend", resend)], clock=_Clock())
    assert chain.send(MESSAGE).provider_message_id == "resend:m-1"
    assert chain.state()["ses"] == 0  # e.g. SES sandbox: the next recipient may be fine
    assert chain.send(MESSAGE).provider_message_id == "ses:m-1"


def test_a_conflict_stops_the_chain_so_nothing_is_sent_twice() -> None:
    first, second = _Fake(CONFLICT), _Fake(OK)
    r = ep.FailoverProvider([("resend", first), ("ses", second)], clock=_Clock()).send(MESSAGE)
    assert r.reason_code == "provider_conflict" and second.calls == 0


def test_when_every_provider_fails_the_message_is_retried_later() -> None:
    clock = _Clock()
    chain = ep.FailoverProvider([("ses", _Fake(DOWN)), ("resend", _Fake(LIMIT))], clock=clock)
    r = chain.send(MESSAGE)
    assert (r.accepted, r.retryable) == (False, True)
    # Both rest now: the worker is told when to come back, nothing is called.
    rested = chain.send(MESSAGE)
    assert rested.reason_code == "provider_rate_limited" and rested.retry_after_seconds
    assert rested.retry_after_seconds <= 60


def _settings(**over: Any) -> SimpleNamespace:
    base = dict(
        ses_access_key_id=None,
        ses_secret_access_key=None,
        ses_region="eu-central-1",
        zeptomail_token=None,
        zeptomail_api_url=ep.ZEPTOMAIL_API_URL,
        resend_api_key="re_x",
        email_provider_order="ses,zeptomail,resend",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_only_configured_providers_join_in_the_chosen_order() -> None:
    assert isinstance(ep.build_provider(_settings()), ResendProvider)  # unchanged default
    chain = ep.build_provider(
        _settings(ses_access_key_id="A", ses_secret_access_key="S", zeptomail_token="z")
    )
    assert isinstance(chain, ep.FailoverProvider) and chain.names == ["ses", "zeptomail", "resend"]
    flipped = ep.configured(
        _settings(zeptomail_token="z", email_provider_order="resend, zeptomail")
    )
    assert [n for n, _ in flipped] == ["resend", "zeptomail"]
    with pytest.raises(RuntimeError):
        ep.build_provider(_settings(resend_api_key=None))


def test_production_wants_both_ses_keys_or_neither() -> None:
    from pydantic import ValidationError

    from tests.prod_settings import production_settings

    with pytest.raises(ValidationError, match="SES_ACCESS_KEY_ID and SES_SECRET_ACCESS_KEY"):
        production_settings(ses_access_key_id="AKIA123")
    assert production_settings(
        ses_access_key_id="AKIA123", ses_secret_access_key="real-s3cret-value"
    )
    with pytest.raises(ValidationError, match="ZEPTOMAIL_TOKEN is a placeholder"):
        production_settings(zeptomail_token="REPLACE_ME_token")
