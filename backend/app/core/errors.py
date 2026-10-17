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
    # Machine-readable context for the §20.2 envelope's `details`. Most errors
    # carry none; the few that do (TASK_RULE_CHANGED) say so in their docstring.
    details: dict[str, object] | None = None

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

    def __init__(self) -> None:
        super().__init__("This attendee has already earned points for this activity.")


class TaskNotFoundError(DomainError):
    """The activity does not exist OR belongs to another booth - one
    answer for both, so a staff member cannot probe other booths' ids."""

    code = "TASK_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("That activity is not available at your booth.")


class TaskInactiveError(DomainError):
    """The activity is the booth's own but has been switched off."""

    code = "TASK_INACTIVE"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("That activity is no longer active.")


class TaskNotConfiguredError(DomainError):
    """The activity has no point value yet (it predates the rule)."""

    code = "TASK_NOT_CONFIGURED"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("That activity has no point value yet. Ask an admin to set one.")


class TaskRuleChangedError(DomainError):
    """The activity's point value or repeat policy is no longer what the
    staff member confirmed. NOTHING was awarded. `details` carries the rule
    as it stands now (plus its new `rule_revision`) so the staff screen can
    show it and ask for a fresh confirmation instead of paying a value the
    attendee-facing booth never agreed to."""

    code = "TASK_RULE_CHANGED"
    status_code = 409

    def __init__(self, details: dict[str, object]) -> None:
        super().__init__(
            "This activity's points or repeat rule changed. Review it and confirm again."
        )
        self.details = details


class ClientOutdatedError(DomainError):
    """The award request came from a staff page that predates the current
    award contract (it names no confirmed `rule_revision`, or still sends a
    point value). Refused before any database access, so it can never create
    points.

    The code is deliberately the EXISTING `CSRF_REJECTED`, not a new one:
    every earlier staff page renders that code as "This request couldn't be
    verified. Please reload the page and try again." A new code would reach
    those pages as an unknown one and show the generic "Something went wrong.
    Please try again." - an invitation to retry something that can never
    succeed. `details.reason` tells the two apart for current code and logs;
    `message` (which old pages ignore) states the real cause."""

    code = "CSRF_REJECTED"
    status_code = 403

    def __init__(self) -> None:
        super().__init__(
            "This page is out of date. Reload the page (or reopen the app) and scan again"
            " - no points were awarded."
        )
        self.details = {"reason": "client_outdated"}


class UnknownInterestError(DomainError):
    """A submitted interest slug does not match any active `interests`
    row. ARCHITECTURE.md §20.3: UNKNOWN_INTEREST, 400. §8.1: "unknown
    slug -> UNKNOWN_INTEREST, rather than silently dropped, so an
    attendee is never told they matched on something that does not
    exist."
    """

    code = "UNKNOWN_INTEREST"
    status_code = 400

    def __init__(self, message: str | None = None, *, unknown: list[str] | None = None) -> None:
        super().__init__(message)
        # The profile editor names WHICH slugs were refused (unknown, or retired
        # from the catalogue) so the screen can say so instead of guessing.
        if unknown:
            self.details = {"slugs": sorted(unknown)}


class ProfileConflictError(DomainError):
    """The profile was changed (on another device or tab) after the client
    loaded it: its `expected_version` is no longer current. NOTHING was saved.
    `details.profile` is the profile as it stands now, so the screen can show
    the difference and let the person decide - their own text is never lost."""

    code = "PROFILE_CONFLICT"
    status_code = 409

    def __init__(self, current: dict[str, object]) -> None:
        super().__init__("Your profile was changed elsewhere. Review the latest version.")
        self.details = {"profile": current}


class PhotoInvalidError(DomainError):
    """The upload is not a usable photo: not a JPEG/PNG/WebP by its CONTENT
    (SVG, GIF, HEIC... and renamed files included), corrupt, animated or too
    small. `details.reason` is a short machine code, never anything from the file."""

    code = "PHOTO_INVALID"
    status_code = 400

    def __init__(self, reason: str) -> None:
        super().__init__("That file is not a supported photo.")
        self.details = {"reason": reason}


class PhotoTooLargeError(DomainError):
    """Over the size limit in bytes, or more pixels than the limit (a tiny file
    that claims to expand to a huge canvas). Refused before any pixel is decoded."""

    code = "PHOTO_TOO_LARGE"
    status_code = 413

    def __init__(self, *, max_bytes: int, max_pixels: int) -> None:
        super().__init__("That photo is too large.")
        self.details = {"max_bytes": max_bytes, "max_pixels": max_pixels}


class PhotoBusyError(DomainError):
    """Too many photos are being processed at once; nothing was changed."""

    code = "PHOTO_BUSY"
    status_code = 503


class PhotoStorageUnavailableError(DomainError):
    """The photo store could not be written (full or missing volume). Nothing was
    changed: the previous photo, if any, is still the attendee's photo."""

    code = "PHOTO_STORAGE_UNAVAILABLE"
    status_code = 503


