"""Request/response schemas for the registration endpoint —
ARCHITECTURE.md §8.1, §12.2, §21.4.

Every mutating schema sets `extra="forbid"` (invariant #16): an
unexpected field is a 422, not a silently ignored value.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StrictBool,
    field_validator,
)

from app.core.languages import DEFAULT_LANGUAGE, RequestLanguage
from app.core.urls import is_linkedin_url
from app.modules.attendees.interest_card import InterestCard, clean_company


def _validate_linkedin_url(value: str) -> str:
    """ARCHITECTURE.md §8.1: "LinkedIn URL validated for scheme and
    host shape." Loose by design — this rejects garbage, not
    legitimate LinkedIn URL variations (country subdomains, trailing
    slashes, query strings)."""
    if not is_linkedin_url(value):
        raise ValueError("linkedin_url must be an http(s) linkedin.com URL")
    return value


class CompanyAndCard(BaseModel):
    """What registration and profile editing share about the company and the
    interest card - ONE contract, so the two forms cannot drift apart. Both are
    optional: an attendee without a company, or who skips the card, registers
    and edits normally."""

    model_config = ConfigDict(extra="forbid")

    # Separate from `profession` (the role): never merged into it.
    company: str | None = Field(default=None, max_length=200)
    interest_card: InterestCard = Field(default_factory=InterestCard)

    @field_validator("company")
    @classmethod
    def _clean_company(cls, v: str | None) -> str | None:
        return clean_company(v)


class RegisterRequest(CompanyAndCard):
    model_config = ConfigDict(extra="forbid")

    first_name: str = Field(min_length=1, max_length=200)
    last_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    phone: str = Field(min_length=1, max_length=50)
    profession: str = Field(min_length=1, max_length=200)
    linkedin_url: str | None = Field(default=None, max_length=2048)
    # Same bound as ProfileUpdate: the list goes into one IN (...) query and an
    # unknown slug is echoed in the error, on an unauthenticated route.
    interest_slugs: list[str] = Field(default_factory=list, max_length=100)
    # Required, no default (ARCHITECTURE.md §8.1: "a required explicit
    # boolean with no default; absence is a validation error").
    sponsor_consent: bool
    # Language of the OTP email: az / en / ru. Optional so older clients keep
    # working (they get the default); anything else is rejected.
    language: RequestLanguage = DEFAULT_LANGUAGE

    @field_validator("linkedin_url")
    @classmethod
    def _check_linkedin_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_linkedin_url(v)

    @field_validator("first_name", "last_name", "phone", "profession")
    @classmethod
    def _strip_whitespace(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class ProfileUpdate(CompanyAndCard):
    """`PUT /api/attendees/me/profile`: the attendee's own editable details, the
    WHOLE set (a form's current state), plus the version the form was loaded at.

    `company` and `interest_card` (shared with registration) may be left out by a
    client that predates them - a cached page of the app - and are then left as
    they are; sent, they replace what is stored (see `update_profile`).

    Same rules as registration (lengths, stripped text, LinkedIn URL shape; the
    parity is tested). Not here, on purpose: `email` (changing it is a separate,
    verified flow that does not exist yet - `extra="forbid"` makes it a 422),
    `sponsor_consent` (its own endpoint, its own audit), the photo, and
    anything that could name another attendee: who is edited is the session's."""

    model_config = ConfigDict(extra="forbid")

    first_name: str = Field(min_length=1, max_length=200)
    last_name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=1, max_length=50)
    profession: str = Field(min_length=1, max_length=200)
    # An empty field means "no LinkedIn": null and "" are the same here.
    linkedin_url: str | None = Field(default=None, max_length=2048)
    interest_slugs: list[str] = Field(max_length=100)
    # The version of the profile this form was loaded at. If someone (this attendee
    # on another device) saved since, the edit is refused with PROFILE_CONFLICT.
    expected_version: int = Field(ge=1, le=2**31 - 1)

    @field_validator("first_name", "last_name", "phone", "profession")
    @classmethod
    def _strip_whitespace(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("linkedin_url")
    @classmethod
    def _check_linkedin_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        return _validate_linkedin_url(v)

    @field_validator("interest_slugs")
    @classmethod
    def _clean_slugs(cls, v: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for slug in v:
            cleaned = slug.strip()
            if not cleaned or len(cleaned) > 100:
                raise ValueError("an interest slug must be 1-100 characters")
            seen.setdefault(cleaned, None)
        return list(seen)


class ConsentUpdate(BaseModel):
    """`PUT /api/attendees/me/sponsor-consent`: the attendee's CHOICE, as a value
    (not "toggle"), so a repeated request is the same request."""

    model_config = ConfigDict(extra="forbid")

    # A real boolean only: a string such as "yes" is a mistake, not a decision.
    granted: StrictBool


class ConsentState(BaseModel):
    """The sponsor-consent state after the request. `changed` is false when the
    value was already what was asked for: nothing was written, no audit event."""

    sponsor_consent: bool
    sponsor_consent_at: AwareDatetime | None
    consent_version: str | None
    changed: bool


class PhotoUrls(BaseModel):
    """Where the attendee's own photo is served: a small and a larger rendition.
    The names are random and change with every new photo (so they cache forever)."""

    small: str
    large: str


class PhotoResponse(BaseModel):
    photo: PhotoUrls | None


class AttendeeMe(BaseModel):
    """`GET /api/attendees/me` — the signed-in attendee's own identity,
    for the frontend's authenticated state. Deliberately minimal:
    no internal id, no phone, no consent state, nothing about the
    session itself (§21.4: "Output schemas are explicit, so a model
    gaining a sensitive column does not silently start leaking it")."""

    first_name: str
    last_name: str
    email: str
    profession: str


class ProfileInterest(BaseModel):
    slug: str
    name: str


class AttendeeProfile(BaseModel):
    """`GET /api/attendees/me/profile` (M08) — the signed-in attendee's
    OWN private profile screen: "registration data, selected interests,
    consent state (private view only)" (IMPLEMENTATION_PLAN.md M08).

    Deliberately a SEPARATE schema from `AttendeeMe`, never derived from
    it — the same discipline §9.3's `CardIdentity`/`PublicCard` split
    uses: each consumer gets exactly the fields it needs, so widening
    one can never silently widen the other. `AttendeeMe` stays exactly
    as M05 defined it (§21.4: "deliberately minimal ... no phone, no
    consent state") for the lightweight session-check widgets that use
    it; this schema exists only for the one screen that is explicitly
    the attendee's own private view of their full registration record —
    never returned about anyone but the caller, and never part of any
    public response (§24.19 governs PUBLIC responses only)."""

    first_name: str
    last_name: str
    email: str
    phone: str
    profession: str
    linkedin_url: str | None
    sponsor_consent: bool
    # When and under which wording the consent was given; None while it is withheld.
    sponsor_consent_at: datetime | None = None
    sponsor_consent_version: str | None = None
    interests: list[ProfileInterest]
    # Private, own-view only - never on the public card, leaderboard or exports.
    company: str | None = None
    interest_card: InterestCard = Field(default_factory=InterestCard)
    # What `PUT /me/profile` must send back as `expected_version`.
    version: int = 1
    photo: PhotoUrls | None = None


class RegisterResponse(BaseModel):
    """Deliberately the ONLY response shape for /register, regardless
    of which of the three duplicate-email branches ran (ARCHITECTURE.md
    §8.1 Step 2: "all three branches return the identical response
    body" — non-enumerating by construction, not by convention)."""

    message: str = "If the submitted information is valid, a verification code has been sent."
