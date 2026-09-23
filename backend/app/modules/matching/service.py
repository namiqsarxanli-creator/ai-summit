"""Orchestrates `app.modules.matching.core`'s pure engine against real
data — ARCHITECTURE.md §11, IMPLEMENTATION_PLAN.md M07.

Everything database-shaped lives here; `core.py` never imports SQLAlchemy.
Pure reads throughout — no reset fence (§19.4). Nothing is cached
(§11.4: "interest changes recompute on read").
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.attendees.models import User, UserInterest
from app.modules.booths.models import Booth, BoothInterest
from app.modules.booths.schemas import BoothOut
from app.modules.booths.service import CURATED_ORDER
from app.modules.interests.models import Interest
from app.modules.matching import core
from app.modules.matching.schemas import Match, MatchedInterest, Recommendation


def _attendee_interest_ids(db: Session, user: User) -> frozenset[int]:
    rows = db.execute(
        select(UserInterest.interest_id).where(UserInterest.user_id == user.id)
    ).scalars()
    return frozenset(rows)


@dataclass(frozen=True)
class _BoothProfile:
    booth: Booth
    weights: dict[int, Decimal]


def _booths_with_profiles(db: Session) -> list[_BoothProfile]:
    """Active booths that have at least one interest row (§10: "A booth
    with no interest profile is excluded from recommendations entirely
    rather than scored at 0"). Two queries, not N+1: the weight map per
    booth is assembled in Python from one bulk fetch."""
    booths = list(
        db.execute(select(Booth).where(Booth.is_active.is_(True)).order_by(CURATED_ORDER)).scalars()
    )
    if not booths:
        return []
    booth_ids = [b.id for b in booths]
    weight_rows = db.execute(
        select(BoothInterest.booth_id, BoothInterest.interest_id, BoothInterest.weight).where(
            BoothInterest.booth_id.in_(booth_ids)
        )
    ).all()
    weights_by_booth: dict[int, dict[int, Decimal]] = {}
    for booth_id, interest_id, weight in weight_rows:
        weights_by_booth.setdefault(booth_id, {})[interest_id] = weight
    return [
        _BoothProfile(booth=b, weights=weights_by_booth[b.id])
        for b in booths
        if b.id in weights_by_booth
    ]


def _interest_refs(db: Session, interest_ids: frozenset[int]) -> dict[int, core.InterestRef]:
    if not interest_ids:
        return {}
    rows = db.execute(
        select(Interest.id, Interest.slug, Interest.name).where(Interest.id.in_(interest_ids))
    ).all()
    return {i: core.InterestRef(slug=slug, name=name) for i, slug, name in rows}


def _booth_out(booth: Booth) -> BoothOut:
    return BoothOut(slug=booth.slug, name=booth.name, location_label=booth.location_label)


@dataclass(frozen=True)
class _Scored:
    booth: Booth
    result: core.MatchResult
    ordered: list[core.InterestRef]


def recommendations_for(db: Session, user: User) -> list[Recommendation]:
    """§11.6's total ordering: `score DESC, booth.name ASC, booth.id ASC`.

    No-interests case (§11.4): every eligible booth in curated order,
    `match: null`. Scored booths with `M = ∅` are excluded here — they
    are visible on the plain `GET /api/booths` list, never in this one.
    """
    attendee_interests = _attendee_interest_ids(db, user)
    profiles = _booths_with_profiles(db)

    if not attendee_interests:
        return [Recommendation(booth=_booth_out(p.booth), match=None) for p in profiles]

    results = [(p, core.score(attendee_interests, p.weights)) for p in profiles]
    matched_results = [(p, r) for p, r in results if r.matched]  # M = ∅ excluded (§11.4)

    all_matched_ids = frozenset().union(*(r.matched for _, r in matched_results))
    interests = _interest_refs(db, all_matched_ids)

    scored = [
        _Scored(
            booth=p.booth,
            result=r,
            ordered=core.order_matched(r.matched, p.weights, interests),
        )
        for p, r in matched_results
    ]
    scored.sort(key=lambda s: (-s.result.score, s.booth.name, s.booth.id))

    return [
        Recommendation(
            booth=_booth_out(s.booth),
            match=Match(
                score=s.result.score,
                matched_interests=[MatchedInterest(slug=i.slug, name=i.name) for i in s.ordered],
                explanation=core.explanation(s.result.score, s.ordered),
                algorithm_version=core.ALGORITHM_VERSION,
            ),
        )
        for s in scored
    ]