class MediaNotFoundError(DomainError):
    """No such photo file (never made, replaced, removed, or not a photo name)."""

    code = "MEDIA_NOT_FOUND"
    status_code = 404


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


class InvalidQrError(DomainError):
    """ARCHITECTURE.md §20.3: INVALID_QR, 404. §9.4: malformed, unknown,
    revoked, provisional-attendee and deleted-attendee tokens all get
    exactly this response — one class, one fixed message, so no branch
    can word it differently. (M11 will also raise it for a `scan_ref`
    whose signature does not verify.)"""

    code = "INVALID_QR"
    status_code = 404
    MESSAGE = "This QR code isn't valid. Ask the attendee to reopen their card."

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class StaffNotAuthorizedError(DomainError):
    """ARCHITECTURE.md §20.3: STAFF_NOT_AUTHORIZED, 403 — "Wrong role,
    inactive account, or admin-only route"."""

    code = "STAFF_NOT_AUTHORIZED"
    status_code = 403


class InvalidCredentialsError(DomainError):
    """ARCHITECTURE.md §12.1 / §20.3: INVALID_CREDENTIALS, 401 —
    "no distinction between unknown email and wrong password." The
    same generic outcome also covers a correct password on an inactive
    account (§7.7: "deactivation kills future logins") — deactivation
    is never a distinguishable response either, for the same
    enumeration-resistance reason."""

    code = "INVALID_CREDENTIALS"
    status_code = 401


class AccountLockedError(DomainError):
    """ARCHITECTURE.md §21.8: ACCOUNT_LOCKED, 429 — "further attempts
    return ACCOUNT_LOCKED without evaluating the password"."""

    code = "ACCOUNT_LOCKED"
    status_code = 429


class BoothNotFoundError(DomainError):
    """No catalogue entry in ARCHITECTURE.md §20.3 names an unknown or
    inactive booth specifically. M07 adds this code, following the
    naming of the catalogue's own USER_NOT_FOUND ("referenced but
    absent") rather than reusing a code that names the wrong entity —
    reported in M07's final response as a documented, additive
    gap-fill, the same category as GET /api/interests (M04) and
    GET /api/attendees/me/card (M06)."""

    code = "BOOTH_NOT_FOUND"
    status_code = 404


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


class ScanExpiredError(DomainError):
    """ARCHITECTURE.md §13.3 check 8 / §20.3: SCAN_EXPIRED, 400.
    Raised only at B3a — a NEW award whose `scan_ref` is older than
    120s. A COMPLETED replay never reaches this check (§13.4: the
    idempotency lookup at B3 stops first), so an expired envelope can
    still replay an already-committed success."""

    code = "SCAN_EXPIRED"
    status_code = 400

    def __init__(self) -> None:
        super().__init__("That scan expired. Scan again.")


class IdempotencyKeyReuseError(DomainError):
    """ARCHITECTURE.md §13.4 / §20.3: IDEMPOTENCY_KEY_REUSE, 409. The
    same `(staff_account_id, idempotency_key)` was already used for a
    request with a different fingerprint (different card token or
    different points) — never a silent second award."""

    code = "IDEMPOTENCY_KEY_REUSE"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("This request has already been used for a different award.")


class InvalidPointValueError(DomainError):
    """ARCHITECTURE.md §14.3 step B5 / §20.3: INVALID_POINT_VALUE, 400.
    Allow-list membership, never a range check — `12` is rejected even
    though it lies between the booth's `10` and `15` presets."""

    code = "INVALID_POINT_VALUE"
    status_code = 400

    def __init__(self) -> None:
        super().__init__("That point value isn't offered for this booth.")


class AwardInProgressError(DomainError):
    """ARCHITECTURE.md §13.4 / §20.3: AWARD_IN_PROGRESS, 409. A safety
    valve only — the idempotency claim (B7) waited past `lock_timeout`
    (2s) for a same-key transaction that is stuck or pathologically
    slow. Normal concurrency waits and replays instead; this is never
    part of the normal contract."""

    code = "AWARD_IN_PROGRESS"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("This award is already being processed. Please try again.")


class InvalidCursorError(DomainError):
    """M13: the `cursor` query parameter on `GET /api/leaderboard`
    (show-all view) failed to decode. Not in ARCHITECTURE.md's §20.3
    catalogue — no locked code names this specific failure — so this
    reuses the existing generic VALIDATION_ERROR code/status rather
    than inventing a new catalogue entry for a value that is always
    server-issued and opaque to a well-behaved client; a genuinely
    malformed value (tampered or hand-typed) is a request-shape
    problem, exactly what VALIDATION_ERROR already means."""

    code = "VALIDATION_ERROR"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("The pagination cursor is invalid.")


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


class PointsEntryNotFoundError(DomainError):
    """The ledger entry an administrator asked to reverse does not exist."""

    code = "POINTS_ENTRY_NOT_FOUND"
    status_code = 404


