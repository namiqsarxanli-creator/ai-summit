"""`GET /api/leaderboard` and `GET /api/attendees/me/score` response
contracts — ARCHITECTURE.md §16, §17.4, IMPLEMENTATION_PLAN.md M13.

`LeaderboardEntry`'s field list is the privacy allow-list: user id, the
public short name ("Anar N.", app/core/names.py - the event's rules; the full
surname stays on the person's own profile), profession, and the score/rank
fields themselves. `reached_at` — used only for
server-side tie-breaking (§16.2) — is never exposed: the ordering it
drives is enforced entirely server-side, so a client has nothing to
verify or reconstruct with it, and every other field this table could
expose (email, phone, LinkedIn, consent) is out of the allow-list
outright (§9.3.1's same discipline applied here).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.modules.points.models import PointsSource


class LeaderboardEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    # First name and surname initial; never the full surname (PDF conformance
    # audit, 18 Oct 2026).
    name: str
    profession: str
    total_points: int
    rank: int
    # The attendee's profile photo (small rendition), or None (the robot is shown).
    photo_url: str | None = None


class LeaderboardResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[LeaderboardEntry]
    # Populated for the default (top-N) view whether the caller is
    # inside or outside `entries` (§16.3) — always None on the keyset
    # "show all" view, which has no single "me" row concept.
    current_user: LeaderboardEntry | None
    as_of: datetime
    dataset_generation: int
    # Non-None only on the "show all" view, and only when a further
    # page exists. Opaque to the client — never PointsLog.id, never an
    # OFFSET (§16.3, invariant #22).
    next_cursor: str | None


class RecentAward(BaseModel):
    """`ScoreSnapshotResponse.recent_awards` — ARCHITECTURE.md §17.4's
    JSON example, reproduced field-for-field. Display order only
    (`created_at DESC, id DESC`); `id` is an identity here, never a
    cursor (invariant #22)."""

    model_config = ConfigDict(extra="forbid")

    id: int
    points: int
    booth_name: str
    source: PointsSource
    created_at: datetime
    # The activity name for awards made against one; None for awards that
    # predate activities (and for feedback rewards).
    task_title: str | None = None


class ScoreSnapshotResponse(BaseModel):
    """The authoritative personal snapshot (§17.4) — total, full-
    population rank, `as_of`, `dataset_generation`, and the most recent
    awards, all computed fresh on every request. No caching (§16.4)."""

    model_config = ConfigDict(extra="forbid")

    total_points: int
    rank: int
    recent_awards: list[RecentAward]
    as_of: datetime
    dataset_generation: int
