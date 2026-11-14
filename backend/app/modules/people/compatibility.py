"""People compatibility, 0-100 (PDF "Matching: uyğunluq və görüş axını").

Deterministic: the same cards give the same score and the same reasons.

* complementary goal - one person seeks what the other offers (either way):
  40, ONCE (bounded, never per combination);
* each shared topic: 10, at most 30;
* shared masterclasses (both CONFIRMED): one 10, two 15;
* same sector: 15.

Suggested only at 30 or more; never oneself, no colleague (`same_company`: the
same normalised company name, or the same work e-mail domain), nobody blocked
either way, only ACTIVE attendees. At most 5,
and never padded with weaker people. A suggestion card carries no contact data.
This is people matching; the booth recommendations (matching/) are separate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

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


class Card(Protocol):
    """The four interest-card answers a compatibility needs (a `User` is one)."""

    @property
    def card_sector(self) -> str | None: ...
    @property
    def card_topics(self) -> Sequence[str]: ...
    @property
    def card_seeking(self) -> Sequence[str]: ...
    @property
    def card_offering(self) -> Sequence[str]: ...


@dataclass(frozen=True)
class _Candidate:
    """One other attendee as the ranking needs them: id, company and card only.
    Loading these few columns instead of full `User` rows keeps a ranking of the
    whole event cheap (it runs inside a mutual-scan transaction)."""

    id: int
    company: str | None
    card_sector: str | None
    card_topics: Sequence[str]
    card_seeking: Sequence[str]
    card_offering: Sequence[str]


@dataclass(frozen=True)
class Match:
    score: int
    shared_topics: tuple[str, ...]
    complement: tuple[str, str] | None  # (what one seeks, what the other offers)
    shared_masterclasses: int
    same_sector: bool
    reasons: tuple[str, ...] = field(default=())


# Legal-form words that do not tell two companies apart ("AIWorks MMC" is
# "AIWorks"), compared after case folding and with punctuation removed.
_LEGAL_FORMS: Final = frozenset(
    {
        "mmc", "asc", "qsc", "ats", "llc", "ltd", "limited", "inc", "corp", "co", "plc",
        "jsc", "cjsc", "ojsc", "gmbh", "ag", "sa", "bv", "ooo", "ооо", "зао", "оао", "пао",
        "şirkəti", "şirkət", "company",
    }
)  # fmt: skip
# Answers that mean "no company": two students are not colleagues.
_NO_COMPANY: Final = frozenset(
    {
        "", "tələbə", "telebe", "student", "студент", "студентка", "fərdi", "ferdi",
        "freelancer", "freelance", "frilanser", "selfemployed", "фрилансер", "самозанятый",
        "yoxdur", "yox", "none", "no", "na", "нет", "işsiz", "unemployed",
    }
)  # fmt: skip
# Mailbox providers anyone can use: a shared domain there says nothing.
_PUBLIC_MAIL: Final = frozenset(
    {
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
        "yahoo.com", "ymail.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me",
        "protonmail.com", "pm.me", "gmx.com", "gmx.de", "gmx.net", "zoho.com", "yandex.com",
        "yandex.ru", "ya.ru", "mail.ru", "inbox.ru", "bk.ru", "list.ru", "rambler.ru",
        "mail.az", "box.az", "rambler.az", "hotmail.co.uk", "outlook.de", "web.de",
    }
)  # fmt: skip


def normalise_company(value: str | None) -> str:
    """The comparable form of a company name: case folded, letters and digits
    only, legal-form words dropped, spaces removed ("AI Works MMC" and "aiworks"
    are one company). "" for no company, including the answers that mean none."""
    words = "".join(ch if ch.isalnum() else " " for ch in (value or "").casefold()).split()
    key = "".join(w for w in words if w not in _LEGAL_FORMS)
    return "" if key in _NO_COMPANY else key


# Reserved names (RFC 2606/6761) and the demo data's own: never anyone's employer.
_RESERVED_DOMAINS: Final = frozenset({"example.com", "example.org", "example.net"})
_RESERVED_SUFFIXES: Final = (".test", ".example", ".invalid", ".localhost", ".demo")


def work_domain(email: str | None) -> str:
    """The domain of a work address, "" for a public mailbox, a reserved name or
    no address."""
    domain = (email or "").rpartition("@")[2].strip().casefold()
    if not domain or domain in _PUBLIC_MAIL or domain in _RESERVED_DOMAINS:
        return ""
    return "" if domain.endswith(_RESERVED_SUFFIXES) else domain


def same_company(
    company_a: str | None, email_a: str | None, company_b: str | None, email_b: str | None
) -> bool:
    """Colleagues: the same named company, or addresses at the same work domain.
    Either is enough, so writing the company differently does not hide it."""
    company = normalise_company(company_a)
    if company and company == normalise_company(company_b):
        return True
    domain = work_domain(email_a)
    return bool(domain) and domain == work_domain(email_b)


def _complement(a: Card, b: Card) -> tuple[str, str] | None:
    for seeker, offerer in ((a, b), (b, a)):
        for want in sorted(seeker.card_seeking):
            offer = COMPLEMENT.get(want)
            if offer is not None and offer in offerer.card_offering:
                return want, offer
    return None


def compatibility(a: Card, b: Card, shared_masterclasses: int) -> Match:
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


def suggested(db: Session, me: User) -> list[tuple[int, Match]]:
    """Up to 5 people as (user id, match), best first (score, then id for a
    stable order). Reads only the columns the ranking needs."""
    excluded = blocked_ids(db, me.id) | {me.id}
    held = _masterclasses(db)
    mine = held.get(me.id, set())
    scored: list[tuple[int, Match]] = []
    rows = db.execute(
        select(
            User.id,
            User.company,
            User.email,
            User.card_sector,
            User.card_topics,
            User.card_seeking,
            User.card_offering,
        ).where(User.status == UserStatus.ACTIVE)
    ).all()
    for uid, other_company, other_email, sector, topics, seeking, offering in rows:
        if uid in excluded:
            continue
        if same_company(me.company, me.email, other_company, other_email):
            continue
        other = _Candidate(uid, other_company, sector, topics or (), seeking or (), offering or ())
        match = compatibility(me, other, len(mine & held.get(uid, set())))
        if match.score >= THRESHOLD:
            scored.append((uid, match))
    scored.sort(key=lambda pair: (-pair[1].score, pair[0]))
    return scored[:SUGGESTIONS]


def suggestions(db: Session, me: User) -> list[tuple[User, Match]]:
    """Up to 5 people, best first, as full rows (for the People page)."""
    top = suggested(db, me)
    if not top:
        return []
    users = {
        u.id: u
        for u in db.execute(select(User).where(User.id.in_([uid for uid, _ in top]))).scalars()
    }
    return [(users[uid], match) for uid, match in top if uid in users]
