"""Request/response schemas for the OTP request/verify endpoints —
ARCHITECTURE.md §8.2. Every mutating schema sets `extra="forbid"`
(invariant #16)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class OtpRequestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class OtpRequestResponse(BaseModel):
    """Same body whether or not the email is registered
    (ARCHITECTURE.md §8.2 enumeration resistance)."""

    message: str = "If this email is registered, a verification code has been sent."


class OtpVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class OtpVerifyResponse(BaseModel):
    status: str = "ACTIVE"
