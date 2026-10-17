"""Leaderboard aggregation, ranking and pagination — ARCHITECTURE.md
§16, §17.4, invariants #1 and #22.

`points_log` is the only authority (invariant #1): every function here
recomputes `total_points` and `rank` from a fresh `SUM`/`RANK() OVER`
on every call — nothing is cached, nothing is stored, and no
`users.total_points` column exists anywhere in this schema to read
instead. The `(user_id)` index on `points_log` (M02) is what keeps this
sub-millisecond at demo scale (§16.4) — no new index is added here.

`_ranked()` is the single source both endpoints read from: one
GROUP BY (the §16.1 aggregation, LEFT JOIN so a zero-point ACTIVE
attendee is never dropped) wrapped in one SELECT that adds two window
functions — `RANK() OVER (ORDER BY total_points DESC)`, the DISPLAYED
competition rank (§16.2), and `ROW_NUMBER() OVER (ORDER BY total_points
DESC, last_award_at ASC NULLS LAST, user_id ASC)`, used only internally
to cut the top-N view at exactly N rows by list POSITION — a tie at the
boundary must not expand `entries` past N, which filtering on `rank`
alone would do (two attendees tied for rank 10 would both pass a
`rank <= 10` filter). Both windows are computed over the COMPLETE
eligible population before any LIMIT/filter is applied, since a
window function's own `ORDER BY` never restricts which rows survive.
"""

from __future__ import annotations

import base64
import binascii
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_, select, true
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session
from sqlalchemy.sql import Subquery

from app.core.config import get_settings
from app.core.images import SMALL
from app.core.media import avatar_url
from app.modules.admin.models import EventConfig
from app.modules.attendees.models import User, UserStatus
from app.modules.booths.models import Booth, Task
from app.modules.competition.rules import COMPETITION_SOURCES
from app.modules.leaderboard.schemas import (
    LeaderboardEntry,
    LeaderboardResponse,
    RecentAward,
    ScoreSnapshotResponse,
)
from app.modules.points.models import PointsLog
from app.modules.realtime.broker import event_bus

# §17.4: "the most recent N (default 10)". Fixed — no `event_config`
# column governs this (M13's own "Database Changes: None"), unlike
# `leaderboard_top_n`, which is an admin-configurable setting.
RECENT_AWARDS_LIMIT = 10

# No locked page size exists for the "show all" keyset view (only
# `leaderboard_top_n` governs the default top-N view, §16.3). Chosen as
# a reasonable, bounded default; callers may override via the `limit`
# query parameter (validated 1-100 at the router).
DEFAULT_PAGE_SIZE = 20


def competition_mode(db: Session) -> bool:
    """Migration 0020: is the Beyond Now competition ranking active?"""
    return bool(
        db.execute(select(EventConfig.competition_mode).where(EventConfig.id == 1)).scalar_one()
    )


def _scored(competition: bool = False) -> Subquery:
    """§16.1's aggregation, verbatim: `LEFT JOIN` (never `INNER JOIN` —
    that would silently drop a zero-point ACTIVE attendee) and the
    `status = 'ACTIVE'` filter (never cosmetic: without it, an
    unverified registration would appear publicly under a name it does
    not own, §16.1)."""
    return (
        select(
            User.id.label("user_id"),
            User.first_name,
            User.last_name,
            User.profession,
            # The photo's random name rides along in the SAME query (no per-row
            # lookup): it is a column of the row already being aggregated.
            User.photo_key,
            func.coalesce(func.sum(PointsLog.points), 0).label("total_points"),
            func.max(PointsLog.created_at).label("last_award_at"),
        )
        .select_from(User)
        .outerjoin(
            PointsLog,
            and_(PointsLog.user_id == User.id, PointsLog.source.in_(COMPETITION_SOURCES))
            if competition
            else PointsLog.user_id == User.id,
        )
        .where(User.status == UserStatus.ACTIVE)
        # Competition mode: the organiser team, volunteers, sponsors and
        # speakers are left out by a server-owned field no attendee can edit.
        .where(User.competition_excluded_reason.is_(None) if competition else true())
        .group_by(User.id)
        .subquery("scored")
    )


