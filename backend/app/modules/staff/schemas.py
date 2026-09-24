"""Staff request/response schemas — ARCHITECTURE.md §12.1, §13.1, §21.4."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.modules.contact_cards.schemas import CardIdentity
from app.modules.staff.models import StaffRole


class ScanResolveRequest(BaseModel):
    """`POST /api/staff/scan/resolve { card_token }` (§13.1 step 4).
    `extra="forbid"` (invariant #16). No length or pattern constraint on
    purpose: a malformed token must reach the one INVALID_QR branch
    (§9.4), not a differently-shaped VALIDATION_ERROR."""

    model_config = ConfigDict(extra="forbid")

    card_token: str


class StaffLoginRequest(BaseModel):
    """`POST /api/staff/auth/login` (§12.1). `extra="forbid"`
    (invariant #16) — deliberately absent: `booth_id`, `role`,
    `staff_id`, anything that could assert an identity or authority
    rather than merely prove one. No length bound tight enough to
    reject a legitimate password, since §12.1 never distinguishes a
    malformed attempt from a wrong one."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class StaffMe(BaseModel):
    """`GET /api/staff/me` — "returning booth name so staff always see
    which booth they are awarding for" (M09). `booth_name` is `None`
    for an ADMIN, who has no booth (§7.7's CHECK constraint). No id, no
    email, no password state, no booth id — only what the panel needs
    to display (§21.4)."""

    full_name: str
    role: StaffRole
    booth_name: str | None


class PresetOut(BaseModel):
    """One entry in `GET /api/staff/points/presets` (§13.2, §7.10)."""

    points: int
    label: str | None


class StaffScanResult(CardIdentity):
    """`POST /api/staff/scan/resolve` response (M10 addition, §13.1
    steps 4-5, §13.3). Extends `CardIdentity` rather than restating its
    three identity fields — `CardIdentity` itself (§24.20's "staff card
    resolution returns name and profession only") stays exactly those
    three fields; `scan_ref` is orthogonal transport for the
    confirm/award flow, not a fourth identity field, which is why it is
    added here in the staff module rather than widening the shared
    `contact_cards` schema."""

    scan_ref: str
