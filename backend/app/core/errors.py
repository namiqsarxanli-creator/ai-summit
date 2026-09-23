"""Domain errors and the constraint-name -> error translation foundation.

ARCHITECTURE.md §7.14: "The application catches IntegrityError, inspects
the violated constraint name, and maps it to a domain error... never by
which error the database happens to report first." §20.4: "a raw
IntegrityError reaching the handler is a bug, and is logged as one."

M02 scope is the FOUNDATION only: the DomainError hierarchy, the
centralised constraint -> outcome map, and the translation function.
No feature endpoint or HTTP exception handler exists yet (§20's full
envelope and rule set land with the first mutating feature, M04+) — but
every later feature reuses this exact map rather than each writing its
own ad hoc constraint-name comparison.
"""

from __future__ import annotations

import logging
import uuid
from typing import Final

from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)


def error_envelope(
    code: str, message: str, *, details: dict[str, object] | None = None
) -> dict[str, object]:
    """ARCHITECTURE.md §20.2's single failure shape. `correlation_id`
    lets a developer find the server-side log entry from what the
    client saw, without the client ever seeing internals."""
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "correlation_id": str(uuid.uuid4()),
        }
    }


class DomainError(Exception):
    """Base for every error with a stable, documented API contract.

    ``code`` and ``status_code`` come directly from ARCHITECTURE.md
    §20.3's error catalogue. Only outcomes that are meant to reach an
    HTTP boundary as an error response are DomainErrors — see
    ``IdempotencyKeyConflict`` below for the one constraint outcome
    that is deliberately NOT one.
    """

    code: str = "INTERNAL_ERROR"
    status_code: int = 500

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code)


class DuplicateFeedbackRewardError(DomainError):
    """`uq_points_feedback_user_booth` violated.

    ARCHITECTURE.md §20.3: DUPLICATE_FEEDBACK, 409. §15 notes the
    feedback endpoint itself never actually returns this code (a
    second submission is a 200 with ``already_rewarded: true``,
    detected via SAVEPOINT rather than surfaced as an error) — this
    class exists so the constraint is still mapped for any other path
    that might hit it (e.g. a future correction/reversal tool).
    """

    code = "DUPLICATE_FEEDBACK"
    status_code = 409


class DuplicateTaskRewardError(DomainError):
    """`uq_points_dedupe_key` violated — a TASK repeat the configured
    policy forbids. ARCHITECTURE.md §20.3: DUPLICATE_TASK_REWARD, 409.
    """

    code = "DUPLICATE_TASK_REWARD"
    status_code = 409


class UnknownInterestError(DomainError):
    """A submitted interest slug does not match any active `interests`
    row. ARCHITECTURE.md §20.3: UNKNOWN_INTEREST, 400. §8.1: "unknown
    slug -> UNKNOWN_INTEREST, rather than silently dropped, so an
    attendee is never told they matched on something that does not
    exist."
    """

    code = "UNKNOWN_INTEREST"
    status_code = 400


class OtpInvalidError(DomainError):
    """Wrong, expired, consumed, or attempt-exhausted OTP code.
    ARCHITECTURE.md §20.3: OTP_INVALID, 400 — "one code for all four"
    outcomes, so the API cannot be used as an oracle distinguishing a
    wrong guess from an expired or already-used code.
    """

    code = "OTP_INVALID"
    status_code = 400


class UnauthenticatedError(DomainError):
    """ARCHITECTURE.md §20.3: UNAUTHENTICATED, 401 — "Missing, expired
    or revoked session." Deliberately one code for all three, and for a
    session of the wrong principal type: which of them it was is not
    the client's business."""

    code = "UNAUTHENTICATED"
    status_code = 401


class RegistrationNotVerifiedError(DomainError):
    """ARCHITECTURE.md §20.3: REGISTRATION_NOT_VERIFIED, 403 — "Action
    requires an ACTIVE attendee (§8.1)"."""

    code = "REGISTRATION_NOT_VERIFIED"
    status_code = 403


class RateLimitedError(DomainError):
    """ARCHITECTURE.md §20.3: RATE_LIMITED, 429 — "Throttle exceeded".
    `retry_after` (seconds) is surfaced as a Retry-After header."""

    code = "RATE_LIMITED"
    status_code = 429

    def __init__(self, retry_after: int, message: str | None = None) -> None:
        super().__init__(message or "Too many requests. Please wait and try again.")
        self.retry_after = retry_after


