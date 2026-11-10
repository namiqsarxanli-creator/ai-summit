"""Request/response schemas for the OTP request/verify endpoints —
ARCHITECTURE.md §8.2. Every mutating schema sets `extra="forbid"`
(invariant #16)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.languages import DEFAULT_LANGUAGE, RequestLanguage


class OtpRequestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    # Language of the code email: az / en / ru. Optional for older clients.
    language: RequestLanguage = DEFAULT_LANGUAGE


class OtpRequestResponse(BaseModel):
    """A code was sent (an address with no account gets EMAIL_NOT_REGISTERED)."""

    message: str = "A verification code has been sent."


class OtpVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    remember_device: bool = Field(default=False, strict=True)


class OtpVerifyResponse(BaseModel):
    status: str = "ACTIVE"