class PointsNotReversibleError(DomainError):
    """Only an AWARD can be reversed. A REVERSAL cannot itself be reversed
    (not supported; a mistaken reversal is corrected by a new award)."""

    code = "POINTS_NOT_REVERSIBLE"
    status_code = 409


class PointsAlreadyReversedError(DomainError):
    """This AWARD already has its REVERSAL (`uq_points_reverses`): a row is
    reversed at most once, whichever request got there first."""

    code = "POINTS_ALREADY_REVERSED"
    status_code = 409


class CurrentPasswordInvalidError(DomainError):
    """The current password given to change a password was wrong. A code of
    its own (not INVALID_CREDENTIALS, whose message means "sign in again")."""

    code = "CURRENT_PASSWORD_INVALID"
    status_code = 400


class PasswordUnchangedError(DomainError):
    """The new password is the same as the current one."""

    code = "PASSWORD_UNCHANGED"
    status_code = 400


class PasswordResetUnavailableError(DomainError):
    """The reset email cannot be sent in this deployment (no email outbox is
    configured). Said explicitly only to an administrator who asked; the public
    request answers the same generic message whatever happens."""

    code = "PASSWORD_RESET_UNAVAILABLE"
    status_code = 503


class MasterclassNotFoundError(DomainError):
    """No such masterclass or block - or one an attendee cannot see (not
    published, not scheduled). One answer for all."""

    code = "MASTERCLASS_NOT_FOUND"
    status_code = 404


class MasterclassFullError(DomainError):
    """Every seat is taken (or people are already queueing for one). Nothing
    changed - a choice the attendee already holds in the block is kept.
    `details.waitlist_open` says whether the waitlist still has room."""

    code = "MASTERCLASS_FULL"
    status_code = 409

    def __init__(self, *, waitlist_open: bool = False) -> None:
        super().__init__("This masterclass is full.")
        self.details = {"waitlist_open": waitlist_open}


class MasterclassWaitlistFullError(DomainError):
    """Seats and waitlist are both full: there is no place at all."""

    code = "MASTERCLASS_WAITLIST_FULL"
    status_code = 409


class MasterclassChangesClosedError(DomainError):
    """The block's change deadline has passed (server clock)."""

    code = "MASTERCLASS_CHANGES_CLOSED"
    status_code = 409


class MasterclassSeatWouldBeLostError(DomainError):
    """The change would give up a CONFIRMED seat for a waitlist place. Nothing
    changed; the client must ask the attendee and resend with
    `give_up_confirmed_seat: true`."""

    code = "MASTERCLASS_SEAT_WOULD_BE_LOST"
    status_code = 409


class MasterclassChoiceConflictError(DomainError):
    """A stale request cannot replace a newer choice. Refresh before retrying."""

    code = "MASTERCLASS_CHOICE_CONFLICT"
    status_code = 409


class MasterclassAdminConflictError(DomainError):
    """An administrator change that would break existing reservations (capacity
    below the confirmed seats, deleting or moving a masterclass that has
    reservations, overlapping blocks). Nothing changed; `details.reason` is a
    short machine code."""

    code = "MASTERCLASS_ADMIN_CONFLICT"
    status_code = 409

    def __init__(self, reason: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.details = {"reason": reason, **details}


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
    "uq_points_task_user_once": DuplicateTaskRewardError,
    # --- control-flow signal, not an error ------------------------------------
    "pk_idempotency_records": IdempotencyKeyConflict,
    # --- structural invariants that indicate an application bug --------------
    "uq_points_reverses": PointsAlreadyReversedError,
    "uq_contact_card_live_user": UnexpectedConstraintViolationError,
    "ck_staff_role_booth": UnexpectedConstraintViolationError,
    "fk_points_task_booth": UnexpectedConstraintViolationError,
    "pk_user_interests": UnexpectedConstraintViolationError,
    "uq_preset_global_points": UnexpectedConstraintViolationError,
    # --- masterclasses (migration 0017): the service checks first under the
    # masterclass row lock, so these firing means a writer skipped that path.
    "ck_masterclass_reservations_capacity": MasterclassFullError,
    "ck_masterclass_reservations_waitlist_capacity": MasterclassWaitlistFullError,
    "uq_masterclass_reservations_one_active_per_block": UnexpectedConstraintViolationError,
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
        # The name is logged above; the client gets no schema detail.
        return UnexpectedConstraintViolationError("unexpected constraint violation")

    return error_cls()


class ClientUpdateRequiredError(DomainError):
    """A page of an earlier version sent a request this server no longer accepts
    in that form (an offline scan queue without its owner label). Answered as a
    TEMPORARY failure on purpose: that page keeps its queue and retries instead
    of discarding the scans, and a reload brings the current page, which moves
    them into its quarantine for a person to decide (competition/offline.py)."""

    code = "CLIENT_UPDATE_REQUIRED"
    status_code = 503
    expected = True
    retry_after = 60
