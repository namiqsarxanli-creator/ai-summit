"""`GET /api/interests` — the interest catalogue for the registration
form.

Not in IMPLEMENTATION_PLAN.md M04's API list, but required by it:
registration takes interest *slugs* (§8.1), and neither locked document
defines any other way for a client to learn them. Read-only, so it
takes no reset fence (§19.4: pure reads are exempt), and it exposes
only `slug` + `name` — the public identifier, never the internal id.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.db.session import DbSession
from app.modules.interests.service import list_active_interests

router = APIRouter(prefix="/interests", tags=["interests"])


class InterestOut(BaseModel):
    slug: str
    name: str


@router.get("", response_model=list[InterestOut])
def list_interests(db: DbSession) -> list[InterestOut]:
    return [InterestOut(slug=i.slug, name=i.name) for i in list_active_interests(db)]
