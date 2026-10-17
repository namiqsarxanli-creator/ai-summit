"""`GET /api/leaderboard`, `GET /api/attendees/me/score` —
ARCHITECTURE.md §16, §17.4, IMPLEMENTATION_PLAN.md M13.

Both are pure reads. §19.6 names "leaderboard, score snapshot, public
card, SSE stream" explicitly among the requests that take no reset
fence. Authentication and materialized reads share one worker call and release
their dependency-injected DB connection before the response is serialized.

`GET /api/leaderboard` doubles as two named views (§16.3) behind one
path, distinguished by `view`: the default top-N view (with the
caller's own row attached via `current_user` if they fall outside it),
and the keyset-paginated "show all" view. Nothing in IMPLEMENTATION_PLAN.md
locks the exact query-parameter shape for this — only the response
semantics — so this is this milestone's own implementation choice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Query, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.errors import InvalidCursorError
from app.core.reads import attendee_read
from app.core.sessions import SESSION_COOKIE_NAME
from app.db.session import DbSession
from app.modules.attendees.models import User
from app.modules.leaderboard.schemas import LeaderboardResponse, ScoreSnapshotResponse
from app.modules.leaderboard.service import (
    DEFAULT_PAGE_SIZE,
    decode_cursor,
    leaderboard_page,
    leaderboard_top,
    my_score,
)

router = APIRouter(tags=["leaderboard"])


@router.get("/leaderboard", response_model=LeaderboardResponse)
async def get_leaderboard(
    request: Request,
    db: DbSession,
    view: Literal["top", "all"] = "top",
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
) -> LeaderboardResponse:
    def read(session: Session, attendee: User) -> LeaderboardResponse:
        as_of = datetime.now(UTC)
        if view == "all":
            decoded = None
            if cursor is not None:
                try:
                    decoded = decode_cursor(cursor)
                except ValueError as err:
                    raise InvalidCursorError() from err
            return leaderboard_page(session, cursor=decoded, limit=limit, as_of=as_of)
        return leaderboard_top(session, attendee=attendee, as_of=as_of)

    return await run_in_threadpool(
        attendee_read, db, request.cookies.get(SESSION_COOKIE_NAME), read, identity_only=True
    )


@router.get("/attendees/me/score", response_model=ScoreSnapshotResponse)
async def get_my_score(request: Request, db: DbSession) -> ScoreSnapshotResponse:
    def read(session: Session, attendee: User) -> ScoreSnapshotResponse:
        return my_score(session, attendee=attendee, as_of=datetime.now(UTC))

    return await run_in_threadpool(
        attendee_read, db, request.cookies.get(SESSION_COOKIE_NAME), read, identity_only=True
    )
