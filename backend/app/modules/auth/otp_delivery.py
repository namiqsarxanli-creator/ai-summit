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
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.modules.attendees.service import PendingOtpDelivery

logger = logging.getLogger(__name__)
_demo_lock = threading.Lock()
_demo_codes: deque[tuple[datetime, int, str, str, str]] = deque(maxlen=50)


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
    """
    settings = get_settings()
    if not settings.demo_mode:
        # ARCHITECTURE.md §8.2: real email delivery is "an email
        # adapter, not a redesign" — but that adapter does not exist
        # yet (M04 explicitly ships only the demo path). Failing
        # loudly here is preferable to silently dropping a code no
        # attendee will ever see.
        raise RuntimeError(
            "no OTP delivery adapter is configured for DEMO_MODE=false; "
            "set DEMO_MODE=true for local/demo use"
        )
    # Deliberately logged: this is the architecture-sanctioned demo
    # path, gated on DEMO_MODE and unreachable in production (see
    # Settings._demo_mode_never_in_production). ARCHITECTURE.md §21.11
    # forbids logging OTP codes in general — this is the one, explicit,
    # documented exception, never active outside DEMO_MODE. DEBUG, not
    # INFO: §21.11 also keeps full email addresses out of INFO-level
    # logs; main._configure_logging opens this logger to DEBUG only in
    # DEMO_MODE.
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
            (datetime.now(UTC), generation, delivery.email, delivery.purpose.value, delivery.code)
        )
