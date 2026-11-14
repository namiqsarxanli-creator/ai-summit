"""The attendee's OWN profile: edit details, sponsor consent, photo.

Every function takes an already-fenced session (the router opened
`mutating_transaction`) and the raw session cookie. Who is edited is the
session's attendee - there is no user id anywhere in a request, so there is no
way to name someone else, and no e-mail field to change.

Three rules shape this module:

* **One writer at a time, and a stale form never wins.** The attendee row is
  locked (`FOR UPDATE`) before anything is compared or changed. A profile edit
  carries the `version` it was loaded at; if it is no longer current, NOTHING is
  saved and the current profile comes back with the error
  (`PROFILE_CONFLICT`), so two devices cannot silently overwrite each other.
* **Saving what is already saved is not an event.** An edit that changes
  nothing writes nothing and does not bump the version; a consent request for
  the value already in force writes no audit row. The audit trail therefore
  holds real decisions only.
* **A change and its audit are one transaction.** The consent value, its
  timestamp/version and its `consent_audit` row are written together or not at
  all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.errors import ProfileConflictError, UnauthenticatedError, UnknownInterestError
from app.core.ratelimit import (
    CONSENT_UPDATE_PER_ATTENDEE,
    PROFILE_UPDATE_PER_ATTENDEE,
    limiter,
)
from app.core.sessions import resolve_attendee_session
from app.modules.attendees.interest_card import card_of, ensure_known
from app.modules.attendees.models import ConsentAudit, User, UserInterest, UserStatus
from app.modules.attendees.schemas import AttendeeProfile, ConsentState, ProfileUpdate
from app.modules.attendees.service import CONSENT_VERSION, attendee_profile
from app.modules.interests.models import Interest

CONSENT_SOURCE_SETTINGS = "settings_update"


@dataclass(frozen=True)
class ProfileOutcome:
    """Both flags False = the edit matched what was saved (nothing written). The
    flags tell the router which signals are worth publishing after commit:
    details (name, profession...) matter to the leaderboard, interests to the
    attendee's own recommendations."""

    profile: AttendeeProfile
    details_changed: bool
    interests_changed: bool
    user_id: int
    # The company or the interest card: private, so not leaderboard-relevant.
    card_changed: bool = False
    # The save completed the card and earned the one-time PROFILE points.
    points_awarded: bool = False

    @property
    def changed(self) -> bool:
        return self.details_changed or self.interests_changed or self.card_changed


@dataclass(frozen=True)
class ConsentOutcome:
    state: ConsentState
    user_id: int


