"""More e-mail providers, and a chain that moves to the next one when a provider
cannot take a message (owner's decision, 30 Oct 2026: "biri bitende digerine
kecsin").

* `SesProvider` - Amazon SES API v2 (`SendEmail`), signed with AWS Signature
  Version 4 here (no SDK: one request shape).
* `ZeptoMailProvider` - Zoho ZeptoMail's send API.
* `FailoverProvider` - tries its providers in order. A provider that answers
  "rate limited / quota", "unavailable", "auth rejected" or a network failure is
  set aside for a while (its cooldown) and the SAME message goes to the next one
  at once; a refusal of this message (e.g. SES's sandbox refusing an unverified
  recipient) moves on without a cooldown. Only "conflict" (Resend: another
  request holds this idempotency key, i.e. it may be sending right now) stops
  the chain, so it cannot send twice on purpose.
* `build_provider(settings)` - the chain of the configured providers, in
  `EMAIL_PROVIDER_ORDER`; just Resend when nothing else is configured.

Known trade-off: a provider that drops the connection after it has taken the
message looks like a network failure, so the next provider may send it again -
the attendee then gets the same code twice (harmless: one code, used once).

Same hard rules as otp_provider.py: never log, store or return a provider's
response body or the request; `accepted` means only "the provider took it". The
provider that took it is recorded as a prefix of the message id ("ses:...").
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from app.modules.auth.otp_provider import (
    NETWORK_ERROR,
    PROVIDER_AUTH,
    PROVIDER_CONFLICT,
    PROVIDER_RATE_LIMITED,
    PROVIDER_REJECTED,
    PROVIDER_UNAVAILABLE,
    UNEXPECTED_ERROR,
    EmailMessage,
    EmailProvider,
    ResendProvider,
    SendResult,
)

_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
_ID = re.compile(r"^[A-Za-z0-9_.@<>\-]{1,200}$")

# How long a provider is set aside after each kind of failure, in seconds.
COOLDOWN: dict[str, float] = {
    PROVIDER_RATE_LIMITED: 60.0,  # or the provider's own Retry-After, if longer
    PROVIDER_UNAVAILABLE: 30.0,
    NETWORK_ERROR: 30.0,
    PROVIDER_AUTH: 600.0,
    UNEXPECTED_ERROR: 60.0,
}


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except ValueError:
        return None


def _safe_id(value: object) -> str | None:
    return value if isinstance(value, str) and _ID.match(value) else None


# --- Amazon SES (API v2, Signature Version 4) ------------------------------------


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    """AWS SigV4's derived key (kDate -> kRegion -> kService -> kSigning)."""
    k_date = _hmac(("AWS4" + secret).encode("utf-8"), date_stamp)
    return _hmac(_hmac(_hmac(k_date, region), service), "aws4_request")


