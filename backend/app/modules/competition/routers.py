"""HTTP surface of verified participation and the competition (migration 0020).

Attendee (the session's own person; no request names anyone):
* POST /api/attendees/me/panels/scan        - a panel's screen QR (entry / exit)
* GET  /api/attendees/me/competition         - own points per category, Founder
* GET  /api/attendees/me/programme           - own chronological programme

Staff (each checked against a server-owned scope or booth INSIDE the fence):
* GET  /api/staff/me/scopes
* GET  /api/staff/organiser                 - the FACEMARK organiser workspace:
                                              administrator or GAME_MASTER only
* POST /api/staff/masterclasses/{id}/door    - MASTERCLASS_DOOR volunteer
* POST /api/staff/playground/slots/{id}/complete - that booth's staff
* POST /api/staff/qmeter/identify            - GAME_MASTER
* POST /api/staff/qmeter/award               - GAME_MASTER

Admin (excluded from the production schema, audited):
* panels, their screen codes and rotation; staff scopes; competition
  eligibility of a person.

Every scan is idempotent by state (one row per unit, one ledger entry per unit),
so a double tap, a retry or a lost response pays once.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Any, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.deps import CurrentAdmin, CurrentAttendee, CurrentStaff, SessionToken
from app.core.ratelimit import RateLimit, limiter
from app.core.sessions import resolve_staff_session
from app.db.session import DbSession, mutating_transaction
from app.modules.admin import service as admin
from app.modules.admin.routers import _read
from app.modules.admin.schemas import PointsLogPage, PointsLogQuery
from app.modules.admin.service import AdminInvalid, AdminResult, required
from app.modules.attendees.models import User
from app.modules.attendees.profile import lock_attendee
from app.modules.booths.models import Booth
from app.modules.competition import attendance, manual, organiser, panels
from app.modules.competition.models import SCOPES, Panel, StaffScope
from app.modules.competition.rules import CATEGORIES
from app.modules.masterclasses.models import Masterclass, MasterclassEvent
from app.modules.mentors.models import MentorSession
from app.modules.points import reversal
from app.modules.points.models import PointsLog, PointsSource
from app.modules.points.schemas import ReversalRequest, ReversalResponse
from app.modules.points.service import maintenance_fast_path_check
from app.modules.realtime.service import publish_award_signals
from app.modules.staff.models import StaffAccount

router = APIRouter(tags=["competition"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])

RowId = Annotated[int, Path(ge=1, le=2**53)]
SCAN_PER_ATTENDEE = RateLimit("panel_scan:attendee", 30, 600)
# One login is shared by FACEMARK's 20-30 door and booth volunteers (7 Nov 2026), so
# the limit is per crowd, not per person: a masterclass entry alone is ~250 scans in
# 10 minutes. It still stops a stolen login from scanning without end.
SCAN_PER_STAFF = RateLimit("attendance_scan:staff", 3000, 600)

AUDIT_PANEL_CREATE = "panel.create"
AUDIT_PANEL_UPDATE = "panel.update"
AUDIT_SCOPE_GRANT = "staff_scope.grant"
AUDIT_SCOPE_REVOKE = "staff_scope.revoke"
AUDIT_ELIGIBILITY = "competition.eligibility"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PanelScanRequest(Strict):
    code: str = Field(min_length=6, max_length=120)  # the QR text, or six typed digits


class DoorScanRequest(Strict):
    card_token: str = Field(min_length=20, max_length=128)
    phase: Literal["ENTRY", "EXIT"]


class CompleteRequest(Strict):
    card_token: str = Field(min_length=20, max_length=128)


class IdentifyRequest(Strict):
    full_name: str = Field(min_length=2, max_length=200)
    phone: str = Field(min_length=7, max_length=40)


class QmeterRequest(Strict):
    user_id: int = Field(ge=1, le=2**53)
    panel_id: int = Field(ge=1, le=2**53)
    reason: str = Field(min_length=3, max_length=300)


def _now() -> datetime:
    return datetime.now(UTC)


def _staff_write(db: DbSession, token: str | None, run: Any) -> dict[str, Any]:
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        staff = resolve_staff_session(tx, token)  # inside the fence: revocation applies
        limiter.check([(SCAN_PER_STAFF, str(staff.id))])
        result: dict[str, Any] = run(tx, staff)
        return result


# --- attendee --------------------------------------------------------------------------


@router.post("/attendees/me/panels/scan")
def scan_panel(payload: PanelScanRequest, token: SessionToken, db: DbSession) -> dict[str, Any]:
    """Scan the code on the main screen. Entry in the first 15 minutes, exit in the
    last 10; both earn the panel. Repeating a scan changes nothing."""
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        user = lock_attendee(tx, token)
        limiter.check([(SCAN_PER_ATTENDEE, str(user.id))])
        return panels.scan(tx, user, payload.code, _now())


@router.get("/panels")
def list_public_panels(db: DbSession) -> list[dict[str, Any]]:
    """Main Stage panels and their times - never their screen codes."""
    rows = db.execute(
        select(Panel, MasterclassEvent.timezone)
        .join(MasterclassEvent, MasterclassEvent.id == Panel.event_id)
        .order_by(Panel.starts_at)
    ).all()
    return [
        {
            "id": p.id,
            "title": p.title,
            "kind": "grand_final" if panels.is_grand_final(p) else "panel",
            "starts_at": p.starts_at,
            "ends_at": p.ends_at,
            "timezone": zone,
        }
        for p, zone in rows
    ]


@router.get("/attendees/me/competition")
def my_competition(user: CurrentAttendee, db: DbSession, response: Response) -> dict[str, Any]:
    """Own points per category against each category's own maximum."""
    response.headers["Cache-Control"] = "no-store"
    totals = dict(
        db.execute(
            select(PointsLog.source, func.coalesce(func.sum(PointsLog.points), 0))
            .where(PointsLog.user_id == user.id, PointsLog.source.in_(tuple(CATEGORIES)))
            .group_by(PointsLog.source)
        ).all()
    )
    categories = [
        {"category": c.source.value, "points": int(totals.get(c.source, 0)), "maximum": c.maximum}
        for c in CATEGORIES.values()
    ]
    from app.modules.competition import final

    return {
        "eligible": user.competition_excluded_reason is None,
        "total": sum(c["points"] for c in categories),
        "maximum": 100,
        "categories": categories,
        # The four zones of Beyond Now Founder and the Lucky Draw eligibility.
        **final.founder_status(db, user.id),
        # The published final result, once a Game Master has published one.
        "final": final.my_final(db, user.id),
        "final_status": final.public(db, _now())["status"],
    }


