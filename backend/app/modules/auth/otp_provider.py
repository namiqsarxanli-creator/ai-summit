"""The email PROVIDER seam for the OTP outbox worker.

`EmailProvider.send` performs ONE attempt and never raises: it returns a
`SendResult` already classified into a short reason code and a
retryable / permanent verdict. The worker decides what to do with that.

Hard rules for every implementation:
* never log, store or return the provider's response body or the request
  (they contain the recipient address and the code);
* `accepted=True` means the provider took the message - nothing more.
  Inbox placement is not observable from here.

Tests use their own in-memory provider; the real `ResendProvider` is only
constructed by the worker process.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

import httpx

RESEND_API_URL = "https://api.resend.com/emails"
_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_\-]{1,100}$")

# Reason codes (stored in otp_deliveries.reason_code). Short, stable, safe.
PROVIDER_AUTH = "provider_auth_rejected"
PROVIDER_REJECTED = "provider_rejected_message"
PROVIDER_RATE_LIMITED = "provider_rate_limited"
PROVIDER_CONFLICT = "provider_conflict"
PROVIDER_UNAVAILABLE = "provider_unavailable"
NETWORK_ERROR = "network_error"
UNEXPECTED_ERROR = "unexpected_error"


@dataclass(frozen=True)
class EmailMessage:
    from_email: str
    to: str
    subject: str
    text: str
    html: str
    idempotency_key: str


@dataclass(frozen=True)
class SendResult:
    accepted: bool
    provider_message_id: str | None = None
    reason_code: str | None = None
    retryable: bool = False
    http_status: int | None = None
    retry_after_seconds: float | None = None


class EmailProvider(Protocol):
    def send(self, message: EmailMessage) -> SendResult: ...


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except ValueError:
        return None


class ResendProvider:
    """Resend's REST API (https://resend.com/docs/api-reference/emails/send-email).
    A direct call instead of the SDK: one endpoint, no templating."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def send(self, message: EmailMessage) -> SendResult:
        try:
            response = httpx.post(
                RESEND_API_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    # Identical on every attempt of one delivery, so a retry
                    # after an ambiguous timeout cannot send the email twice.
                    "Idempotency-Key": message.idempotency_key,
                },
                json={
                    "from": message.from_email,
                    "to": [message.to],
                    "reply_to": message.from_email,
                    "subject": message.subject,
                    "html": message.html,
                    "text": message.text,
                },
                timeout=_TIMEOUT,
            )
        except httpx.TransportError:
            return SendResult(False, reason_code=NETWORK_ERROR, retryable=True)
        except Exception:  # noqa: BLE001 - classified, never propagated, never logged with detail
            return SendResult(False, reason_code=UNEXPECTED_ERROR, retryable=False)

        status = response.status_code
        if 200 <= status < 300:
            return SendResult(True, provider_message_id=_message_id(response), http_status=status)
        retry_after = _retry_after(response)
        if status == 429:
            return SendResult(
                False,
                reason_code=PROVIDER_RATE_LIMITED,
                retryable=True,
                http_status=status,
                retry_after_seconds=retry_after,
            )
        if status >= 500:
            return SendResult(
                False,
                reason_code=PROVIDER_UNAVAILABLE,
                retryable=True,
                http_status=status,
                retry_after_seconds=retry_after,
            )
        if status == 409:  # a concurrent request holds this idempotency key
            return SendResult(
                False, reason_code=PROVIDER_CONFLICT, retryable=True, http_status=status
            )
        if status in (401, 403):
            return SendResult(False, reason_code=PROVIDER_AUTH, retryable=False, http_status=status)
        return SendResult(False, reason_code=PROVIDER_REJECTED, retryable=False, http_status=status)


def _message_id(response: httpx.Response) -> str | None:
    try:
        value = response.json().get("id")
    except Exception:  # noqa: BLE001 - an unparseable success body is still a success
        return None
    return value if isinstance(value, str) and _MESSAGE_ID.match(value) else None