def _ranked(competition: bool = False) -> Subquery:
    """Competition mode: legacy TASK / FEEDBACK rows are not counted (decision of
    16 Oct 2026, competition/rules.py) and every person has their OWN place -
    on equal points whoever reached the total earlier is ahead, an identical
    moment is decided by the stable user id - instead of shared ranks."""
    scored = _scored(competition)
    order = (
        scored.c.total_points.desc(),
        scored.c.last_award_at.asc().nulls_last(),
        scored.c.user_id.asc(),
    )
    return (
        select(
            scored.c.user_id,
            scored.c.first_name,
            scored.c.last_name,
            scored.c.profession,
            scored.c.photo_key,
            scored.c.total_points,
            scored.c.last_award_at,
            (
                func.row_number().over(order_by=order)
                if competition
                else func.rank().over(order_by=scored.c.total_points.desc())
            ).label("rank"),
            func.row_number().over(order_by=order).label("rn"),
        )
        .select_from(scored)
        .subquery("ranked")
    )


def _entry(row: Row[Any]) -> LeaderboardEntry:
    return LeaderboardEntry(
        user_id=row.user_id,
        first_name=row.first_name,
        last_name=row.last_name,
        profession=row.profession,
        total_points=int(row.total_points),
        rank=int(row.rank),
        photo_url=avatar_url(row.photo_key, SMALL),
    )


def top_n_for(db: Session) -> int:
    return db.execute(select(EventConfig.leaderboard_top_n).where(EventConfig.id == 1)).scalar_one()


def _dataset_generation(db: Session) -> int:
    return int(
        db.execute(select(EventConfig.dataset_generation).where(EventConfig.id == 1)).scalar_one()
    )


@dataclass(frozen=True)
class RankingSnapshot:
    """The whole ACTIVE population, ranked once and shared by every request
    until the leaderboard may have changed.

    Found by load-testing a production copy: every award broadcasts
    `leaderboard_changed`, and every open phone then refetches the leaderboard
    and its score at the same moment. Ranking the full population per request
    made the single worker CPU-bound with a few hundred phones. Now one request
    ranks and the rest reuse it. It stays exact for this process: it is built
    for one `event_bus.leaderboard_version` (bumped by every award, reversal,
    leaderboard-visible profile change and dataset reset) and is discarded when
    that moves, and at the latest after `leaderboard_cache_seconds` (for changes
    another process makes, such as an attendee activating)."""

    version: int
    competition: bool
    built_at: float
    top_n: int
    dataset_generation: int
    entries: tuple[LeaderboardEntry, ...]  # in list order
    positions: dict[int, LeaderboardEntry]  # user_id -> entry


_snapshot: RankingSnapshot | None = None
_snapshot_lock = threading.Lock()


def _fresh(snapshot: RankingSnapshot | None, max_age: float, competition: bool) -> bool:
    return (
        snapshot is not None
        and snapshot.version == event_bus.leaderboard_version
        and snapshot.competition == competition
        and time.monotonic() - snapshot.built_at < max_age
    )


def ranking_snapshot(db: Session) -> RankingSnapshot | None:
    """The shared ranking, or None when caching is off (then callers query)."""
    global _snapshot
    max_age = get_settings().leaderboard_cache_seconds
    if max_age <= 0:
        return None
    competition = competition_mode(db)
    current = _snapshot
    if _fresh(current, max_age, competition):
        return current
    with _snapshot_lock:  # one request ranks; the others wait and reuse it
        current = _snapshot
        if _fresh(current, max_age, competition):
            return current
        built = _build_snapshot(db)
        _snapshot = built
        return built


def _build_snapshot(db: Session) -> RankingSnapshot:
    version = event_bus.leaderboard_version  # read BEFORE the query
    competition = competition_mode(db)
    ranked = _ranked(competition)
    rows = db.execute(
        select(ranked).order_by(
            ranked.c.total_points.desc(),
            ranked.c.last_award_at.asc().nulls_last(),
            ranked.c.user_id.asc(),
        )
    ).all()
    entries = tuple(_entry(r) for r in rows)
    return RankingSnapshot(
        version=version,
        competition=competition,
        built_at=time.monotonic(),
        top_n=top_n_for(db),
        dataset_generation=_dataset_generation(db),
        entries=entries,
        positions={e.user_id: e for e in entries},
    )


