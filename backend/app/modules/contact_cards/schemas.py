"""Response shapes for card resolution — ARCHITECTURE.md §9.3.

"The same token resolves differently depending on who asks." Each
consumer gets its own explicit schema (§21.4: "a model gaining a
sensitive column does not silently start leaking it"):

- `PublicCard` — the networking card (§9.3.1). There is NO phone field,
  NO consent field and NO id field to fill, and exactly one `contact`.
- `CardIdentity` — what staff see: name and profession only (§24.20).
  It is its own class, never derived from `PublicCard`, so widening the
  public card cannot widen it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

ContactType = Literal["LINKEDIN", "EMAIL"]


class _Strict(BaseModel):
    # An output schema that cannot carry an unlisted field even if a
    # caller tries to pass one.
    model_config = ConfigDict(extra="forbid")


class ContactAction(_Strict):
    type: ContactType
    value: str


class CardInterest(_Strict):
    slug: str
    name: str


class PublicCard(_Strict):
    first_name: str
    last_name: str
    profession: str
    interests: list[CardInterest]
    # Beyond Now (migration 0021): the contact action opens ONLY to a signed-in
    # attendee connected with the card owner by the mutual QR scan; everyone
    # else gets None, so the public card is no way round the contact gate.
    contact: ContactAction | None = None
    # The attendee's profile photo, or None (the screen then shows the robot).
    photo_url: str | None = None


class CardIdentity(_Strict):
    """`POST /api/staff/scan/resolve` — exactly three identity fields.
    M10's `scan_ref` is added by `staff.schemas.StaffScanResult`, a
    subclass defined in the staff module rather than a field added
    here, so this shared schema itself never widens (§24.20)."""

    first_name: str
    last_name: str
    profession: str


class OwnCard(_Strict):
    """`GET /api/attendees/me/card` — the signed-in attendee's QR target
    and a preview of exactly what a scanner of it will see.

    `card_url` is the whole QR payload (§9.2): the application origin +
    `/c/` + the opaque token, and nothing else."""

    card_url: str
    card: PublicCard
    # Six characters to type when the QR cannot be read (service.card_short_code).
    short_code: str
