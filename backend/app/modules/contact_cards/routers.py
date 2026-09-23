"""`GET /api/cards/{token}` (public, §9.3) and `GET /api/attendees/me/card`.

The second route is not named in either locked document, but M06
requires it: the "My QR" screen renders the code client-side from the
token (IMPLEMENTATION_PLAN.md M06), and the only way for the client to
learn its own token is to ask the server, which picks the card from the
session — there is no user id anywhere in the request.

Both are pure reads — no reset fence (§19.4). Privacy headers and log
masking for these paths are applied app-wide (app/core/privacy.py).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import ClientIp, CurrentAttendee
from app.core.ratelimit import CARD_READ_PER_IP, CARD_READ_PER_TOKEN, limiter
from app.core.security import hash_card_token
from app.db.session import DbSession
from app.modules.contact_cards.schemas import OwnCard, PublicCard
from app.modules.contact_cards.service import own_card, resolve_public_card

router = APIRouter(tags=["contact-cards"])


# `:path` so that a token containing anything at all — even a `/` —
# still reaches the one INVALID_QR branch (§9.4 "malformed") instead of
# the framework's differently-shaped 404.
@router.get("/cards/{token:path}", response_model=PublicCard)
def get_public_card(token: str, db: DbSession, ip: ClientIp) -> PublicCard:
    """Unauthenticated. §21.7: 30/min per token, 1200/min per IP. The
    token bucket is keyed on the token's hash, so the limiter never holds
    a copy of a live capability."""
    limiter.check([(CARD_READ_PER_TOKEN, hash_card_token(token)), (CARD_READ_PER_IP, ip)])
    return resolve_public_card(db, token)


@router.get("/attendees/me/card", response_model=OwnCard)
def get_my_card(attendee: CurrentAttendee, db: DbSession) -> OwnCard:
    return own_card(db, attendee)