def leaderboard_push_payload(db: Session) -> dict[str, object]:
    """The top-N view as the `leaderboard_changed` event carries it.

    Ranked once per change and pushed to every open stream, so phones no
    longer all refetch `GET /api/leaderboard` at the same moment. The same
    rows `GET /api/leaderboard` returns; the caller's own row outside the
    top N is not in it (the client keeps the one it last fetched until its
    next reconciliation). `version` lets a client drop an out-of-order push."""
    snapshot = ranking_snapshot(db) or _build_snapshot(db)
    return {
        "entries": [e.model_dump(mode="json") for e in snapshot.entries[: snapshot.top_n]],
        "dataset_generation": snapshot.dataset_generation,
        "version": snapshot.version,
    }


def invalidate_ranking_snapshot() -> None:
    global _snapshot
    _snapshot = None


def leaderboard_top(
    db: Session, *, attendee: User, top_n: int | None = None, as_of: datetime
) -> LeaderboardResponse:
    """Default view: top N by list position, plus the caller's own row
    if it falls outside that window — one query, one round trip
    (§16.3: "not a second round trip"), by OR-ing the row-number cutoff
    with the caller's own `user_id` and letting the final `ORDER BY`
    place every returned row correctly."""
    snapshot = ranking_snapshot(db)
    if snapshot is not None and attendee.id in snapshot.positions:
        return LeaderboardResponse(
            entries=list(snapshot.entries[: snapshot.top_n]),
            current_user=snapshot.positions[attendee.id],
            as_of=as_of,
            dataset_generation=snapshot.dataset_generation,
            next_cursor=None,
        )
    if top_n is None:
        top_n = top_n_for(db)
    ranked = _ranked(competition_mode(db))
    rows = db.execute(
        select(ranked)
        .where(or_(ranked.c.rn <= top_n, ranked.c.user_id == attendee.id))
        .order_by(
            ranked.c.total_points.desc(),
            ranked.c.last_award_at.asc().nulls_last(),
            ranked.c.user_id.asc(),
        )
    ).all()
    entries = [_entry(r) for r in rows if r.rn <= top_n]
    current_user = next((_entry(r) for r in rows if r.user_id == attendee.id), None)
    return LeaderboardResponse(
        entries=entries,
        current_user=current_user,
        as_of=as_of,
        dataset_generation=_dataset_generation(db),
        next_cursor=None,
    )


@dataclass(frozen=True)
class LeaderboardCursor:
    total_points: int
    last_award_at: datetime | None
    user_id: int


