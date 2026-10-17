"""HTTP surface of the final result (migration 0023).

Public (privacy-safe; the big screen and every phone):
* GET  /api/final                                   - open / pending_review / published

Game Master (GAME_MASTER scope) or administrator, re-checked inside the fence:
* GET  /api/staff/final                             - versions and the close
* POST /api/staff/final                             - take a DRAFT (after the close)
* GET  /api/staff/final/{id}                        - review a snapshot
* POST /api/staff/final/{id}/publish                - publish (supersedes the previous)
* POST /api/staff/final/{id}/discard                - discard a DRAFT
* GET  /api/staff/final/{id}/founders.csv           - the Lucky Draw eligibility list

After the event (competition/report.py):
* GET  /api/attendees/me/report                     - one's own report
* GET  /api/staff/booth/report                      - a booth's own stats and consenting leads
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Response

from app.core.deps import CurrentAttendee, CurrentStaff, SessionToken
from app.core.sessions import resolve_staff_session
from app.db.session import DbSession, mutating_transaction
from app.modules.competition import final, report
from app.modules.points.service import maintenance_fast_path_check

router = APIRouter(tags=["competition"])
RowId = Annotated[int, Path(ge=1, le=2**53)]


def _now() -> datetime:
    return datetime.now(UTC)


@router.get("/final")
def public_final(db: DbSession, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return final.public(db, _now())


@router.get("/staff/final")
def versions(staff: CurrentStaff, db: DbSession, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return final.versions(db, staff)


@router.get("/staff/final/{result_id}")
def detail(
    result_id: RowId, staff: CurrentStaff, db: DbSession, response: Response
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return final.detail(db, staff, result_id)


def _write(db: DbSession, token: str | None, run: Any) -> dict[str, Any]:
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        staff = resolve_staff_session(tx, token)  # inside the fence: revocation applies
        result: dict[str, Any] = run(tx, staff)
        return result


@router.post("/staff/final", status_code=201)
def create(token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, staff: final.create(tx, staff, _now()))


@router.post("/staff/final/{result_id}/publish")
def publish(result_id: RowId, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, staff: final.publish(tx, staff, result_id, _now()))


@router.post("/staff/final/{result_id}/discard")
def discard(result_id: RowId, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, staff: final.discard(tx, staff, result_id, _now()))


@router.get("/staff/final/{result_id}/founders.csv")
def founders_csv(result_id: RowId, token: SessionToken, db: DbSession) -> Response:
    # A GET that writes an audit row, like the existing CSV exports: fenced.
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        staff = resolve_staff_session(tx, token)
        body = final.founders_csv(tx, staff, result_id)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="lucky-draw-{result_id}.csv"',
        },
    )


@router.get("/attendees/me/report")
def my_report(user: CurrentAttendee, db: DbSession, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return report.attendee_report(db, user)


@router.get("/staff/booth/report")
def booth_report(token: SessionToken, db: DbSession, response: Response) -> dict[str, Any]:
    # Contact details leave the server here: fenced and audited, like an export.
    response.headers["Cache-Control"] = "no-store"
    return _write(db, token, lambda tx, staff: report.booth_report(tx, staff))
