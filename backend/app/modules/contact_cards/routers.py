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

from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import ClientIp, CurrentAttendee, CurrentStaff, SessionToken
from app.core.errors import DomainError
from app.core.ratelimit import CARD_READ_PER_IP, CARD_READ_PER_TOKEN, RateLimit, limiter
from app.core.security import hash_card_token
from app.core.sessions import resolve_attendee_session
from app.db.session import DbSession
from app.modules.contact_cards.schemas import OwnCard, PublicCard
from app.modules.contact_cards.service import own_card, resolve_public_card, token_for_short_code

router = APIRouter(tags=["contact-cards"])


# `:path` so that a token containing anything at all — even a `/` —
# still reaches the one INVALID_QR branch (§9.4 "malformed") instead of
# the framework's differently-shaped 404.
@router.get("/cards/{token:path}", response_model=PublicCard)
def get_public_card(token: str, db: DbSession, ip: ClientIp, session: SessionToken) -> PublicCard:
    """Unauthenticated. §21.7: 30/min per token, 1200/min per IP. The
    token bucket is keyed on the token's hash, so the limiter never holds
    a copy of a live capability."""
    limiter.check([(CARD_READ_PER_IP, ip), (CARD_READ_PER_TOKEN, hash_card_token(token))])
    viewer_id: int | None = None
    if session:
        try:  # an optional viewer: only to learn whether they are connected
            viewer_id = resolve_attendee_session(db, session).id
        except DomainError:
            viewer_id = None
    return resolve_public_card(db, token, viewer_id)


@router.get("/attendees/me/card", response_model=OwnCard)
def get_my_card(attendee: CurrentAttendee, db: DbSession) -> OwnCard:
    return own_card(db, attendee)


# --- the card's short code, typed when the QR cannot be read ---------------------

# A typed card code, when a QR will not scan: shared by every volunteer on one login.
CARD_LOOKUP_PER_STAFF = RateLimit("card_lookup:staff", 1500, 600)
CARD_LOOKUP_PER_ATTENDEE = RateLimit("card_lookup:attendee", 20, 600)


class CardLookup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=6, max_length=12)


@router.post("/staff/cards/lookup")
def staff_card_lookup(
    payload: CardLookup, staff: CurrentStaff, db: DbSession, response: Response
) -> dict[str, Any]:
    """A volunteer or booth staff types the six characters under a card: the
    card token, exactly what scanning its QR would have given them."""
    limiter.check([(CARD_LOOKUP_PER_STAFF, str(staff.id))])
    response.headers["Cache-Control"] = "no-store"
    return {"card_token": token_for_short_code(db, payload.code)}


@router.post("/attendees/me/cards/lookup")
def attendee_card_lookup(
    payload: CardLookup, attendee: CurrentAttendee, db: DbSession, response: Response
) -> dict[str, Any]:
    """The same for another attendee connecting by hand (People page)."""
    limiter.check([(CARD_LOOKUP_PER_ATTENDEE, str(attendee.id))])
    response.headers["Cache-Control"] = "no-store"
    return {"card_token": token_for_short_code(db, payload.code)}