def encode_cursor(cursor: LeaderboardCursor) -> str:
    payload = {
        "tp": cursor.total_points,
        "la": cursor.last_award_at.isoformat() if cursor.last_award_at is not None else None,
        "uid": cursor.user_id,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(raw: str) -> LeaderboardCursor:
    """Raises `ValueError` on anything malformed — a truncated/tampered
    string, non-JSON payload, or a payload missing/mistyping a field.
    The router translates that into `InvalidCursorError` (422). A
    well-behaved client only ever round-trips a value this module
    itself produced, so this path exists for robustness, not as part
    of the normal contract."""
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if not isinstance(payload, dict):
            raise ValueError("cursor payload is not an object")
        last_award_raw = payload["la"]
        last_award_at = (
            None if last_award_raw is None else datetime.fromisoformat(str(last_award_raw))
        )
        return LeaderboardCursor(
            total_points=int(payload["tp"]),
            last_award_at=last_award_at,
            user_id=int(payload["uid"]),
        )
    except (binascii.Error, ValueError, KeyError, TypeError) as err:
        raise ValueError("malformed leaderboard cursor") from err


def _after_cursor(ranked: Subquery, cursor: LeaderboardCursor) -> ColumnElement[bool]:
    """Keyset "strictly after" predicate for `(total_points DESC,
    last_award_at ASC NULLS LAST, user_id ASC)` — the exact ordering
    §16.2/§16.3 lock. Branches on whether the cursor's own
    `last_award_at` is NULL are resolved in Python (the value is fixed
    per request), not with a runtime CASE, since NULLS LAST reverses
    the usual ">" intuition once NULL enters either side of the
    comparison."""
    if cursor.last_award_at is not None:
        same_total_after = or_(
            ranked.c.last_award_at.is_(None),  # NULL sorts after every non-NULL value
            ranked.c.last_award_at > cursor.last_award_at,
            and_(ranked.c.last_award_at == cursor.last_award_at, ranked.c.user_id > cursor.user_id),
        )
    else:
        # Cursor is itself in the NULL tail of its total_points group;
        # only later NULL rows (by user_id) remain in that group.
        same_total_after = and_(ranked.c.last_award_at.is_(None), ranked.c.user_id > cursor.user_id)
    return or_(
        ranked.c.total_points < cursor.total_points,
        and_(ranked.c.total_points == cursor.total_points, same_total_after),
    )


def leaderboard_page(
    db: Session, *, cursor: LeaderboardCursor | None, limit: int, as_of: datetime
) -> LeaderboardResponse:
    """Keyset "show all" view (§16.3). No OFFSET anywhere — the next
    page's starting point is entirely determined by the last row of
    this one. Fetches `limit + 1` to learn whether a further page
    exists without a second query."""
    ranked = _ranked(competition_mode(db))
    stmt = select(ranked)
    if cursor is not None:
        stmt = stmt.where(_after_cursor(ranked, cursor))
    stmt = stmt.order_by(
        ranked.c.total_points.desc(),
        ranked.c.last_award_at.asc().nulls_last(),
        ranked.c.user_id.asc(),
    ).limit(limit + 1)
    rows = db.execute(stmt).all()

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    entries = [_entry(r) for r in page_rows]

    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor(
            LeaderboardCursor(
                total_points=int(last.total_points),
                last_award_at=last.last_award_at,
                user_id=last.user_id,
            )
        )

    return LeaderboardResponse(
        entries=entries,
        current_user=None,
        as_of=as_of,
        dataset_generation=_dataset_generation(db),
        next_cursor=next_cursor,
    )


def my_score(db: Session, *, attendee: User, as_of: datetime) -> ScoreSnapshotResponse:
    """§17.4's authoritative snapshot. `attendee` came from
    `CurrentAttendee`, which only ever resolves an ACTIVE session — so
    this row is always present in `_ranked()`'s ACTIVE-only population;
    `.one()` is the correct assertion, not a defensive guess."""
    snapshot = ranking_snapshot(db)
    mine = snapshot.positions.get(attendee.id) if snapshot is not None else None
    generation: int | None = None
    if snapshot is not None and mine is not None:
        total_points, rank = mine.total_points, mine.rank
        generation = snapshot.dataset_generation
    else:
        ranked = _ranked(competition_mode(db))
        row = db.execute(select(ranked).where(ranked.c.user_id == attendee.id)).one()
        total_points, rank = int(row.total_points), int(row.rank)

    recent_rows = db.execute(
        select(
            PointsLog.id,
            PointsLog.points,
            Booth.name.label("booth_name"),
            PointsLog.source,
            PointsLog.created_at,
            Task.title.label("task_title"),
        )
        .join(Booth, Booth.id == PointsLog.booth_id)
        .outerjoin(Task, Task.id == PointsLog.task_id)
        .where(PointsLog.user_id == attendee.id)
        # Display order only (§17.4) — never a delivery guarantee, and
        # `id` here is an identity/tiebreak, never a cursor (#22).
        .order_by(PointsLog.created_at.desc(), PointsLog.id.desc())
        .limit(RECENT_AWARDS_LIMIT)
    ).all()

    return ScoreSnapshotResponse(
        total_points=total_points,
        rank=rank,
        recent_awards=[
            RecentAward(
                id=r.id,
                points=r.points,
                booth_name=r.booth_name,
                source=r.source,
                created_at=r.created_at,
                task_title=r.task_title,
            )
            for r in recent_rows
        ],
        as_of=as_of,
        dataset_generation=generation if generation is not None else _dataset_generation(db),
    )


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "LeaderboardCursor",
    "RECENT_AWARDS_LIMIT",
    "decode_cursor",
    "encode_cursor",
    "leaderboard_page",
    "leaderboard_top",
    "my_score",
    "top_n_for",
]
