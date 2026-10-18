"""The Beyond Now interest card: four questions every attendee may answer.

* sector  - "my field", at most one choice
* topics  - "topics I'm interested in", at most three
* seeking - "what I'm looking for", at most two
* offering - "what I offer", at most two

Answers are stored as STABLE technical identifiers (`users.card_*`, migration
0016), never as display labels: the AZ/EN/RU labels live in the frontend and
can be reworded without touching data. The catalogue below is the single list
the server accepts; the frontend mirrors it (frontend/src/lib/interestCard.ts)
and a test keeps the two identical.

The card is stored separately from the older `interests` / `user_interests`
catalogue, which carries the booth weights. Booth recommendations use both: the
interests an attendee picked on the profile, and the catalogue interests their
card TOPICS stand for (`TOPIC_BOOTH_INTERESTS`, by catalogue slug). Nothing is
written back into `user_interests`. The documents fix only maximums, so no
group is required - an empty card is valid.

Validation happens in two layers:

* the schema (`InterestCard`) checks the SHAPE - well-formed identifiers, the
  per-group maximum, no repeats - the same for registration and profile editing;
* `ensure_known` checks the identifiers against the catalogue. Registration
  accepts the catalogue only. A profile edit additionally accepts an identifier
  the attendee ALREADY has stored in that same group, so a choice retired from
  the catalogue later can be kept as it is or removed, and never blocks editing
  the rest of the profile - but it can never be newly picked, moved to another
  group, or used to exceed a maximum (the maximum counts it like any other).
"""

from __future__ import annotations

import re
from typing import Final

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator

SECTORS: Final = (
    "bank_finance",
    "telecom_it",
    "public_sector",
    "education",
    "healthcare",
    "retail",
    "manufacturing_logistics",
    "energy",
    "media_marketing",
    "hr_consulting",
    "startup",
    "student",
    "other",
)
TOPICS: Final = (
    "ai_solutions",
    "business_solutions",
    "erp",
    "data",
    "ai_education",
    "hr",
    "design",
    "personal_effectiveness",
    "ai_strategy",
    "fintech",
    "marketing",
    "cybersecurity",
    "startups_investment",
    "public_services",
    "ai_ethics",
)
# Card topic -> booth-catalogue interest slugs it stands for in booth
# recommendations. Slugs the catalogue does not have (or has retired) are simply
# ignored, so an admin-edited catalogue never breaks recommendations. A test
# keeps the keys equal to TOPICS.
TOPIC_BOOTH_INTERESTS: Final[dict[str, tuple[str, ...]]] = {
    "ai_solutions": ("artificial-intelligence", "generative-ai"),
    "business_solutions": ("automation", "cloud-infrastructure"),
    "erp": ("automation", "cloud-infrastructure"),
    "data": ("data-analytics",),
    "ai_education": ("artificial-intelligence",),
    "hr": ("automation",),
    "design": ("generative-ai",),
    "personal_effectiveness": ("generative-ai", "automation"),
    "ai_strategy": ("artificial-intelligence", "data-analytics"),
    "fintech": ("fintech",),
    "marketing": ("generative-ai", "data-analytics"),
    "cybersecurity": ("cybersecurity",),
    "startups_investment": ("fintech",),
    "public_services": ("automation", "cloud-infrastructure"),
    "ai_ethics": ("artificial-intelligence",),
}

SEEKING: Final = (
    "solution_supplier",
    "partner",
    "investor",
    "employee",
    "job_opportunity",
    "mentor",
    "knowledge_exchange",
    "friendship",
    "career_advice",
)
OFFERING: Final = (
    "product_service",
    "partnership",
    "investment",
    "candidate",
    "vacancy",
    "mentoring",
    "sharing_experience",
    "career_advice",
    "volunteering",
    "friendship_hobby",
)

MAX_TOPICS: Final = 3
MAX_SEEKING: Final = 2
MAX_OFFERING: Final = 2


# What an identifier looks like (the catalogue's own style). A display label
# ("Bank və maliyyə") or anything else free-form is refused by the schema.
_IDENTIFIER: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _well_formed(v: str) -> str:
    if not _IDENTIFIER.fullmatch(v):
        raise ValueError("unknown choice")
    return v


def _checked(values: list[str], maximum: int) -> list[str]:
    """Too many, repeated or malformed choices are refused, never silently fixed:
    a client that sends them has a bug the person should not pay for later."""
    if len(values) > maximum:
        raise ValueError(f"choose at most {maximum}")
    if len(set(values)) != len(values):
        raise ValueError("a choice may appear only once")
    for v in values:
        _well_formed(v)
    return values


class InterestCard(BaseModel):
    """The card as registration and profile editing both send it (one contract)
    and as the attendee's own profile returns it."""

    model_config = ConfigDict(extra="forbid")

    sector: str | None = None
    topics: list[str] = Field(default_factory=list)
    seeking: list[str] = Field(default_factory=list)
    offering: list[str] = Field(default_factory=list)

    @field_validator("sector")
    @classmethod
    def _check_sector(cls, v: str | None) -> str | None:
        return None if v is None else _well_formed(v)

    @field_validator("topics")
    @classmethod
    def _check_topics(cls, v: list[str]) -> list[str]:
        return _checked(v, MAX_TOPICS)

    @field_validator("seeking")
    @classmethod
    def _check_seeking(cls, v: list[str]) -> list[str]:
        return _checked(v, MAX_SEEKING)

    @field_validator("offering")
    @classmethod
    def _check_offering(cls, v: list[str]) -> list[str]:
        return _checked(v, MAX_OFFERING)


CATALOGUE: Final[dict[str, tuple[str, ...]]] = {
    "sector": SECTORS,
    "topics": TOPICS,
    "seeking": SEEKING,
    "offering": OFFERING,
}


def _chosen(card: InterestCard, group: str) -> list[str]:
    if group == "sector":
        return [] if card.sector is None else [card.sector]
    values: list[str] = getattr(card, group)
    return values


def refused_choices(card: InterestCard, stored: InterestCard | None = None) -> dict[str, list[str]]:
    """Per group, the identifiers that are neither in the catalogue nor already
    stored for this attendee IN THAT GROUP. `stored` is None for registration."""
    refused: dict[str, list[str]] = {}
    for group, allowed in CATALOGUE.items():
        kept = set(_chosen(stored, group)) if stored is not None else set()
        bad = [v for v in _chosen(card, group) if v not in allowed and v not in kept]
        if bad:
            refused[group] = bad
    return refused


def ensure_known(card: InterestCard, stored: InterestCard | None = None) -> None:
    """Refuse unknown choices with the same 422 VALIDATION_ERROR the schema
    gives, located on the offending group (`body.interest_card.<group>`), so the
    client shows them exactly like any other card error. The identifiers
    themselves are not echoed back."""
    refused = refused_choices(card, stored)
    if refused:
        raise RequestValidationError(
            [
                {
                    "loc": ("body", "interest_card", group),
                    "msg": "unknown choice",
                    "type": "value_error",
                }
                for group in refused
            ]
        )


def card_of(
    sector: str | None, topics: list[str], seeking: list[str], offering: list[str]
) -> InterestCard:
    """The stored card, for output. Read back as stored (`model_construct`), so a
    choice retired from the catalogue later never makes a profile unreadable."""
    return InterestCard.model_construct(
        sector=sector, topics=list(topics), seeking=list(seeking), offering=list(offering)
    )


def clean_company(v: str | None) -> str | None:
    """Optional: surrounding space stripped, empty means "no company"."""
    if v is None:
        return None
    v = v.strip()
    return v or None
