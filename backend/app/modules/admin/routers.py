"""Minimal admin API. Mutations resolve authority only after the reset fence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.deps import CurrentAdmin, SessionToken
from app.core.ratelimit import limiter
from app.db.session import DbSession, mutating_transaction
from app.modules.admin import audit, exports
from app.modules.admin import service as admin
from app.modules.admin.models import EventConfig
from app.modules.admin.schemas import (
    AttendeeFilters,
    AttendeeListQuery,
    AttendeePage,
    AuditListQuery,
    AuditPage,
    BoothEdit,
    BoothWrite,
    ConfigEdit,
    ExportPrepared,
    ExportStatus,
    InterestEdit,
    InterestWrite,
    OtpDeliveryPage,
    OtpDeliveryStatusName,
    PointsFilters,
    PointsLogPage,
    PointsLogQuery,
    PresetEdit,
    PresetWrite,
    SponsorExportRequest,
    StaffCreate,
    TaskEdit,
    TaskWrite,
    WeightWrite,
)
from app.modules.auth import otp_outbox
from app.modules.auth.otp_delivery import recent_demo_otps
from app.modules.booths.models import TaskPresetPoint
from app.modules.interests.models import Interest
from app.modules.points import reversal
from app.modules.points.schemas import ReversalRequest, ReversalResponse
from app.modules.points.service import maintenance_fast_path_check
from app.modules.realtime.service import publish_award_signals
from app.modules.staff import password as staff_password
from app.modules.staff.models import StaffAccount
from app.modules.staff.schemas import StaffPasswordResetStart

router = APIRouter(prefix="/admin", tags=["admin"])


def _write(db: Session, token: str | None, operation: admin.AdminOperation) -> dict[str, Any]:
    maintenance_fast_path_check(db)
    try:
        with mutating_transaction(db) as tx:
            return admin.execute(tx, token, operation)
    except IntegrityError as err:
        sqlstate = getattr(err.orig, "sqlstate", None)
        if sqlstate == "23505":
            raise admin.AdminConflict("A record with that value already exists.") from err
        if sqlstate in {"23503", "23514", "23502"}:
            raise admin.AdminInvalid("That change violates a data constraint.") from err
        raise


def _read(admin_user: StaffAccount) -> None:
    from app.core.ratelimit import limiter
    from app.modules.admin.service import ADMIN_PER_ACCOUNT

    limiter.check([(ADMIN_PER_ACCOUNT, str(admin_user.id))])


@router.get("/attendees", response_model=AttendeePage)
def attendees(
    admin_user: CurrentAdmin,
    db: DbSession,
    response: Response,
    query: Annotated[AttendeeListQuery, Query()],
) -> AttendeePage:
    """Attendees, newest registration first, keyset-paginated (`cursor` = the
    previous page's `next_cursor`; 50 per page, at most 100). Search `q` and the
    status / sponsor-consent / registration-date filters combine with AND; dates
    are timezone-aware with `from` inclusive and `to` exclusive. Personal data:
    never cached, administrators only."""
    _read(admin_user)
    response.headers["Cache-Control"] = "no-store"
    return admin.attendee_page(db, query)


@router.get("/booths")
def booths(admin_user: CurrentAdmin, db: DbSession) -> list[dict[str, Any]]:
    _read(admin_user)
    return admin.booth_rows(db)


@router.get("/interests")
def interests(admin_user: CurrentAdmin, db: DbSession) -> list[dict[str, Any]]:
    _read(admin_user)
    return [
        admin.interest_view(r) for r in db.execute(select(Interest).order_by(Interest.id)).scalars()
    ]


@router.get("/staff")
def staff(admin_user: CurrentAdmin, db: DbSession) -> list[dict[str, Any]]:
    _read(admin_user)
    return [
        admin.staff_view(r)
        for r in db.execute(select(StaffAccount).order_by(StaffAccount.id)).scalars()
    ]


@router.get("/tasks")
def tasks(admin_user: CurrentAdmin, db: DbSession) -> list[dict[str, Any]]:
    _read(admin_user)
    return admin.task_rows(db)


@router.get("/presets")
def presets(admin_user: CurrentAdmin, db: DbSession) -> list[dict[str, Any]]:
    _read(admin_user)
    return [
        admin.preset_view(r)
        for r in db.execute(select(TaskPresetPoint).order_by(TaskPresetPoint.id)).scalars()
    ]


@router.get("/event-config")
def event_config(admin_user: CurrentAdmin, db: DbSession) -> dict[str, Any]:
    _read(admin_user)
    return admin.config_view(admin.required(db, EventConfig, 1))


@router.get("/points-log", response_model=PointsLogPage)
def points_log(
    admin_user: CurrentAdmin,
    db: DbSession,
    response: Response,
    query: Annotated[PointsLogQuery, Query()],
) -> PointsLogPage:
    """The points ledger, newest first, keyset-paginated like the attendee
    list. Filters: attendee, booth, activity, staff account, source,
    entry type and a half-open timezone-aware date range. Read-only."""
    _read(admin_user)
    response.headers["Cache-Control"] = "no-store"
    return admin.points_page(db, query)


@router.get("/audit", response_model=AuditPage)
def audit_log(
    admin_user: CurrentAdmin,
    db: DbSession,
    response: Response,
    query: Annotated[AuditListQuery, Query()],
) -> AuditPage:
    """The admin audit trail, newest first, keyset-paginated like the other admin
    lists. Filters: acting staff account, action, target type (+ id) and a
    half-open timezone-aware date range. Read-only: viewing the trail writes
    nothing (no audit row, no state). Only an allowlisted part of each event is
    returned, never the raw before/after JSON. Never cached, administrators only."""
    _read(admin_user)
    response.headers["Cache-Control"] = "no-store"
    return audit.audit_page(db, query)


@router.post("/points-log/{entry_id}/reverse", response_model=ReversalResponse)
def reverse_points_entry(
    entry_id: Annotated[int, Path(ge=1, le=2**53)],
    payload: ReversalRequest,
    token: SessionToken,
    db: DbSession,
) -> ReversalResponse:
    """Cancels ONE award in full (administrators only). The original entry is
    kept; a new negative REVERSAL entry is added and linked to it. The amount,
    attendee, booth, activity and source come from the original on the server -
    the request carries only the reason and an idempotency key (a retry or a
    double click replays the first answer). The reversal and its audit row commit
    together; the attendee's score and the leaderboard are refreshed after the
    commit. It does NOT make a once-only activity earnable again."""
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        outcome = reversal.reverse_entry(
            tx,
            raw_session_token=token,
            entry_id=entry_id,
            request=payload,
            now=datetime.now(UTC),
        )
    if outcome.reversed_user_id is not None:  # never for a replay: nothing new committed
        publish_award_signals(outcome.reversed_user_id)
    return outcome.response


@router.post("/staff/{staff_id}/password-reset", status_code=202)
def start_staff_password_reset(
    staff_id: Annotated[int, Path(ge=1, le=2**53)],
    payload: StaffPasswordResetStart,
    token: SessionToken,
    db: DbSession,
) -> dict[str, str]:
    """Makes the system email a one-time reset code to the staff member's OWN
    address. The administrator never sees the code and cannot set the new
    password: only the staff member can, with the code."""
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        staff_password.start_reset_for(
            tx, raw_session_token=token, staff_id=staff_id, language=payload.language
        )
    return {"status": "queued"}


# --- CSV exports --------------------------------------------------------------
# Two steps (see admin/export_tickets.py): POST prepares and returns a one-time
# ticket; GET /exports/download redeems it and streams the file. Authority is
# checked by `CurrentAdmin` before anything is read or streamed.


@router.post("/exports/attendees", response_model=ExportPrepared)
def prepare_attendee_export(
    admin_user: CurrentAdmin, db: DbSession, payload: AttendeeFilters
) -> ExportPrepared:
    """Prepare the internal attendee list as CSV: every attendee matching the
    same search and filters as `GET /attendees` (no cursor, no page size)."""
    _read(admin_user)
    return exports.prepare(db, actor=admin_user, kind="attendees", filters=payload)


@router.post("/exports/points-log", response_model=ExportPrepared)
def prepare_points_export(
    admin_user: CurrentAdmin, db: DbSession, payload: PointsFilters
) -> ExportPrepared:
    """Prepare the points ledger as CSV: every row matching the same filters as
    `GET /points-log`."""
    _read(admin_user)
    return exports.prepare(db, actor=admin_user, kind="points-log", filters=payload)


@router.post("/exports/sponsor-contacts", response_model=ExportPrepared)
def prepare_sponsor_export(
    admin_user: CurrentAdmin, db: DbSession, payload: SponsorExportRequest | None = None
) -> ExportPrepared:
    """Prepare the sponsor contact list. Takes no parameters: the server includes
    only ACTIVE attendees whose sponsor consent is true at the snapshot."""
    _read(admin_user)
    return exports.prepare(
        db, actor=admin_user, kind="sponsor-contacts", filters=payload or SponsorExportRequest()
    )


@router.get("/exports/download")
def download_export(
    admin_user: CurrentAdmin,
    db: DbSession,
    sessions: exports.ExportSessions,
    ticket: Annotated[str, Query(min_length=20, max_length=200)],
) -> StreamingResponse:
    """Redeem a one-time ticket and stream the CSV. Personal data: never cached,
    fixed file name, the exact row count and snapshot instant in headers."""
    _read(admin_user)
    stream = exports.open_download(db, sessions, actor=admin_user, token=ticket)
    return StreamingResponse(
        stream.chunks(), media_type="text/csv; charset=utf-8", headers=stream.headers
    )


@router.get("/exports/status/{ref}", response_model=ExportStatus)
def export_status(
    ref: Annotated[str, Path(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")],
    admin_user: CurrentAdmin,
    response: Response,
) -> ExportStatus:
    """What became of one export's download, as the server knows it: outcome and
    row counts only - never CSV content, filter values or the ticket. Only the
    administrator who prepared the export can read it (anyone else, and an
    unknown or forgotten `ref`, gets 404)."""
    limiter.check([(exports.EXPORT_STATUS_PER_ACCOUNT, str(admin_user.id))])
    response.headers["Cache-Control"] = "no-store"
    return ExportStatus.model_validate(vars(exports.export_status(actor=admin_user, ref=ref)))


@router.get("/demo-otps")
def demo_otps(admin_user: CurrentAdmin, response: Response) -> list[dict[str, str]]:
    _read(admin_user)
    if not get_settings().demo_mode:
        raise admin.AdminNotFound("Demo OTP visibility is unavailable.")
    response.headers["Cache-Control"] = "no-store"
    return recent_demo_otps()


@router.get("/otp-deliveries", response_model=OtpDeliveryPage)
def otp_deliveries(
    admin_user: CurrentAdmin,
    db: DbSession,
    response: Response,
    status: OtpDeliveryStatusName | None = None,
    email: Annotated[str | None, Query(max_length=320)] = None,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> OtpDeliveryPage:
    """Status of OTP emails, newest first, keyset-paginated (`before_id` =
    the previous page's `next_before_id`). Read-only; the code and its
    ciphertext are never selected, and addresses are masked."""
    _read(admin_user)
    response.headers["Cache-Control"] = "no-store"
    items, next_before_id = otp_outbox.list_deliveries(
        db, status=status, email=email, before_id=before_id, limit=limit
    )
    summary = otp_outbox.summarise(db)
    return OtpDeliveryPage.model_validate(
        {
            "items": [vars(i) for i in items],
            "next_before_id": next_before_id,
            "summary": vars(summary),
        }
    )


@router.post("/staff", status_code=201)
def create_staff(payload: StaffCreate, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.create_staff(tx, actor, payload))


@router.post("/staff/{staff_id}/deactivate")
def deactivate_staff(staff_id: int, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.deactivate_staff(tx, actor, staff_id))


@router.post("/booths", status_code=201)
def create_booth(payload: BoothWrite, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.create_booth(tx, actor, payload))


@router.patch("/booths/{booth_id}")
def edit_booth(
    booth_id: int, payload: BoothEdit, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.edit_booth(tx, actor, booth_id, payload))


@router.post("/interests", status_code=201)
def create_interest(payload: InterestWrite, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.create_interest(tx, actor, payload))


@router.patch("/interests/{interest_id}")
def edit_interest(
    interest_id: int, payload: InterestEdit, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.edit_interest(tx, actor, interest_id, payload))


@router.put("/booths/{booth_id}/weights/{interest_id}")
def set_weight(
    booth_id: int, interest_id: int, payload: WeightWrite, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _write(
        db, token, lambda tx, actor: admin.set_weight(tx, actor, booth_id, interest_id, payload)
    )


@router.delete("/booths/{booth_id}/weights/{interest_id}")
def remove_weight(
    booth_id: int, interest_id: int, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _write(
        db, token, lambda tx, actor: admin.remove_weight(tx, actor, booth_id, interest_id)
    )


@router.post("/tasks", status_code=201)
def create_task(payload: TaskWrite, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.create_task(tx, actor, payload))


@router.patch("/tasks/{task_id}")
def edit_task(
    task_id: int, payload: TaskEdit, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.edit_task(tx, actor, task_id, payload))


@router.post("/presets", status_code=201)
def create_preset(payload: PresetWrite, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.create_preset(tx, actor, payload))


@router.patch("/presets/{preset_id}")
def edit_preset(
    preset_id: int, payload: PresetEdit, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.edit_preset(tx, actor, preset_id, payload))


@router.patch("/event-config")
def edit_config(payload: ConfigEdit, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.edit_config(tx, actor, payload))
