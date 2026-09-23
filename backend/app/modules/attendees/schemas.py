"""Request/response schemas for the registration endpoint —
ARCHITECTURE.md §8.1, §12.2, §21.4.

Every mutating schema sets `extra="forbid"` (invariant #16): an
unexpected field is a 422, not a silently ignored value.
"""

from __future__ import annotations

from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


def _validate_linkedin_url(value: str) -> str:
    """ARCHITECTURE.md §8.1: "LinkedIn URL validated for scheme and
    host shape." Loose by design — this rejects garbage, not
    legitimate LinkedIn URL variations (country subdomains, trailing
    slashes, query strings)."""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or "linkedin.com" not in parsed.netloc.lower():
        raise ValueError("linkedin_url must be an http(s) linkedin.com URL")
    return value


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_name: str = Field(min_length=1, max_length=200)
    last_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    phone: str = Field(min_length=1, max_length=50)
    profession: str = Field(min_length=1, max_length=200)
    linkedin_url: str | None = Field(default=None, max_length=2048)
    interest_slugs: list[str] = Field(default_factory=list)
    # Required, no default (ARCHITECTURE.md §8.1: "a required explicit
    # boolean with no default; absence is a validation error").
    sponsor_consent: bool

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


class RegisterResponse(BaseModel):
    """Deliberately the ONLY response shape for /register, regardless
    of which of the three duplicate-email branches ran (ARCHITECTURE.md
    §8.1 Step 2: "all three branches return the identical response
    body" — non-enumerating by construction, not by convention)."""

    message: str = "If the submitted information is valid, a verification code has been sent."