class MaintenanceError(DomainError):
    """A fenced mutation's `lock_timeout` expired waiting on the reset
    fence (PostgreSQL SQLSTATE 55P03, lock_not_available).
    ARCHITECTURE.md §19.4: "or its lock_timeout (2 s) expires and it
    returns MAINTENANCE". §20.3: MAINTENANCE, 503.
    """

    code = "MAINTENANCE"
    status_code = 503


class UnexpectedConstraintViolationError(DomainError):
    """A named, business-critical constraint was violated in a shape
    that should be structurally impossible for a correctly written
    service to produce (e.g. the composite task/booth FK, or the
    staff-role/booth CHECK) — every legitimate path that could reach
    these is already prevented earlier by server-derived values, never
    by client input. ARCHITECTURE.md §20.4: "a raw IntegrityError
    reaching the handler is a bug, and is logged as one." Maps to the
    generic INTERNAL_ERROR code — there is no more specific catalogue
    entry because no normal user action is meant to trigger this.
    """

    code = "INTERNAL_ERROR"
    status_code = 500


class IdempotencyKeyConflict(Exception):  # noqa: N818 — deliberately not an Error suffix; see docstring
    """NOT a DomainError.

    A conflict on `pk_idempotency_records` means "another transaction
    already claimed — or already completed — this exact
    (staff_account_id, idempotency_key)". ARCHITECTURE.md §13.4 is
    explicit that the caller's response to this is to look the record
    up and either wait-then-replay its stored snapshot, or (if the
    fingerprint differs) raise `IDEMPOTENCY_KEY_REUSE` itself — the
    constraint violation is a control-flow signal, not a terminal
    error in its own right. Kept out of the DomainError hierarchy on
    purpose so a future award service cannot accidentally let this
    propagate straight to an HTTP response without handling it.
    """


# Every business-critical constraint named in ARCHITECTURE.md §7.14,
# plus the additional structural constraints declared alongside the
# M02 models. The map is intentionally total for constraints that can
# realistically fire — anything absent here is deliberately NOT a
# "guess": translate_integrity_error() logs and refuses to invent a
# mapping for a name it doesn't recognise.
CONSTRAINT_ERROR_MAP: Final[dict[str, type[Exception]]] = {
    # --- user-facing outcomes -------------------------------------------------
    "uq_points_feedback_user_booth": DuplicateFeedbackRewardError,
    "uq_points_dedupe_key": DuplicateTaskRewardError,
    # --- control-flow signal, not an error ------------------------------------
    "pk_idempotency_records": IdempotencyKeyConflict,
    # --- structural invariants that indicate an application bug --------------
    "uq_points_reverses": UnexpectedConstraintViolationError,
    "uq_contact_card_live_user": UnexpectedConstraintViolationError,
    "ck_staff_role_booth": UnexpectedConstraintViolationError,
    "fk_points_task_booth": UnexpectedConstraintViolationError,
    "pk_user_interests": UnexpectedConstraintViolationError,
    "uq_preset_global_points": UnexpectedConstraintViolationError,
}


def translate_integrity_error(err: IntegrityError) -> Exception:
    """Dispatch a raw ``IntegrityError`` by constraint NAME.

    This is the only sanctioned way a later service turns a database
    constraint violation into a domain-meaningful outcome. It never
    branches on error message text or on "whichever violation
    PostgreSQL happened to report first" (ARCHITECTURE.md §7.14,
    invariant #24) — only on ``err.orig.diag.constraint_name``, which
    psycopg (and every other PEP 249 driver's DB-API 2.0 diagnostics
    object) populates from the server's own constraint identification,
    not from parsing a human-readable message.

    Returns the exception to raise; does not raise it itself, so the
    caller controls whether/how a SAVEPOINT is rolled back first.
    """
    diag = getattr(err.orig, "diag", None)
    constraint_name = getattr(diag, "constraint_name", None)

    if constraint_name is None:
        # Not a named-constraint violation (e.g. a NOT NULL failure with
        # no CHECK/UNIQUE/FK name attached, or a driver that doesn't
        # populate diagnostics). Nothing to translate — hand the
        # original error back unchanged rather than guessing.
        return err

    error_cls = CONSTRAINT_ERROR_MAP.get(constraint_name)
    if error_cls is None:
        logger.error("Unmapped constraint violation: %s", constraint_name)
        return UnexpectedConstraintViolationError(f"unmapped constraint: {constraint_name}")

    return error_cls()
