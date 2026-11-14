"""Card resolution — ARCHITECTURE.md §9.

Read-only: every function here is a pure read, so none takes the reset
fence (§19.4: "public card" is listed among the pure reads).

Token ISSUANCE is `app.core.security.generate_card_token` (§9.1), called
by registration (M04), which creates the card. Rotation/revocation is
schema-ready but not built (§9.6): resolution filters
`revoked_at IS NULL`, so a revoked token already fails with no extra
code path.
"""

from __future__ import annotations

import hashlib
import hmac
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import InvalidQrError
from app.core.images import LARGE
from app.core.media import avatar_url
from app.core.urls import is_linkedin_url
from app.modules.attendees.models import User, UserInterest, UserStatus
from app.modules.contact_cards.models import ContactCard
from app.modules.contact_cards.schemas import (
    CardIdentity,
    CardInterest,
    ContactAction,
    OwnCard,
    PublicCard,
)
from app.modules.interests.models import Interest


def contact_action_for(*, linkedin_url: str | None, email: str) -> ContactAction:
    """§9.3.1, verbatim: "if linkedin_url is present → action =
    LINKEDIN, value = linkedin_url; else → action = EMAIL, value =
    email". Chosen here, on the server: the client is never sent both."""
    if linkedin_url and is_linkedin_url(linkedin_url):
        return ContactAction(type="LINKEDIN", value=linkedin_url.strip())
    return ContactAction(type="EMAIL", value=email)


# The card's SHORT CODE (owner's decision, 3 Nov 2026): six characters under the
# QR that a volunteer or another attendee types when the camera cannot read it.
# Derived from the live token with the server's secret (an HMAC, never stored),
# so a new token gets a new code; the alphabet leaves out 0/O, 1/I/L. A lookup
# answers only for a live card of an ACTIVE attendee and only to a signed-in
# caller under a rate limit: 31**6 (~ 8.9e8) codes against a few hundred cards.
SHORT_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_SHORT_CODE = re.compile(f"[{SHORT_ALPHABET}]{{6}}")


def card_short_code(token: str) -> str:
    digest = hmac.new(
        get_settings().scan_ref_secret.encode(),
        f"card-short-v1:{token}".encode(),
        hashlib.sha256,
    ).digest()
    n = int.from_bytes(digest[:8], "big")
    chars = []
    for _ in range(6):
        n, r = divmod(n, len(SHORT_ALPHABET))
        chars.append(SHORT_ALPHABET[r])
    return "".join(chars)


def normalise_short_code(text: str) -> str | None:
    code = re.sub(r"[\s-]", "", text).upper()
    return code if _SHORT_CODE.fullmatch(code) else None


def token_for_short_code(db: Session, text: str) -> str:
    """The live card token a typed short code names, or `InvalidQrError`."""
    code = normalise_short_code(text)
    if code is None:
        raise InvalidQrError()
    live = db.execute(
        select(ContactCard.public_token)
        .join(User, User.id == ContactCard.user_id)
        .where(ContactCard.revoked_at.is_(None), User.status == UserStatus.ACTIVE)
    ).scalars()
    found = [t for t in live if hmac.compare_digest(card_short_code(t), code)]
    if len(found) != 1:  # none, or (never seen) two alike: refuse, never guess
        raise InvalidQrError()
    return found[0]


def card_url_for(token: str) -> str:
    """§9.2: the QR encodes only `https://<host>/c/<public_token>`. The
    host is the configured application origin — the one origin the
    browser ever talks to (§3.2) — never a request header."""
    return f"{get_settings().app_origin}/c/{token}"


def _live_card_owner(db: Session, token: str) -> User | None:
    """The single lookup behind BOTH consumers. Malformed, unknown,
    revoked, provisional-owner and deleted-owner tokens all fall out of
    this one query as None — the same statement runs for every token, so
    no failure case takes a different path (§9.4)."""
    return db.execute(
        select(User)
        .join(ContactCard, ContactCard.user_id == User.id)
        .where(
            ContactCard.public_token == token,
            ContactCard.revoked_at.is_(None),
            User.status == UserStatus.ACTIVE,
        )
    ).scalar_one_or_none()


def _public_card_for(db: Session, user: User, viewer_id: int | None = None) -> PublicCard:
    interests = db.execute(
        select(Interest.slug, Interest.name)
        .join(UserInterest, UserInterest.interest_id == Interest.id)
        .where(UserInterest.user_id == user.id, Interest.is_active.is_(True))
        .order_by(Interest.sort_order, Interest.slug)
    ).all()
    return PublicCard(
        first_name=user.first_name,
        last_name=user.last_name,
        profession=user.profession,
        interests=[CardInterest(slug=slug, name=name) for slug, name in interests],
        contact=(
            contact_action_for(linkedin_url=user.linkedin_url, email=user.email)
            if viewer_id is not None and _connected(db, viewer_id, user.id)
            else None
        ),
        # Public by design: it is what the person chose to show on their card and on
        # the leaderboard. Read off the row already loaded - no extra query.
        photo_url=avatar_url(user.photo_key, LARGE),
    )


def resolve_active_attendee(db: Session, token: str) -> User:
    """The live `ACTIVE` attendee a card token names, or `InvalidQrError`
    (§9.4). Shared by every consumer that needs the full `User` row —
    staff resolve (M06, via `resolve_card_identity` below) and the M11
    award path (`points/service.py`, which needs `user.id`) — so a
    card's validity is judged by exactly one predicate everywhere."""
    user = _live_card_owner(db, token)
    if user is None:
        raise InvalidQrError()
    return user


def _connected(db: Session, a: int, b: int) -> bool:
    from app.modules.people.connections import connected

    return connected(db, a, b)


def resolve_public_card(db: Session, token: str, viewer_id: int | None = None) -> PublicCard:
    user = _live_card_owner(db, token)
    if user is None:
        raise InvalidQrError()
    return _public_card_for(db, user, viewer_id)


def resolve_card_identity(db: Session, token: str) -> CardIdentity:
    """The staff shape — name and profession only (§9.3, §24.20)."""
    user = resolve_active_attendee(db, token)
    return CardIdentity(
        first_name=user.first_name, last_name=user.last_name, profession=user.profession
    )


def own_card(db: Session, user: User) -> OwnCard:
    """`user` comes from the resolved session, never from input."""
    token = db.execute(
        select(ContactCard.public_token).where(
            ContactCard.user_id == user.id, ContactCard.revoked_at.is_(None)
        )
    ).scalar_one_or_none()
    if token is None:
        # Registration issues the card (M04) and nothing revokes one yet
        # (§9.6), so an ACTIVE attendee without a live card is a broken
        # invariant — surfaced as INTERNAL_ERROR, not papered over.
        raise RuntimeError("active attendee has no live contact card")
    return OwnCard(
        card_url=card_url_for(token),
        card=_public_card_for(db, user),
        short_code=card_short_code(token),
    )
