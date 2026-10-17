"""People compatibility, 0-100 (PDF "Matching: uyğunluq və görüş axını").

Deterministic: the same cards give the same score and the same reasons.

* complementary goal - one person seeks what the other offers (either way):
  40, ONCE (bounded, never per combination);
* each shared topic: 10, at most 30;
* shared masterclasses (both CONFIRMED): one 10, two 15;
* same sector: 15.

Suggested only at 30 or more; never oneself, nobody with the same (non-empty,
normalised) company, nobody blocked either way, only ACTIVE attendees. At most 5,
and never padded with weaker people. A suggestion card carries no contact data.
This is people matching; the booth recommendations (matching/) are separate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.modules.attendees.models import User, UserStatus
from app.modules.masterclasses.models import CONFIRMED, MasterclassReservation
from app.modules.people.models import UserBlock

# "Axtarıram" <-> "təklif edirəm" pairs (PDF: həll <-> məhsul, iş imkanı <->
# vakansiya, mentor <-> mentorluq, dostluq <-> dostluq; the rest by meaning).
COMPLEMENT: Final[dict[str, str]] = {
    "solution_supplier": "product_service",
    "partner": "partnership",
    "investor": "investment",
    "employee": "candidate",
    "job_opportunity": "vacancy",
    "mentor": "mentoring",
    "knowledge_exchange": "sharing_experience",
    "friendship": "friendship_hobby",
    "career_advice": "career_advice",
}
COMPLEMENT_POINTS: Final = 40
TOPIC_POINTS: Final = 10
TOPIC_MAX: Final = 30
MASTERCLASS_ONE: Final = 10
MASTERCLASS_TWO: Final = 15
SECTOR_POINTS: Final = 15
THRESHOLD: Final = 30
SUGGESTIONS: Final = 5


@dataclass(frozen=True)
class Match:
    score: int
    shared_topics: tuple[str, ...]
    complement: tuple[str, str] | None  # (what one seeks, what the other offers)
    shared_masterclasses: int
    same_sector: bool
    reasons: tuple[str, ...] = field(default=())


def normalise_company(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _complement(a: User, b: User) -> tuple[str, str] | None:
    for seeker, offerer in ((a, b), (b, a)):
        for want in sorted(seeker.card_seeking):
            offer = COMPLEMENT.get(want)
            if offer is not None and offer in offerer.card_offering:
                return want, offer
    return None


def compatibility(a: User, b: User, shared_masterclasses: int) -> Match:
    pair = _complement(a, b)
    topics = tuple(sorted(set(a.card_topics) & set(b.card_topics)))
    same_sector = a.card_sector is not None and a.card_sector == b.card_sector
    mc = (
        MASTERCLASS_TWO
        if shared_masterclasses >= 2
        else MASTERCLASS_ONE
        if shared_masterclasses == 1
        else 0
    )
    score = (
        (COMPLEMENT_POINTS if pair else 0)
        + min(TOPIC_POINTS * len(topics), TOPIC_MAX)
        + mc
        + (SECTOR_POINTS if same_sector else 0)
    )
    reasons = tuple(
        r
        for r, on in (
            ("complement", pair is not None),
            ("topics", bool(topics)),
            ("masterclass", shared_masterclasses > 0),
            ("sector", same_sector),
        )
        if on
    )
    return Match(min(score, 100), topics, pair, min(shared_masterclasses, 2), same_sector, reasons)


def blocked_ids(db: Session, user_id: int) -> set[int]:
    rows = db.execute(
        select(UserBlock.blocker_id, UserBlock.blocked_id).where(
            or_(UserBlock.blocker_id == user_id, UserBlock.blocked_id == user_id)
        )
    ).all()
    return {b if a == user_id else a for a, b in rows}


def _masterclasses(db: Session) -> dict[int, set[int]]:
    held: dict[int, set[int]] = {}
    for uid, mc in db.execute(
        select(MasterclassReservation.user_id, MasterclassReservation.masterclass_id).where(
            MasterclassReservation.status == CONFIRMED
        )
    ).all():
        held.setdefault(uid, set()).add(mc)
    return held


def suggestions(db: Session, me: User) -> list[tuple[User, Match]]:
    """Up to 5 people, best first (score, then id for a stable order)."""
    excluded = blocked_ids(db, me.id) | {me.id}
    company = normalise_company(me.company)
    held = _masterclasses(db)
    mine = held.get(me.id, set())
    scored: list[tuple[User, Match]] = []
    for other in db.execute(select(User).where(User.status == UserStatus.ACTIVE)).scalars():
        if other.id in excluded:
            continue
        if company and normalise_company(other.company) == company:
            continue
        match = compatibility(me, other, len(mine & held.get(other.id, set())))
        if match.score >= THRESHOLD:
            scored.append((other, match))
    scored.sort(key=lambda pair: (-pair[1].score, pair[0].id))
    return scored[:SUGGESTIONS]