def sigv4_headers(
    *,
    access_key: str,
    secret: str,
    region: str,
    host: str,
    path: str,
    body: bytes,
    now: datetime,
    service: str = "ses",
) -> dict[str, str]:
    """The headers of a signed JSON POST (content-type, host, date, hash, auth)."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {
        "content-type": "application/json",
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    signed = ";".join(sorted(headers))
    canonical = "\n".join(
        [
            "POST",
            path,
            "",
            "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)),
            signed,
            payload_hash,
        ]
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    signature = hmac.new(
        signing_key(secret, date_stamp, region, service), to_sign.encode(), hashlib.sha256
    ).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    return headers


# SES answers these when THIS account cannot send now (quota, paused, throttled):
# a reason to use the next provider, not a fault in the message.
_SES_ACCOUNT_LIMITS = ("LimitExceeded", "SendingPaused", "AccountSuspended", "TooManyRequests")


class SesProvider:
    def __init__(
        self,
        access_key: str,
        secret: str,
        region: str,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._access_key = access_key
        self._secret = secret
        self._region = region
        self._clock = clock

    def send(self, message: EmailMessage) -> SendResult:
        host = f"email.{self._region}.amazonaws.com"
        path = "/v2/email/outbound-emails"
        body = json.dumps(
            {
                "FromEmailAddress": message.from_email,
                "Destination": {"ToAddresses": [message.to]},
                "ReplyToAddresses": [message.from_email],
                "Content": {
                    "Simple": {
                        "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                        "Body": {
                            "Text": {"Data": message.text, "Charset": "UTF-8"},
                            "Html": {"Data": message.html, "Charset": "UTF-8"},
                        },
                    }
                },
            }
        ).encode("utf-8")
        try:
            headers = sigv4_headers(
                access_key=self._access_key,
                secret=self._secret,
                region=self._region,
                host=host,
                path=path,
                body=body,
                now=self._clock(),
            )
            response = httpx.post(
                f"https://{host}{path}", headers=headers, content=body, timeout=_TIMEOUT
            )
        except httpx.TransportError:
            return SendResult(False, reason_code=NETWORK_ERROR, retryable=True)
        except Exception:  # noqa: BLE001 - classified, never propagated, never logged with detail
            return SendResult(False, reason_code=UNEXPECTED_ERROR, retryable=False)
        status = response.status_code
        if 200 <= status < 300:
            try:
                message_id = _safe_id(response.json().get("MessageId"))
            except Exception:  # noqa: BLE001 - an unparseable success is still a success
                message_id = None
            return SendResult(True, provider_message_id=message_id, http_status=status)
        error_type = response.headers.get("x-amzn-errortype", "")
        if status == 429 or any(t in error_type for t in _SES_ACCOUNT_LIMITS):
            return SendResult(
                False,
                reason_code=PROVIDER_RATE_LIMITED,
                retryable=True,
                http_status=status,
                retry_after_seconds=_retry_after(response),
            )
        if status >= 500:
            return SendResult(
                False, reason_code=PROVIDER_UNAVAILABLE, retryable=True, http_status=status
            )
        if status in (401, 403):
            return SendResult(False, reason_code=PROVIDER_AUTH, retryable=False, http_status=status)
        return SendResult(False, reason_code=PROVIDER_REJECTED, retryable=False, http_status=status)


# --- ZeptoMail ----------------------------------------------------------------------

ZEPTOMAIL_API_URL = "https://api.zeptomail.com/v1.1/email"


def _split_from(from_email: str) -> dict[str, str]:
    """`Name <address>` -> ZeptoMail's {"address", "name"}."""
    match = re.match(r"^\s*(.*?)\s*<([^>]+)>\s*$", from_email)
    if match:
        name, address = match.group(1).strip().strip('"'), match.group(2).strip()
        return {"address": address, "name": name} if name else {"address": address}
    return {"address": from_email.strip()}


class ZeptoMailProvider:
    def __init__(self, token: str, api_url: str = ZEPTOMAIL_API_URL) -> None:
        # The console shows the token as "Zoho-enczapikey <key>"; either form works.
        self._auth = token if token.startswith("Zoho-enczapikey ") else f"Zoho-enczapikey {token}"
        self._url = api_url

    def send(self, message: EmailMessage) -> SendResult:
        sender = _split_from(message.from_email)
        try:
            response = httpx.post(
                self._url,
                headers={"Authorization": self._auth, "Accept": "application/json"},
                json={
                    "from": sender,
                    "to": [{"email_address": {"address": message.to}}],
                    "reply_to": [{"address": sender["address"]}],
                    "subject": message.subject,
                    "textbody": message.text,
                    "htmlbody": message.html,
                },
                timeout=_TIMEOUT,
            )
        except httpx.TransportError:
            return SendResult(False, reason_code=NETWORK_ERROR, retryable=True)
        except Exception:  # noqa: BLE001 - classified, never propagated, never logged with detail
            return SendResult(False, reason_code=UNEXPECTED_ERROR, retryable=False)
        status = response.status_code
        if 200 <= status < 300:
            try:
                message_id = _safe_id(response.json().get("request_id"))
            except Exception:  # noqa: BLE001
                message_id = None
            return SendResult(True, provider_message_id=message_id, http_status=status)
        if status == 429 or status == 402 or _mentions_credit(response):
            return SendResult(
                False,
                reason_code=PROVIDER_RATE_LIMITED,
                retryable=True,
                http_status=status,
                retry_after_seconds=_retry_after(response),
            )
        if status >= 500:
            return SendResult(
                False, reason_code=PROVIDER_UNAVAILABLE, retryable=True, http_status=status
            )
        if status in (401, 403):
            return SendResult(False, reason_code=PROVIDER_AUTH, retryable=False, http_status=status)
        return SendResult(False, reason_code=PROVIDER_REJECTED, retryable=False, http_status=status)


