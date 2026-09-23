"""Staff request schemas — ARCHITECTURE.md §13.1, §21.4."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ScanResolveRequest(BaseModel):
    """`POST /api/staff/scan/resolve { card_token }` (§13.1 step 4).
    `extra="forbid"` (invariant #16). No length or pattern constraint on
    purpose: a malformed token must reach the one INVALID_QR branch
    (§9.4), not a differently-shaped VALIDATION_ERROR."""

    model_config = ConfigDict(extra="forbid")

    card_token: str
