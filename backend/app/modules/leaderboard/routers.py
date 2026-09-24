"""`GET /api/leaderboard`, `GET /api/attendees/me/score` —
ARCHITECTURE.md §16, §17.4, IMPLEMENTATION_PLAN.md M13.

Both are pure reads. §19.6 names "leaderboard, score snapshot, public
card, SSE stream" explicitly among the requests that take no reset
fence: a reader sees either the pre- or post-restore dataset, never a
partial one, because the restore itself is one transaction. So — like
`matching/routers.py`'s `/attendees/me/recommendations` — both routes
use `CurrentAttendee` directly, the ordinary FastAPI dependency
resolved before the handler body runs; there is no `mutating_transaction`
here for it to run ahead of.

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

from fastapi import APIRouter, Query

from app.core.deps import CurrentAttendee
from app.core.errors import InvalidCursorError
from app.db.session import DbSession
from app.modules.leaderboard.schemas import LeaderboardResponse, ScoreSnapshotResponse
from app.modules.leaderboard.service import (
    DEFAULT_PAGE_SIZE,
    decode_cursor,
    leaderboard_page,
    leaderboard_top,
    my_score,
    top_n_for,
)

router = APIRouter(tags=["leaderboard"])


@router.get("/leaderboard", response_model=LeaderboardResponse)
def get_leaderboard(
    attendee: CurrentAttendee,
    db: DbSession,
    view: Literal["top", "all"] = "top",
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
) -> LeaderboardResponse:
    as_of = datetime.now(UTC)
    if view == "all":
        decoded = None
        if cursor is not None:
            try:
                decoded = decode_cursor(cursor)
            except ValueError as err:
                raise InvalidCursorError() from err
        return leaderboard_page(db, cursor=decoded, limit=limit, as_of=as_of)
    return leaderboard_top(db, attendee=attendee, top_n=top_n_for(db), as_of=as_of)


@router.get("/attendees/me/score", response_model=ScoreSnapshotResponse)
def get_my_score(attendee: CurrentAttendee, db: DbSession) -> ScoreSnapshotResponse:
    return my_score(db, attendee=attendee, as_of=datetime.now(UTC))