def _mentions_credit(response: httpx.Response) -> bool:
    """ZeptoMail refuses with a 4xx when the account's credits are used up; only
    the word is looked at, nothing of the body is kept."""
    try:
        return "credit" in response.text.casefold()
    except Exception:  # noqa: BLE001
        return False


# --- the chain ----------------------------------------------------------------------


class FailoverProvider:
    def __init__(
        self,
        providers: Sequence[tuple[str, EmailProvider]],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not providers:
            raise ValueError("at least one provider")
        self._providers = list(providers)
        self._clock = clock
        self._lock = threading.Lock()
        self._resting: dict[str, float] = {}  # name -> monotonic time it may be used again

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self._providers]

    def state(self) -> dict[str, Any]:
        """For the status line: each provider and the seconds it still rests."""
        now = self._clock()
        with self._lock:
            return {
                name: max(0.0, round(self._resting.get(name, 0.0) - now, 1))
                for name, _ in self._providers
            }

    def _rest(self, name: str, result: SendResult) -> None:
        seconds = COOLDOWN.get(result.reason_code or "", 0.0)
        if result.retry_after_seconds:
            seconds = max(seconds, result.retry_after_seconds)
        if seconds > 0:
            with self._lock:
                self._resting[name] = self._clock() + seconds

    def send(self, message: EmailMessage) -> SendResult:
        now = self._clock()
        with self._lock:
            ready = [(n, p) for n, p in self._providers if self._resting.get(n, 0.0) <= now]
            soonest = min(self._resting.get(n, 0.0) for n, _ in self._providers) - now
        if not ready:
            return SendResult(
                False,
                reason_code=PROVIDER_RATE_LIMITED,
                retryable=True,
                retry_after_seconds=max(1.0, soonest),
            )
        last: SendResult | None = None
        retryable = False
        for name, provider in ready:
            result = provider.send(message)
            if result.accepted:
                ident = result.provider_message_id
                return SendResult(
                    True,
                    provider_message_id=f"{name}:{ident}" if ident else name,
                    http_status=result.http_status,
                )
            if result.reason_code == PROVIDER_CONFLICT:
                return result  # this key may be sending right now: never a second copy
            self._rest(name, result)
            retryable = retryable or result.retryable
            last = result
        assert last is not None
        return SendResult(
            False,
            reason_code=last.reason_code,
            retryable=retryable,
            http_status=last.http_status,
            retry_after_seconds=last.retry_after_seconds,
        )


def configured(settings: Any) -> list[tuple[str, EmailProvider]]:
    """The configured providers in `EMAIL_PROVIDER_ORDER` (unknown names ignored)."""
    available: dict[str, Callable[[], EmailProvider]] = {}
    if settings.ses_access_key_id and settings.ses_secret_access_key:
        available["ses"] = lambda: SesProvider(
            settings.ses_access_key_id, settings.ses_secret_access_key, settings.ses_region
        )
    if settings.zeptomail_token:
        available["zeptomail"] = lambda: ZeptoMailProvider(
            settings.zeptomail_token, settings.zeptomail_api_url
        )
    if settings.resend_api_key:
        available["resend"] = lambda: ResendProvider(settings.resend_api_key)
    order = [n.strip().lower() for n in settings.email_provider_order.split(",") if n.strip()]
    return [(name, available[name]()) for name in dict.fromkeys(order) if name in available]


def build_provider(settings: Any) -> EmailProvider:
    chain = configured(settings)
    if not chain:
        raise RuntimeError("no e-mail provider is configured")
    if len(chain) == 1:
        return chain[0][1]
    return FailoverProvider(chain)
