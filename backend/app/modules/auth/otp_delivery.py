"""The OTP delivery adapter seam — ARCHITECTURE.md §8.2's "demo
simplification, explicitly bounded": "when DEMO_MODE=true, the OTP is
written to the server log ... instead of being emailed. The endpoint
contract, hashing, expiry, single-use and attempt-limit logic are
identical in both modes... Adding real delivery later is an email
adapter, not a redesign."

This module is that seam. `deliver_otp` is the one function every
caller uses; only its body changes when a real email provider is
added — nothing about issuance or verification (app/modules/attendees/
service.py) depends on how the code reaches the attendee.

Two real-delivery paths exist (the choice is `Settings.outbox_enabled`):

* OUTBOX (production, and any deployment with OTP_OUTBOX_ENCRYPTION_KEY):
  `issue_otp` wrote a durable outbox row in the same transaction as the
  challenge and the separate worker (`python -m app.worker`,
  app/modules/auth/otp_outbox.py) sends it. `deliver_otp` then sends
  nothing itself - `delivery.queued` is True.
* LEGACY in-process sender (this file): development convenience when no
  encryption key is configured. Best-effort - an email in flight is lost if
  the process dies. Never used in production (the key is mandatory there).

Real delivery (Resend) and the demo log are independent, not
either/or: a demo deployment with RESEND_API_KEY set gets BOTH — the
real email, and the existing DEMO_MODE log line / admin "recent OTPs"
panel, so an operator can still see the code without reading their own
inbox during a rehearsal. `deliver_otp` raises if neither is
configured, rather than silently dropping a code.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx

from app.core.config import get_settings
from app.modules.attendees.service import PendingOtpDelivery
from app.modules.auth.otp_email import render_otp_email

logger = logging.getLogger(__name__)
_demo_lock = threading.Lock()
_demo_codes: deque[tuple[datetime, int, str, str, str]] = deque(maxlen=50)

_RESEND_API_URL = "https://api.resend.com/emails"

# A request thread must never wait on a third-party API: delivery runs
# on a small worker pool. This also removes a timing oracle — an email
# is only sent for some register/login branches (never for an
# already-ACTIVE address on register), so sending inline would make
# those responses measurably slower than the others (§8.1: the response
# must not reveal which branch ran).
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="otp-email")
_pending: set[Future[None]] = set()
_pending_lock = threading.Lock()

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (0.5, 1.5)
_TIMEOUT = httpx.Timeout(8.0, connect=3.0)


def _send_via_resend(
    *, api_key: str, from_email: str, delivery: PendingOtpDelivery, idempotency_key: str
) -> None:
    """One attempt, a direct REST call — not the `resend` SDK (one
    endpoint, no templating, not worth a dependency). Raises on any
    network failure or non-2xx response; `_send_with_retries` decides
    what to do about it."""
    content = render_otp_email(delivery.language, delivery.purpose, delivery.code)
    response = httpx.post(
        _RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Idempotency-Key": idempotency_key,
        },
        json={
            "from": from_email,
            "to": [delivery.email],
            "reply_to": from_email,
            "subject": content.subject,
            "html": content.html,
            "text": content.text,
        },
        timeout=_TIMEOUT,
    )
    response.raise_for_status()


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return isinstance(exc, httpx.TransportError)


def _send_with_retries(*, api_key: str, from_email: str, delivery: PendingOtpDelivery) -> None:
    """Never raises: it runs on a worker thread nobody awaits. A final
    failure is logged WITHOUT the address or code (§21.11) — only the
    purpose, attempt count and a reason class — and the attendee
    recovers by requesting a new code (rate limited, §21.7)."""
    reason = "unknown"
    # One key per delivery, shared by all its attempts, so a retry after
    # an ambiguous timeout can never email the code twice.
    idempotency_key = uuid.uuid4().hex
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            _send_via_resend(
                api_key=api_key,
                from_email=from_email,
                delivery=delivery,
                idempotency_key=idempotency_key,
            )
            return
        except Exception as exc:  # noqa: BLE001 — classified below, never propagated
            reason = (
                f"http {exc.response.status_code}"
                if isinstance(exc, httpx.HTTPStatusError)
                else type(exc).__name__
            )
            if attempt == _MAX_ATTEMPTS or not _is_retryable(exc):
                break
            time.sleep(_BACKOFF_SECONDS[attempt - 1])
    logger.error(
        "OTP email delivery failed (purpose=%s, reason=%s)", delivery.purpose.value, reason
    )


def _submit_email(*, api_key: str, from_email: str, delivery: PendingOtpDelivery) -> None:
    future = _executor.submit(
        _send_with_retries, api_key=api_key, from_email=from_email, delivery=delivery
    )
    with _pending_lock:
        _pending.add(future)
    future.add_done_callback(_forget)


def _forget(future: Future[None]) -> None:
    with _pending_lock:
        _pending.discard(future)


def wait_for_pending(timeout: float = 15.0) -> None:
    """Blocks until queued emails have been attempted. For tests and
    orderly shutdown; request handlers never call it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _pending_lock:
            if not _pending:
                return
        time.sleep(0.01)


