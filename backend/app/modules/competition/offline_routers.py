"""HTTP surface of offline scans (migration 0026).

* POST /api/staff/offline-scans                 - a queued scan from a staff phone
* GET  /api/staff/offline-scans                 - the review queue (Game Master / admin)
* POST /api/staff/offline-scans/{id}/approve    - apply it as of the reviewed time
* POST /api/staff/offline-scans/{id}/reject
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Path, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.deps import CurrentStaff, SessionToken
from app.core.errors import ClientUpdateRequiredError
from app.core.ratelimit import RateLimit, limiter
from app.core.schedule import lock_schedule
from app.core.sessions import resolve_staff_session
from app.db.session import DbSession, mutating_transaction
from app.modules.competition import offline
from app.modules.points.service import maintenance_fast_path_check

router = APIRouter(tags=["competition"])
RowId = Annotated[int, Path(ge=1, le=2**53)]
SUBMIT_PER_STAFF = RateLimit("offline_scan:staff", 600, 600)


def _now() -> datetime:
    return datetime.now(UTC)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Submission(Strict):
    client_scan_id: UUID
    kind: Literal["MASTERCLASS_DOOR", "PLAYGROUND_COMPLETE", "MENTOR_COMPLETE"]
    target_id: int = Field(ge=1, le=2**31 - 1)
    phase: Literal["ENTRY", "EXIT"] | None = None
    card_token: str = Field(min_length=20, max_length=128)
    scanned_at: datetime
    # What the phone filed the scan under (GET /api/staff/me): checked against the
    # session, so a queue is never replayed as another account or another dataset.
    # Optional in the schema ONLY so that a page of the first queue version (which
    # sends neither) is answered with a retryable CLIENT_UPDATE_REQUIRED instead of
    # a 422 it would treat as final and discard the scan for. Both are required.
    queue_owner: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    dataset_generation: int | None = Field(default=None, ge=1, le=2**62)

    @field_validator("scanned_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("a time zone offset is required")
        return v


class Review(Strict):
    reason: str = Field(min_length=3, max_length=300)


class Approval(Review):
    # The time the reviewer VERIFIED (e.g. from the host's list), with its offset.
    # The phone's claimed time stays on the row beside it; it is not the evidence.
    verified_at: datetime
    # Only for rows submitted before migration 0027 (no card evidence): the
    # attendee's current card, re-scanned by the reviewer.
    card_token: str | None = Field(default=None, min_length=20, max_length=128)

    @field_validator("verified_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("a time zone offset is required")
        return v


def _write(db: DbSession, token: str | None, run: Any) -> dict[str, Any]:
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        lock_schedule(tx)  # the scans change seats and bookings
        staff = resolve_staff_session(tx, token)  # inside the fence: revocation applies
        result: dict[str, Any] = run(tx, staff)
        return result


@router.post("/staff/offline-scans")
def submit(payload: Submission, token: SessionToken, db: DbSession) -> dict[str, Any]:
    owner, generation = payload.queue_owner, payload.dataset_generation
    if owner is None or generation is None:
        raise ClientUpdateRequiredError("Reload the page: your saved scans are kept.")

    def run(tx: Any, staff: Any) -> dict[str, Any]:
        limiter.check([(SUBMIT_PER_STAFF, str(staff.id))])
        return offline.submit(
            tx,
            staff,
            client_scan_id=str(payload.client_scan_id),
            kind=payload.kind,
            target_id=payload.target_id,
            phase=payload.phase,
            card_token=payload.card_token,
            scanned_at=payload.scanned_at,
            now=_now(),
            owner=owner,
            generation=generation,
        )

    return _write(db, token, run)


@router.get("/staff/offline-scans")
def queue(staff: CurrentStaff, db: DbSession, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {"scans": offline.pending(db, staff)}


@router.post("/staff/offline-scans/{scan_id}/approve")
def approve(
    scan_id: RowId, payload: Approval, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _write(
        db,
        token,
        lambda tx, staff: offline.approve(
            tx,
            staff,
            scan_id,
            payload.reason,
            _now(),
            verified_at=payload.verified_at,
            card_token=payload.card_token,
        ),
    )


@router.post("/staff/offline-scans/{scan_id}/reject")
def reject(scan_id: RowId, payload: Review, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(
        db, token, lambda tx, staff: offline.reject(tx, staff, scan_id, payload.reason, _now())
    )
