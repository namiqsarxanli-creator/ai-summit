"""HTTP surface of people matching, meetings and connections (migration 0021).

Every attendee route acts for the session's own person; the other person is
named by id, and a block is never revealed (an unknown and a blocked person get
the same answer). Mutations are fenced and rate-limited.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.deps import CurrentAdmin, CurrentAttendee, SessionToken
from app.core.ratelimit import RateLimit, limiter
from app.db.session import DbSession, mutating_transaction
from app.modules.admin import service as admin
from app.modules.admin.routers import _read
from app.modules.admin.service import AdminResult, required
from app.modules.attendees.models import User
from app.modules.attendees.profile import lock_attendee
from app.modules.masterclasses.models import MasterclassEvent
from app.modules.people import connections, meetings
from app.modules.people.compatibility import suggestions
from app.modules.people.models import MatchTable
from app.modules.points.service import maintenance_fast_path_check
from app.modules.staff.models import StaffAccount

router = APIRouter(tags=["people"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])
RowId = Annotated[int, Path(ge=1, le=2**53)]
PEOPLE_PER_ATTENDEE = RateLimit("people:attendee", 60, 600)

AUDIT_TABLE_CREATE = "match_table.create"
AUDIT_TABLE_UPDATE = "match_table.update"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequestCreate(Strict):
    recipient_id: int = Field(ge=1, le=2**53)
    command_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$")


class Respond(Strict):
    accept: bool


class BlockCreate(Strict):
    user_id: int = Field(ge=1, le=2**53)


class ScanCreate(Strict):
    card_token: str = Field(min_length=20, max_length=128)


class TableWrite(Strict):
    event_id: int = Field(ge=1, le=2**53)
    label: str = Field(min_length=1, max_length=40)


class TableEdit(Strict):
    label: str | None = Field(default=None, min_length=1, max_length=40)
    is_active: bool | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _write(db: DbSession, token: str | None, run: Any) -> dict[str, Any]:
    maintenance_fast_path_check(db)
    try:
        with mutating_transaction(db) as tx:
            user = lock_attendee(tx, token)
            limiter.check([(PEOPLE_PER_ATTENDEE, str(user.id))])
            result: dict[str, Any] = run(tx, user)
            return result
    except IntegrityError as err:  # a race the checks lost: one defined answer
        raise meetings.PeopleError("changed_meanwhile") from err


def _initial(user: User) -> str:
    return f"{user.first_name} {user.last_name[:1]}."


@router.get("/attendees/me/people")
def people(user: CurrentAttendee, db: DbSession, response: Response) -> dict[str, Any]:
    """Up to 5 compatible people (score 30+), best first, with the reasons. No
    contact data: that opens only after the mutual QR scan."""
    response.headers["Cache-Control"] = "no-store"
    return {
        "people": [
            {
                "user_id": other.id,
                "name": _initial(other),
                "profession": other.profession,
                "score": match.score,
                "shared_topics": list(match.shared_topics),
                "complement": list(match.complement) if match.complement else None,
                "shared_masterclasses": match.shared_masterclasses,
                "same_sector": match.same_sector,
                "reasons": list(match.reasons),
            }
            for other, match in suggestions(db, user)
        ]
    }


@router.get("/attendees/me/meetings")
def my_meetings(user: CurrentAttendee, db: DbSession, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return meetings.mine(db, user)


@router.post("/attendees/me/meeting-requests")
def send_request(payload: RequestCreate, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(
        db,
        token,
        lambda tx, u: meetings.send(tx, u, payload.recipient_id, payload.command_id, _now()),
    )


@router.post("/attendees/me/meeting-requests/{request_id}/respond")
def respond(
    request_id: RowId, payload: Respond, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _write(
        db, token, lambda tx, u: meetings.respond(tx, u, request_id, payload.accept, _now())
    )


@router.post("/attendees/me/meeting-requests/{request_id}/cancel")
def cancel_request(request_id: RowId, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, u: meetings.cancel(tx, u, request_id, _now()))


@router.post("/attendees/me/blocks")
def block(payload: BlockCreate, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, u: meetings.block(tx, u, payload.user_id, _now()))


@router.post("/attendees/me/connections/scan")
def scan(payload: ScanCreate, token: SessionToken, db: DbSession) -> dict[str, Any]:
    """Scan the other person's QR; when both have scanned, you are connected."""
    return _write(db, token, lambda tx, u: connections.scan(tx, u, payload.card_token, _now()))


@router.get("/attendees/me/connections")
def my_connections(user: CurrentAttendee, db: DbSession, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {"connections": connections.my_connections(db, user)}


# --- admin: Match Corner tables -----------------------------------------------------------


def table_view(row: MatchTable) -> dict[str, Any]:
    return {"id": row.id, "event_id": row.event_id, "label": row.label, "is_active": row.is_active}


def _create_table(tx: Any, actor: StaffAccount, payload: TableWrite) -> AdminResult:
    required(tx, MasterclassEvent, payload.event_id)
    row = MatchTable(event_id=payload.event_id, label=payload.label)
    tx.add(row)
    tx.flush()
    view = table_view(row)
    return AUDIT_TABLE_CREATE, "match_table", None, view, view


def _edit_table(tx: Any, actor: StaffAccount, table_id: int, payload: TableEdit) -> AdminResult:
    row = required(tx, MatchTable, table_id)
    before = table_view(row)
    if payload.label is not None:
        row.label = payload.label
    if payload.is_active is not None:
        row.is_active = payload.is_active  # a deactivated table keeps its booked meetings
    tx.flush()
    after = table_view(row)
    return AUDIT_TABLE_UPDATE, "match_table", before, after, after


def _admin_write(db: DbSession, token: str | None, op: admin.AdminOperation) -> dict[str, Any]:
    maintenance_fast_path_check(db)
    try:
        with mutating_transaction(db) as tx:
            return admin.execute(tx, token, op)
    except IntegrityError as err:
        raise admin.AdminConflict("A table with that label already exists.") from err


@admin_router.get("/match-tables")
def list_tables(admin_user: CurrentAdmin, db: DbSession) -> list[dict[str, Any]]:
    _read(admin_user)
    return [table_view(t) for t in db.execute(select(MatchTable).order_by(MatchTable.id)).scalars()]


@admin_router.post("/match-tables", status_code=201)
def create_table(payload: TableWrite, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _admin_write(db, token, lambda tx, actor: _create_table(tx, actor, payload))


@admin_router.patch("/match-tables/{table_id}")
def edit_table(
    table_id: RowId, payload: TableEdit, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _admin_write(db, token, lambda tx, actor: _edit_table(tx, actor, table_id, payload))