def recent_demo_otps() -> list[dict[str, str]]:
    """Short-lived delivery-adapter memory, never reconstructed from OTP hashes."""
    if not get_settings().demo_mode:
        return []
    from app.modules.realtime.service import current_dataset_generation

    generation = current_dataset_generation()
    cutoff = datetime.now(UTC) - timedelta(minutes=10)
    with _demo_lock:
        return [
            {"email": email, "purpose": purpose, "code": code, "issued_at": issued_at.isoformat()}
            for issued_at, gen, email, purpose, code in reversed(_demo_codes)
            if gen == generation and issued_at >= cutoff
        ]


def deliver_otp(delivery: PendingOtpDelivery) -> None:
    """Never called from inside a mutating_transaction block — always
    after the transaction that created the challenge has committed
    (see app/modules/attendees/routers.py and
    app/modules/auth/routers.py), so a code is never announced for a
    challenge that did not actually persist.

    The two delivery paths below are independent, not either/or (see
    module docstring): `settings.resend_api_key` set queues a real
    email (sent asynchronously, retried on transient failure, a final
    failure logged — never raised into the request, which would leak
    which register/login branch ran); `settings.demo_mode` set
    additionally logs it and feeds the admin "recent OTPs" panel. If
    NEITHER applies, this raises: that is a misconfiguration, not a
    runtime hiccup, and must not be silent.
    """
    settings = get_settings()
    if not settings.resend_api_key and not settings.demo_mode:
        raise RuntimeError(
            "no OTP delivery is configured: set RESEND_API_KEY, or DEMO_MODE=true "
            "for local/demo use"
        )
    if settings.resend_api_key and not delivery.queued:
        _submit_email(
            api_key=settings.resend_api_key, from_email=settings.otp_from_email, delivery=delivery
        )
    if settings.demo_mode:
        # Deliberately logged: this is the architecture-sanctioned demo
        # path, gated on DEMO_MODE and unreachable in production (see
        # Settings._demo_mode_never_in_production). ARCHITECTURE.md
        # §21.11 forbids logging OTP codes in general — this is the
        # one, explicit, documented exception, never active outside
        # DEMO_MODE. DEBUG, not INFO: §21.11 also keeps full email
        # addresses out of INFO-level logs; main._configure_logging
        # opens this logger to DEBUG only in DEMO_MODE.
        logger.debug(
            "[DEMO_MODE] OTP for %s (purpose=%s): %s",
            delivery.email,
            delivery.purpose.value,
            delivery.code,
        )
        from app.modules.realtime.service import current_dataset_generation

        generation = current_dataset_generation()
        with _demo_lock:
            _demo_codes.append(
                (
                    datetime.now(UTC),
                    generation,
                    delivery.email,
                    delivery.purpose.value,
                    delivery.code,
                )
            )