@router.get("/attendees/me/programme")
def my_programme(user: CurrentAttendee, db: DbSession, response: Response) -> dict[str, Any]:
    """The person's own day in time order: masterclasses, Playground, Match Corner
    meetings and mentor groups, each with its real status (competition/programme.py)."""
    from app.modules.competition.programme import programme

    response.headers["Cache-Control"] = "no-store"
    return programme(db, user.id)


# --- staff -----------------------------------------------------------------------------


@router.get("/staff/me/scopes")
def my_scopes(staff: CurrentStaff, db: DbSession, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    rows = db.execute(
        select(StaffScope.scope, StaffScope.target_id).where(
            StaffScope.staff_account_id == staff.id, StaffScope.revoked_at.is_(None)
        )
    ).all()
    return {"scopes": [{"scope": s, "target_id": t} for s, t in rows]}


@router.get("/staff/organiser")
def organiser_workspace(staff: CurrentStaff, db: DbSession, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return organiser.workspace(db, staff)


# --- points corrections in the organiser panel: an administrator or a Game Master ---------


@router.get("/staff/id-reference")
def organiser_id_reference(
    token: SessionToken, db: DbSession, response: Response
) -> dict[str, list[dict[str, Any]]]:
    """Which booth and which staff account each number in the points list is:
    names only, no contact data. An administrator or a Game Master."""
    organiser.authenticate_organiser(db, token)
    response.headers["Cache-Control"] = "no-store"
    booths = db.execute(select(Booth.id, Booth.name).order_by(Booth.id)).all()
    staff = db.execute(
        select(StaffAccount.id, StaffAccount.full_name).order_by(StaffAccount.id)
    ).all()
    return {
        "booths": [{"id": i, "name": n} for i, n in booths],
        "staff": [{"id": i, "name": n} for i, n in staff],
    }


@router.get("/staff/points-log", response_model=PointsLogPage)
def organiser_points_log(
    token: SessionToken,
    db: DbSession,
    response: Response,
    query: Annotated[PointsLogQuery, Query()],
) -> PointsLogPage:
    """The ledger as the admin console lists it (read-only), for the organiser."""
    organiser.authenticate_organiser(db, token)
    response.headers["Cache-Control"] = "no-store"
    return admin.points_page(db, query)


class ManualPoints(Strict):
    user_id: int = Field(ge=1, le=2**53)
    source: Literal["PROFILE", "PANEL", "QMETER", "MASTERCLASS", "PLAYGROUND", "CONNECTION"]
    points: int = Field(ge=1, le=24)
    reason: str = Field(min_length=3, max_length=280)
    command_id: UUID


@router.post("/staff/points-log/award")
def organiser_add_points(
    payload: ManualPoints, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    """Adds points for a scan that was missed (competition/manual.py): an
    administrator or a Game Master, with a reason; a retry adds nothing."""
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        staff = organiser.authenticate_organiser(tx, token)
        body, result = manual.add_points(
            tx,
            staff,
            user_id=payload.user_id,
            source=PointsSource(payload.source),
            points=payload.points,
            reason=payload.reason.strip(),
            command_id=str(payload.command_id),
            now=_now(),
        )
    if result.paid:
        publish_award_signals(payload.user_id)
    return body


@router.post("/staff/points-log/{entry_id}/reverse", response_model=ReversalResponse)
def organiser_reverse_points_entry(
    entry_id: Annotated[int, Path(ge=1, le=2**53)],
    payload: ReversalRequest,
    token: SessionToken,
    db: DbSession,
) -> ReversalResponse:
    """Cancels ONE award in full with a reason - exactly the admin console's
    reversal (points/reversal.py: idempotent, audited, the original kept), made
    by an administrator or a Game Master."""
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        outcome = reversal.reverse_entry(
            tx,
            raw_session_token=token,
            entry_id=entry_id,
            request=payload,
            now=datetime.now(UTC),
            authenticate=organiser.authenticate_organiser,
        )
    if outcome.reversed_user_id is not None:  # never for a replay
        publish_award_signals(outcome.reversed_user_id)
    return outcome.response


@router.post("/staff/masterclasses/{masterclass_id}/door")
def door(
    masterclass_id: RowId, payload: DoorScanRequest, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _staff_write(
        db,
        token,
        lambda tx, staff: attendance.door_scan(
            tx,
            staff,
            masterclass_id=masterclass_id,
            card_token=payload.card_token,
            phase=payload.phase,
            now=_now(),
        ),
    )


@router.post("/staff/playground/slots/{slot_id}/complete")
def complete(
    slot_id: RowId, payload: CompleteRequest, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _staff_write(
        db,
        token,
        lambda tx, staff: attendance.playground_complete(
            tx, staff, slot_id=slot_id, card_token=payload.card_token, now=_now()
        ),
    )


@router.post("/staff/qmeter/identify")
def qmeter_identify(payload: IdentifyRequest, token: SessionToken, db: DbSession) -> dict[str, Any]:
    """The one attendee matching BOTH the name and the phone (masked back)."""

    def run(tx: Any, staff: StaffAccount) -> dict[str, Any]:
        user = panels.identify(tx, staff, payload.full_name, payload.phone)
        return {
            "user_id": user.id,
            "name": f"{user.first_name} {user.last_name}",
            "phone": panels.masked(user.phone),
        }

    return _staff_write(db, token, run)


@router.post("/staff/qmeter/award")
def qmeter_award(payload: QmeterRequest, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _staff_write(
        db,
        token,
        lambda tx, staff: panels.record_qmeter(
            tx,
            staff,
            user_id=payload.user_id,
            panel_id=payload.panel_id,
            reason=payload.reason,
            now=_now(),
        ),
    )


# --- admin ------------------------------------------------------------------------------


class PanelWrite(Strict):
    event_id: int = Field(ge=1, le=2**53)
    code: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    title: str = Field(min_length=1, max_length=200)
    starts_at: datetime
    ends_at: datetime

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("a time zone offset is required")
        return v


class PanelEdit(Strict):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    rotate_qr: bool = False


class ScopeWrite(Strict):
    staff_account_id: int = Field(ge=1, le=2**53)
    scope: Literal["MASTERCLASS_DOOR", "GAME_MASTER", "MENTOR_HOST"]
    target_id: int | None = Field(default=None, ge=1, le=2**31 - 1)


class EligibilityWrite(Strict):
    # NULL = takes part; otherwise why not (organiser team, volunteer, sponsor...).
    excluded_reason: str | None = Field(default=None, min_length=3, max_length=200)


def panel_view(row: Panel) -> dict[str, Any]:
    return {
        "id": row.id,
        "event_id": row.event_id,
        "code": row.code,
        "title": row.title,
        "starts_at": row.starts_at.isoformat(),
        "ends_at": row.ends_at.isoformat(),
        "qr_version": row.qr_version,
    }


def _admin_write(db: DbSession, token: str | None, op: admin.AdminOperation) -> dict[str, Any]:
    maintenance_fast_path_check(db)
    try:
        with mutating_transaction(db) as tx:
            return admin.execute(tx, token, op)
    except IntegrityError as err:  # a duplicate panel code or a live scope twice
        raise admin.AdminConflict("A record with that value already exists.") from err


def _create_panel(tx: Any, actor: StaffAccount, payload: PanelWrite) -> AdminResult:
    required(tx, MasterclassEvent, payload.event_id)
    if payload.ends_at <= payload.starts_at:
        raise AdminInvalid("The panel must end after it starts.")
    row = Panel(**payload.model_dump())
    tx.add(row)
    tx.flush()
    view = panel_view(row)
    return AUDIT_PANEL_CREATE, "panel", None, view, view


def _edit_panel(tx: Any, actor: StaffAccount, panel_id: int, payload: PanelEdit) -> AdminResult:
    row = required(tx, Panel, panel_id)
    before = panel_view(row)
    if payload.title is not None:
        row.title = payload.title
    if payload.rotate_qr:  # every code shown before stops working
        row.qr_version += 1
    tx.flush()
    after = panel_view(row)
    return AUDIT_PANEL_UPDATE, "panel", before, after, after


def scope_view(row: StaffScope) -> dict[str, Any]:
    return {
        "id": row.id,
        "staff_account_id": row.staff_account_id,
        "scope": row.scope,
        "target_id": row.target_id,
        "revoked": row.revoked_at is not None,
    }


def _grant(tx: Any, actor: StaffAccount, payload: ScopeWrite) -> AdminResult:
    if (payload.scope == "GAME_MASTER") != (payload.target_id is None):
        raise AdminInvalid("Game Master is event-wide; the other scopes need a target.")
    required(tx, StaffAccount, payload.staff_account_id)
    # A scope names a real target: the masterclass a door volunteer works, the
    # mentor session a host runs. A typo must not leave a scope that matches
    # nothing today and something else after a later insert.
    if payload.scope == "MASTERCLASS_DOOR":
        required(tx, Masterclass, cast(int, payload.target_id))
    elif payload.scope == "MENTOR_HOST":
        required(tx, MentorSession, cast(int, payload.target_id))
    row = StaffScope(
        staff_account_id=payload.staff_account_id,
        scope=payload.scope,
        target_id=payload.target_id,
        granted_by_staff_id=actor.id,
    )
    tx.add(row)
    tx.flush()
    view = scope_view(row)
    return AUDIT_SCOPE_GRANT, "staff_scope", None, view, view


def _revoke(tx: Any, actor: StaffAccount, scope_id: int) -> AdminResult:
    row = required(tx, StaffScope, scope_id)
    before = scope_view(row)
    if row.revoked_at is None:
        row.revoked_at = _now()
    tx.flush()
    after = scope_view(row)
    return AUDIT_SCOPE_REVOKE, "staff_scope", before, after, after


def _eligibility(
    tx: Any, actor: StaffAccount, user_id: int, payload: EligibilityWrite
) -> AdminResult:
    user = required(tx, User, user_id)
    before = {"id": user.id, "excluded": user.competition_excluded_reason is not None}
    user.competition_excluded_reason = payload.excluded_reason
    tx.flush()
    after = {
        "id": user.id,
        "excluded": user.competition_excluded_reason is not None,
        "reason": payload.excluded_reason,
    }
    return AUDIT_ELIGIBILITY, "user", before, after, after


@admin_router.get("/readiness")
def readiness_report(admin_user: CurrentAdmin, db: DbSession, response: Response) -> dict[str, Any]:
    """READ-ONLY: the event-readiness checklist of `competition-report`
    (competition/readiness.py) - counts, times and row ids, no personal data."""
    from app.modules.competition.readiness import check, ready

    _read(admin_user)
    response.headers["Cache-Control"] = "no-store"
    checks = check(db)
    return {
        "ready": ready(checks),
        "checks": [{"key": c.key, "status": c.status, "detail": c.detail} for c in checks],
    }


@admin_router.get("/panels")
def list_panels(admin_user: CurrentAdmin, db: DbSession) -> list[dict[str, Any]]:
    _read(admin_user)
    return [panel_view(p) for p in db.execute(select(Panel).order_by(Panel.starts_at)).scalars()]


def _screen_until(event: MasterclassEvent) -> datetime:
    """A screen link works until midnight after the event day (event time)."""
    return datetime.combine(event.event_date + timedelta(days=1), time(0), ZoneInfo(event.timezone))


@router.get("/staff/panel-screen-links")
def panel_screen_links(
    token: SessionToken, db: DbSession, response: Response
) -> list[dict[str, Any]]:
    """For the hall computer: one link per panel that shows its codes with no
    sign-in (panels.py, SCREEN LINK). An administrator or a Game Master: FACEMARK
    runs the screens. Never cached."""
    organiser.authenticate_organiser(db, token)
    response.headers["Cache-Control"] = "no-store"
    rows = db.execute(
        select(Panel, MasterclassEvent)
        .join(MasterclassEvent, MasterclassEvent.id == Panel.event_id)
        .order_by(Panel.starts_at)
    ).all()
    return [
        {
            "id": p.id,
            "title": p.title,
            "kind": "grand_final" if panels.is_grand_final(p) else "panel",
            "starts_at": p.starts_at.isoformat(),
            "timezone": e.timezone,
            "key": panels.screen_key(p, _screen_until(e)),
            "expires_at": _screen_until(e).isoformat(),
        }
        for p, e in rows
    ]


@router.get("/screen/panel")
def public_panel_screen(
    db: DbSession, response: Response, k: Annotated[str, Query(min_length=10, max_length=120)]
) -> dict[str, Any]:
    """The hall computer's view of one panel's codes, opened by its screen link."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex"
    panel = panels.read_screen_key(db, k, _now())
    return _screen_payload(panel, required(db, MasterclassEvent, panel.event_id))


@admin_router.get("/panels/{panel_id}/screen")
def panel_screen(
    panel_id: RowId, admin_user: CurrentAdmin, db: DbSession, response: Response
) -> dict[str, Any]:
    """The two codes for the main screen (administrators only, never cached)."""
    _read(admin_user)
    response.headers["Cache-Control"] = "no-store"
    panel = required(db, Panel, panel_id)
    return _screen_payload(panel, required(db, MasterclassEvent, panel.event_id))


def _screen_payload(panel: Panel, event: MasterclassEvent) -> dict[str, Any]:
    return {
        **panel_view(panel),
        "timezone": event.timezone,
        "kind": "grand_final" if panels.is_grand_final(panel) else "panel",
        "entry_code": panels.screen_code(panel, "ENTRY"),
        "exit_code": panels.screen_code(panel, "EXIT"),
        "entry_short": panels.short_code(panel, "ENTRY"),
        "exit_short": panels.short_code(panel, "EXIT"),
    }


@admin_router.post("/panels", status_code=201)
def create_panel(payload: PanelWrite, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _admin_write(db, token, lambda tx, actor: _create_panel(tx, actor, payload))


@admin_router.patch("/panels/{panel_id}")
def edit_panel(
    panel_id: RowId, payload: PanelEdit, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _admin_write(db, token, lambda tx, actor: _edit_panel(tx, actor, panel_id, payload))


@admin_router.get("/staff-scopes")
def list_scopes(admin_user: CurrentAdmin, db: DbSession) -> list[dict[str, Any]]:
    _read(admin_user)
    return [scope_view(s) for s in db.execute(select(StaffScope).order_by(StaffScope.id)).scalars()]


@admin_router.post("/staff-scopes", status_code=201)
def grant_scope(payload: ScopeWrite, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _admin_write(db, token, lambda tx, actor: _grant(tx, actor, payload))


@admin_router.post("/staff-scopes/{scope_id}/revoke")
def revoke_scope(scope_id: RowId, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _admin_write(db, token, lambda tx, actor: _revoke(tx, actor, scope_id))


@admin_router.patch("/users/{user_id}/competition-eligibility")
def set_eligibility(
    user_id: RowId, payload: EligibilityWrite, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _admin_write(db, token, lambda tx, actor: _eligibility(tx, actor, user_id, payload))


assert set(SCOPES) == {"MASTERCLASS_DOOR", "GAME_MASTER", "MENTOR_HOST"}