def lock_attendee(db: Session, raw_session_token: str | None) -> User:
    """The signed-in attendee, authenticated INSIDE the fence and then locked.

    `populate_existing` makes the returned row the locked, current one even if
    the session had already loaded an older copy."""
    attendee = resolve_attendee_session(db, raw_session_token)
    locked = db.execute(
        select(User)
        .where(User.id == attendee.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if locked.status != UserStatus.ACTIVE:
        raise UnauthenticatedError("Please sign in.")
    return locked


def _resolve_interests(db: Session, slugs: list[str], current_ids: set[int]) -> set[int]:
    """The interest ids an edit may select: the ACTIVE catalogue, plus whatever
    the attendee already has (an interest retired after they chose it can stay
    until they remove it, but can never be newly picked). Anything else names
    the offending slugs in the error."""
    if not slugs:
        return set()
    rows = db.execute(
        select(Interest.id, Interest.slug, Interest.is_active).where(Interest.slug.in_(slugs))
    ).all()
    by_slug = {slug: (interest_id, active) for interest_id, slug, active in rows}
    refused = [
        slug
        for slug in slugs
        if slug not in by_slug or (not by_slug[slug][1] and by_slug[slug][0] not in current_ids)
    ]
    if refused:
        raise UnknownInterestError(
            "Some interests are unknown or no longer available.", unknown=refused
        )
    return {by_slug[slug][0] for slug in slugs}


def update_profile(
    db: Session, *, raw_session_token: str | None, payload: ProfileUpdate
) -> ProfileOutcome:
    user = lock_attendee(db, raw_session_token)
    limiter.check([(PROFILE_UPDATE_PER_ATTENDEE, str(user.id))])

    if payload.expected_version != user.profile_version:
        raise ProfileConflictError(attendee_profile(db, user).model_dump(mode="json"))

    current_ids = set(
        db.execute(select(UserInterest.interest_id).where(UserInterest.user_id == user.id))
        .scalars()
        .all()
    )
    wanted_ids = _resolve_interests(db, payload.interest_slugs, current_ids)

    details_changed = (
        user.first_name != payload.first_name
        or user.last_name != payload.last_name
        or user.phone != payload.phone
        or user.profession != payload.profession
        or user.linkedin_url != payload.linkedin_url
    )
    interests_changed = wanted_ids != current_ids

    # Left out by a client that predates them (a cached page): kept as stored.
    sent = payload.model_fields_set
    company = payload.company if "company" in sent else user.company
    card = payload.interest_card if "interest_card" in sent else None
    if card is not None:
        # A choice retired from the catalogue may stay (or go) if it is already
        # stored in that group; nothing unknown can be newly added.
        stored = card_of(user.card_sector, user.card_topics, user.card_seeking, user.card_offering)
        ensure_known(card, stored)
    card_changed = company != user.company or (
        card is not None
        and (
            card.sector != user.card_sector
            or list(card.topics) != list(user.card_topics)
            or list(card.seeking) != list(user.card_seeking)
            or list(card.offering) != list(user.card_offering)
        )
    )
    if not details_changed and not interests_changed and not card_changed:
        return ProfileOutcome(attendee_profile(db, user), False, False, user.id)

    user.first_name = payload.first_name
    user.last_name = payload.last_name
    user.phone = payload.phone
    user.profession = payload.profession
    user.linkedin_url = payload.linkedin_url
    user.company = company
    if card is not None:
        user.card_sector = card.sector
        user.card_topics = list(card.topics)
        user.card_seeking = list(card.seeking)
        user.card_offering = list(card.offering)
    if interests_changed:
        removed = current_ids - wanted_ids
        if removed:
            db.execute(
                delete(UserInterest).where(
                    UserInterest.user_id == user.id, UserInterest.interest_id.in_(removed)
                )
            )
        db.add_all(
            [UserInterest(user_id=user.id, interest_id=iid) for iid in wanted_ids - current_ids]
        )
    user.profile_version = user.profile_version + 1
    db.flush()
    from app.modules.competition.profile import maybe_award_profile

    profile_award = maybe_award_profile(db, user, datetime.now(UTC))
    return ProfileOutcome(
        attendee_profile(db, user),
        details_changed,
        interests_changed,
        user.id,
        card_changed,
        points_awarded=profile_award is not None and profile_award.paid,
    )


def set_sponsor_consent(
    db: Session, *, raw_session_token: str | None, granted: bool
) -> ConsentOutcome:
    user = lock_attendee(db, raw_session_token)
    limiter.check([(CONSENT_UPDATE_PER_ATTENDEE, str(user.id))])

    if user.sponsor_consent == granted:
        # Already the value in force: no write, no audit row, no new timestamp.
        return ConsentOutcome(_consent_state(user, changed=False), user.id)

    now = datetime.now(UTC)
    user.sponsor_consent = granted
    user.sponsor_consent_at = now if granted else None
    user.sponsor_consent_version = CONSENT_VERSION if granted else None
    db.add(
        ConsentAudit(
            user_id=user.id,
            granted=granted,
            consent_version=CONSENT_VERSION,
            changed_at=now,
            source=CONSENT_SOURCE_SETTINGS,
        )
    )
    db.flush()
    return ConsentOutcome(_consent_state(user, changed=True), user.id)


def _consent_state(user: User, *, changed: bool) -> ConsentState:
    return ConsentState(
        sponsor_consent=user.sponsor_consent,
        sponsor_consent_at=user.sponsor_consent_at,
        consent_version=user.sponsor_consent_version,
        changed=changed,
    )
