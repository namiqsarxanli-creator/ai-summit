"""Staff request/response schemas — ARCHITECTURE.md §12.1, §13.1, §21.4."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.core.languages import DEFAULT_LANGUAGE, RequestLanguage
from app.modules.contact_cards.schemas import CardIdentity
from app.modules.staff.initial_admin import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
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


class _NewPassword(BaseModel):
    """A new password, typed twice. The length rule is the one the console
    applies when it creates an account (`initial_admin`). The text is never
    trimmed or altered, and never echoed back in an error (the app's validation
    handler returns only the location and the rule)."""

    model_config = ConfigDict(extra="forbid")

    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    new_password_confirmation: str = Field(max_length=MAX_PASSWORD_LENGTH)

    @model_validator(mode="after")
    def _typed_twice_the_same(self) -> _NewPassword:
        if self.new_password != self.new_password_confirmation:
            raise ValueError("the two new passwords are not the same")
        return self


class StaffPasswordChange(_NewPassword):
    """`POST /api/staff/auth/password`: the CURRENT password proves it is the
    account's owner at the keyboard, not just someone holding a session."""

    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class StaffPasswordResetRequest(BaseModel):
    """`POST /api/staff/auth/password-reset/request`. Language of the email."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    language: RequestLanguage = DEFAULT_LANGUAGE


class StaffPasswordResetRequestResponse(BaseModel):
    """The same body whether or not the address belongs to a staff account."""

    message: str = (
        "If this address belongs to an active staff account, a reset code has been sent to it."
    )


class StaffPasswordResetConfirm(_NewPassword):
    """`POST /api/staff/auth/password-reset/confirm`: the emailed code and the
    new password."""

    email: EmailStr
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class StaffPasswordResetStart(BaseModel):
    """`POST /api/admin/staff/{id}/password-reset`: only the language of the
    email. There is deliberately no field for a password or a code: the
    administrator can trigger the letter, nothing else."""

    model_config = ConfigDict(extra="forbid")

    language: RequestLanguage = DEFAULT_LANGUAGE


class StaffMe(BaseModel):
    """`GET /api/staff/me` — "returning booth name so staff always see
    which booth they are awarding for" (M09). `booth_name` is `None`
    for an ADMIN, who has no booth (§7.7's CHECK constraint). No id, no
    email, no password state, no booth id — only what the panel needs
    to display (§21.4)."""

    full_name: str
    role: StaffRole
    booth_name: str | None
    # Offline scan queue (competition/offline.py): an opaque, stable label of THIS
    # account (a keyed hash; it reveals no id and authenticates nothing) and the
    # dataset the scans belong to. A phone files queued scans under both and never
    # replays them under another account or into another dataset.
    queue_owner: str
    dataset_generation: int


class PresetOut(BaseModel):
    """One entry in `GET /api/staff/points/presets` (§13.2, §7.10)."""

    points: int
    label: str | None


class StaffTaskOut(BaseModel):
    """One awardable activity in `GET /api/staff/tasks`: what the staff
    member sees after a scan. `points` is the server-side value the award
    will carry - display only; the award request cannot send one."""

    id: int
    title: str
    description: str | None
    points: int
    # True when one attendee may earn this activity more than once
    # (resolved server-side from activity -> booth -> event default).
    repeatable: bool
    # Opaque server-issued revision of this rule (points + repeat policy).
    # The scanner sends it back with the award as "I confirmed this rule".
    rule_revision: str


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
