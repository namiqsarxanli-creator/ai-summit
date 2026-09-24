"""`POST /api/staff/auth/login`, `POST /api/staff/auth/logout`,
`GET /api/staff/me`, `GET /api/staff/points/presets` (M09), and
`POST /api/staff/scan/resolve` (M06 identity lookup, M10 adds
`scan_ref`) — ARCHITECTURE.md §12.1-§12.3, §13.1-§13.3, §21.7-§21.8.

Ordering in every mutating handler mirrors `auth/routers.py`: rate-limit
checks (pure, touch no application data) -> `mutating_transaction` (the
reset fence is its first statement) -> every authoritative read and
write -> commit -> post-commit side effects (the cookie). §19.4 permits
only dataset-independent work before the fence.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Response

from app.core.config import get_settings
from app.core.deps import (
    ClientIp,
    CurrentBoothStaff,
    CurrentStaff,
    CurrentStaffSessionId,
    SessionToken,
)
from app.core.errors import (
    AccountLockedError,
    InvalidCredentialsError,
    UnexpectedConstraintViolationError,
)
from app.core.ratelimit import (
    SCAN_RESOLVE_PER_STAFF,
    STAFF_LOGIN_PER_EMAIL,
    STAFF_LOGIN_PER_IP,
    limiter,
)
from app.core.sessions import clear_session_cookie, revoke_session, set_session_cookie
from app.db.session import DbSession, mutating_transaction
from app.modules.booths.models import Booth
from app.modules.contact_cards.service import resolve_card_identity
from app.modules.staff.scan_ref import issue_scan_ref
from app.modules.staff.schemas import (
    PresetOut,
    ScanResolveRequest,
    StaffLoginRequest,
    StaffMe,
    StaffScanResult,
)
from app.modules.staff.service import login_staff, resolved_presets_for_booth

router = APIRouter(prefix="/staff", tags=["staff"])


@router.post("/auth/login", status_code=204)
def login(payload: StaffLoginRequest, response: Response, db: DbSession, ip: ClientIp) -> None:
    """§12.1: email + password, verified against the Argon2id hash.
    Failure is `INVALID_CREDENTIALS` regardless of cause (unknown
    email, wrong password, or a correct password on a deactivated
    account) — never distinguishable (§12.1, §7.7). `ACCOUNT_LOCKED` is
    the one exception, and is itself generic: it names only "too many
    failed attempts", never which of the above the account is.

    The per-email limit is keyed on the submitted address whether or
    not it belongs to a real account, exactly like OTP's own rows
    (§21.7) — so the rate limiter itself cannot be used as an oracle.
    """
    email = payload.email.strip().lower()
    limiter.check([(STAFF_LOGIN_PER_EMAIL, email), (STAFF_LOGIN_PER_IP, ip)])

    with mutating_transaction(db) as tx:
        outcome = login_staff(tx, email=payload.email, password=payload.password)

    if outcome.locked:
        raise AccountLockedError("Too many failed attempts. Try again later.")
    if not outcome.ok:
        raise InvalidCredentialsError("Incorrect email or password.")

    assert outcome.session_token is not None  # ok=True always carries one
    set_session_cookie(response, outcome.session_token)


@router.post("/auth/logout", status_code=204)
def logout(response: Response, db: DbSession, token: SessionToken) -> None:
    """Identical shape to the attendee logout (§8.3): any principal
    type may revoke its own session through the same `revoke_session`.
    Idempotent — a missing or already-dead session is a no-op, and the
    cookie is cleared either way."""
    with mutating_transaction(db) as tx:
        revoke_session(tx, token)
    clear_session_cookie(response)


@router.get("/me", response_model=StaffMe)
def me(staff: CurrentStaff, db: DbSession) -> StaffMe:
    """ "Returning booth name so staff always see which booth they are
    awarding for" (M09) — open to any active staff account, booth staff
    or admin, since an admin needs to confirm their own signed-in
    identity too; `booth_name` is simply `None` for them. Read-only, so
    no reset fence (§19.4)."""
    booth_name = None
    if staff.booth_id is not None:
        booth = db.get(Booth, staff.booth_id)
        booth_name = booth.name if booth is not None else None
    return StaffMe(full_name=staff.full_name, role=staff.role, booth_name=booth_name)


@router.get("/points/presets", response_model=list[PresetOut])
def points_presets(staff: CurrentBoothStaff, db: DbSession) -> list[PresetOut]:
    """§13.2: the allow-list for the AUTHENTICATED staff member's own
    booth — never a booth named by the client, because there is no
    booth parameter to name one with. Admin-only accounts are rejected
    by `CurrentBoothStaff` before this body runs: presets exist only to
    feed the award buttons, and admins cannot award (§12.3)."""
    if staff.booth_id is None:
        # Structurally impossible: §7.7's CHECK constraint guarantees
        # every BOOTH_STAFF row has a booth_id. Defensive only.
        raise UnexpectedConstraintViolationError("booth staff account has no booth_id")
    presets = resolved_presets_for_booth(db, staff.booth_id)
    return [PresetOut(points=p.points, label=p.label) for p in presets]


@router.post("/scan/resolve", response_model=StaffScanResult)
def scan_resolve(
    payload: ScanResolveRequest,
    staff: CurrentStaff,
    session_id: CurrentStaffSessionId,
    db: DbSession,
) -> StaffScanResult:
    """§12.3: booth staff and admins may resolve a card. §21.7: 120/min
    per staff account. The M06 identity lookup is unchanged; M10 adds
    `scan_ref` (§13.3) — issued only once the token resolves to a real
    ACTIVE attendee, never for an INVALID_QR outcome, so a `scan_ref`
    always names a real, currently-scannable card."""
    limiter.check([(SCAN_RESOLVE_PER_STAFF, str(staff.id))])
    identity = resolve_card_identity(db, payload.card_token)
    scan_ref = issue_scan_ref(
        secret=get_settings().scan_ref_secret,
        staff_id=staff.id,
        session_id=session_id,
        card_token=payload.card_token,
        now=datetime.now(UTC),
    )
    return StaffScanResult(**identity.model_dump(), scan_ref=scan_ref)
