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

from app.core.config import get_settings
from app.modules.attendees.service import PendingOtpDelivery

logger = logging.getLogger(__name__)


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
    # documented exception, never active outside DEMO_MODE.
    logger.info(
        "[DEMO_MODE] OTP for %s (purpose=%s): %s",
        delivery.email,
        delivery.purpose.value,
        delivery.code,
    )
