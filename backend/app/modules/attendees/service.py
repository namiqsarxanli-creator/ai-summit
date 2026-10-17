"""Registration, activation and OTP-challenge mechanics that
`otp_challenges`, `users`, `user_interests`, `consent_audit` and
`contact_cards` are the natural home for — ARCHITECTURE.md §8.1, §8.2.

Every function here takes an already-fenced session (the caller opened
`mutating_transaction`) — see app/db/session.py's docstring: "the ONLY
sanctioned entry point for a reset-sensitive mutation." Nothing in this
module opens its own transaction or acquires the fence itself.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import UnknownInterestError
from app.core.images import LARGE, SMALL
from app.core.languages import DEFAULT_LANGUAGE
from app.core.media import avatar_url
from app.core.security import (
    canonical_json,
    generate_card_token,
    generate_otp_code,
    hash_otp_code,
    hash_session_token,
    verify_secret,
)
from app.core.sessions import create_attendee_session
from app.modules.attendees.interest_card import InterestCard, card_of, ensure_known
from app.modules.attendees.models import (
    ConsentAudit,
    OtpChallenge,
    OtpPurpose,
    User,
    UserInterest,
    UserStatus,
)
from app.modules.attendees.schemas import (
    AttendeeProfile,
    PhotoUrls,
    ProfileInterest,
    RegisterRequest,
)
from app.modules.auth.otp_outbox import enqueue as enqueue_otp_delivery
from app.modules.contact_cards.models import ContactCard
from app.modules.interests.models import Interest

OTP_EXPIRY_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
PROVISIONAL_TTL_HOURS = 24

# No consent-text versioning system exists yet (out of scope for M04) —
# a single fixed literal identifies "the consent text in force when
# this milestone shipped", which is enough to make the users.
# sponsor_consent_version / consent_audit.consent_version columns
# genuinely auditable rather than merely present.
CONSENT_VERSION = "registration-v1"


@dataclass(frozen=True)
class PendingOtpDelivery:
    """An OTP challenge's plaintext code, held only long enough to hand
    to the delivery adapter AFTER the transaction that created it has
    committed (ARCHITECTURE.md invariant #12's principle — never
    signal/deliver something that might still roll back — applied here
    to OTP delivery, not just realtime events). Never persisted; never
    logged by this module itself."""

    email: str
    purpose: OtpPurpose
    code: str
    # The language the attendee asked in (validated at the API edge); the
    # email is written in it.
    language: str = DEFAULT_LANGUAGE
    # True when `issue_otp` already wrote the durable outbox row (in the
    # same transaction as the challenge): the worker will send the email,
    # so `deliver_otp` must not send it a second time.
    queued: bool = False


@dataclass(frozen=True)
class VerifyOutcome:
    """Deliberately NOT raised as an exception from inside the fenced
    transaction: a wrong guess must still commit its `attempt_count`
    increment (ARCHITECTURE.md §8.2), so failure here is a normal
    return value, not a rollback trigger. The caller (the router) turns
    `ok=False` into `OtpInvalidError` only AFTER the transaction has
    already committed."""

    ok: bool
    user: User | None = None
    session_token: str | None = None


def _resolve_interest_ids(db: Session, slugs: list[str]) -> list[int]:
    unique_slugs = set(slugs)
    if not unique_slugs:
        return []
    rows = db.execute(
        select(Interest.id, Interest.slug).where(
            Interest.slug.in_(unique_slugs), Interest.is_active.is_(True)
        )
    ).all()
    found = {slug: interest_id for interest_id, slug in rows}
    missing = unique_slugs - found.keys()
    if missing:
        raise UnknownInterestError(f"Unknown interest slug(s): {', '.join(sorted(missing))}")
    return list(found.values())


def _supersede_live_challenges(db: Session, email: str, purpose: OtpPurpose) -> None:
    """ARCHITECTURE.md §8.2: "Requesting a new code marks prior
    unconsumed challenges for that email as consumed." Also what keeps
    the partial unique index `(email, purpose) WHERE consumed_at IS
    NULL` satisfiable — without this, issuing a second challenge before
    the first is consumed would violate it."""
    db.execute(
        update(OtpChallenge)
        .where(
            OtpChallenge.email == email,
            OtpChallenge.purpose == purpose,
            OtpChallenge.consumed_at.is_(None),
        )
        .values(consumed_at=datetime.now(UTC))
    )


def issue_otp(
    db: Session,
    *,
    email: str,
    user_id: int | None,
    purpose: OtpPurpose,
    language: str = DEFAULT_LANGUAGE,
    registration_intent_hash: str | None = None,
    registration_snapshot: dict[str, object] | None = None,
) -> PendingOtpDelivery:
    _supersede_live_challenges(db, email, purpose)
    code = generate_otp_code()
    challenge = OtpChallenge(
        email=email,
        user_id=user_id,
        purpose=purpose,
        code_hash=hash_otp_code(code),
        expires_at=datetime.now(UTC) + timedelta(minutes=OTP_EXPIRY_MINUTES),
        registration_intent_hash=registration_intent_hash,
        registration_snapshot=registration_snapshot,
    )
    db.add(challenge)
    db.flush()

    # The outbox row joins THIS transaction: if the caller rolls back, the
    # challenge and its email vanish together; once it commits, the email
    # can no longer be lost. Only when the outbox is configured (see
    # Settings.outbox_enabled); otherwise the legacy in-process sender runs.
    settings = get_settings()
    queued = False
    if settings.outbox_enabled:
        enqueue_otp_delivery(
            db, challenge=challenge, code=code, language=language, settings=settings
        )
        queued = True
    return PendingOtpDelivery(
        email=email, purpose=purpose, code=code, language=language, queued=queued
    )


def hash_registration_intent(intent: str) -> str:
    """SHA-256 of the browser-held registration intent (see OtpChallenge).
    The intent is 256 bits of CSPRNG output, so a fast hash is enough."""
    return hash_session_token(intent)


def _registration_snapshot(payload: RegisterRequest, interest_ids: list[int]) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "first_name": payload.first_name,
        "last_name": payload.last_name,
        "phone": payload.phone,
        "profession": payload.profession,
        "linkedin_url": payload.linkedin_url,
        "interest_ids": sorted(set(interest_ids)),
        "sponsor_consent": payload.sponsor_consent,
        "company": payload.company,
        "interest_card": payload.interest_card.model_dump(),
    }
    # A digest of the submission, so tests and audits can tell two apart
    # without comparing personal data field by field.
    snapshot["digest"] = hashlib.sha256(canonical_json(dict(snapshot))).hexdigest()
    return snapshot


def _write_company_and_card(user: User, company: str | None, card: InterestCard) -> None:
    user.company = company
    user.card_sector = card.sector
    user.card_topics = list(card.topics)
    user.card_seeking = list(card.seeking)
    user.card_offering = list(card.offering)


def _register_new(
    db: Session,
    email: str,
    payload: RegisterRequest,
    interest_ids: list[int],
    intent_hash: str | None = None,
) -> PendingOtpDelivery | None:
    """Returns None if a concurrent request won the race to register
    this exact brand-new email first — ARCHITECTURE.md §8.1: "citext
    uniqueness enforced by the database (never a 500)". A SAVEPOINT
    keeps that expected race from aborting the whole fenced
    transaction (the same pattern §15 uses for the feedback-reward
    SAVEPOINT)."""
    savepoint = db.begin_nested()
    user = User(
        first_name=payload.first_name,
        last_name=payload.last_name,
        email=email,
        phone=payload.phone,
        profession=payload.profession,
        linkedin_url=payload.linkedin_url,
        sponsor_consent=payload.sponsor_consent,
        sponsor_consent_at=datetime.now(UTC) if payload.sponsor_consent else None,
        sponsor_consent_version=CONSENT_VERSION if payload.sponsor_consent else None,
        status=UserStatus.PROVISIONAL,
    )
    _write_company_and_card(user, payload.company, payload.interest_card)
    db.add(user)
    try:
        db.flush()
    except IntegrityError as err:
        savepoint.rollback()
        diag = getattr(err.orig, "diag", None)
        constraint_name = getattr(diag, "constraint_name", None)
        if constraint_name == "uq_users_email":
            return None
        raise
    else:
        savepoint.commit()

    db.add_all([UserInterest(user_id=user.id, interest_id=iid) for iid in interest_ids])
    db.add(
        ConsentAudit(
            user_id=user.id,
            granted=payload.sponsor_consent,
            consent_version=CONSENT_VERSION,
            source="registration",
        )
    )
    db.add(
        ContactCard(
            user_id=user.id,
            public_token=generate_card_token(),
            issued_at=datetime.now(UTC),
        )
    )
    db.flush()

    return issue_otp(
        db,
        email=email,
        user_id=user.id,
        purpose=OtpPurpose.ACTIVATION,
        language=payload.language,
        registration_intent_hash=intent_hash,
        registration_snapshot=_registration_snapshot(payload, interest_ids),
    )


def _reregister_provisional(
    db: Session,
    existing: User,
    payload: RegisterRequest,
    interest_ids: list[int],
    intent_hash: str | None = None,
) -> PendingOtpDelivery:
    """ARCHITECTURE.md §8.1 Step 2: "Overwrite the provisional profile
    with the new submission and send a fresh OTP. The unverified data
    has no standing." Activation later applies only the submission whose
    intent the verifying browser holds (`_bind_activation`), so this
    overwrite cannot decide what the owner ends up activating."""
    existing.first_name = payload.first_name
    existing.last_name = payload.last_name
    existing.phone = payload.phone
    existing.profession = payload.profession
    existing.linkedin_url = payload.linkedin_url
    existing.sponsor_consent = payload.sponsor_consent
    existing.sponsor_consent_at = datetime.now(UTC) if payload.sponsor_consent else None
    existing.sponsor_consent_version = CONSENT_VERSION if payload.sponsor_consent else None
    _write_company_and_card(existing, payload.company, payload.interest_card)

    db.execute(delete(UserInterest).where(UserInterest.user_id == existing.id))
    db.add_all([UserInterest(user_id=existing.id, interest_id=iid) for iid in interest_ids])
    db.add(
        ConsentAudit(
            user_id=existing.id,
            granted=payload.sponsor_consent,
            consent_version=CONSENT_VERSION,
            source="registration",
        )
    )

    has_live_card = db.execute(
        select(ContactCard.id).where(
            ContactCard.user_id == existing.id, ContactCard.revoked_at.is_(None)
        )
    ).first()
    if has_live_card is None:
        db.add(
            ContactCard(
                user_id=existing.id,
                public_token=generate_card_token(),
                issued_at=datetime.now(UTC),
            )
        )

    db.flush()
    return issue_otp(
        db,
        email=existing.email,
        user_id=existing.id,
        purpose=OtpPurpose.ACTIVATION,
        language=payload.language,
        registration_intent_hash=intent_hash,
        registration_snapshot=_registration_snapshot(payload, interest_ids),
    )


def register_attendee(
    db: Session, payload: RegisterRequest, intent_hash: str | None = None
) -> PendingOtpDelivery | None:
    """The three-branch registration flow, ARCHITECTURE.md §8.1 Step 2.
    Interests are resolved BEFORE branching on email state — a bad
    interest slug fails identically regardless of whether the email is
    new, provisional, or active, which is what keeps `UNKNOWN_INTEREST`
    from itself becoming a membership oracle."""
    email = payload.email.strip().lower()
    interest_ids = _resolve_interest_ids(db, payload.interest_slugs)
    # The card's catalogue check, likewise before branching. Registration has
    # nothing stored yet, so only the current catalogue is accepted.
    ensure_known(payload.interest_card)

    existing = db.execute(
        select(User).where(User.email == email).with_for_update()
    ).scalar_one_or_none()

    if existing is None:
        return _register_new(db, email, payload, interest_ids, intent_hash)
    if existing.status == UserStatus.PROVISIONAL:
        return _reregister_provisional(db, existing, payload, interest_ids, intent_hash)
    # ACTIVE: create nothing, submitted fields discarded, send a LOGIN
    # OTP instead of an ACTIVATION one (§8.1 Step 2).
    return issue_otp(
        db, email=email, user_id=existing.id, purpose=OtpPurpose.LOGIN, language=payload.language
    )


def _bind_activation(db: Session, user: User, intent: str | None, now: datetime) -> None:
    """Proof of email ownership activates only what the verifying browser
    itself submitted.

    A PROVISIONAL row can be re-submitted by anyone who knows the address
    (§8.1 Step 2), and every re-submission mails the owner a fresh code.
    Entering that code proves the inbox, not which submission it belongs
    to. So the profile, interests and sponsor consent are taken from the
    snapshot of the submission whose intent this browser presents. With no
    matching snapshot (another device, cleared cookies, or an activation
    code requested without registering) the profile already on the row is
    kept, but sponsor consent is switched off: it is never granted on
    someone else's say-so. The attendee can opt in from their profile."""
    snapshot: dict[str, object] | None = None
    if intent:
        snapshot = db.execute(
            select(OtpChallenge.registration_snapshot)
            .where(
                OtpChallenge.user_id == user.id,
                OtpChallenge.purpose == OtpPurpose.ACTIVATION,
                OtpChallenge.registration_intent_hash == hash_registration_intent(intent),
                OtpChallenge.registration_snapshot.is_not(None),
            )
            .order_by(OtpChallenge.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    consent = False
    if snapshot is not None:
        user.first_name = str(snapshot["first_name"])
        user.last_name = str(snapshot["last_name"])
        user.phone = str(snapshot["phone"])
        user.profession = str(snapshot["profession"])
        linkedin = snapshot.get("linkedin_url")
        user.linkedin_url = str(linkedin) if linkedin is not None else None
        raw_ids = snapshot.get("interest_ids")
        wanted = {int(i) for i in raw_ids} if isinstance(raw_ids, list) else set()
        # Interests deactivated since the submission are simply dropped.
        active = (
            set(
                db.execute(
                    select(Interest.id).where(Interest.id.in_(wanted), Interest.is_active.is_(True))
                ).scalars()
            )
            if wanted
            else set()
        )
        db.execute(delete(UserInterest).where(UserInterest.user_id == user.id))
        db.add_all([UserInterest(user_id=user.id, interest_id=iid) for iid in sorted(active)])
        consent = snapshot.get("sponsor_consent") is True
        # Snapshots written before migration 0016 have neither key: keep the row's.
        if "company" in snapshot or "interest_card" in snapshot:
            raw_company = snapshot.get("company")
            raw_card = snapshot.get("interest_card")
            card = (
                InterestCard.model_validate(raw_card)
                if isinstance(raw_card, dict)
                else InterestCard()
            )
            _write_company_and_card(
                user, str(raw_company) if isinstance(raw_company, str) else None, card
            )

    if user.sponsor_consent != consent:
        user.sponsor_consent = consent
        user.sponsor_consent_at = now if consent else None
        user.sponsor_consent_version = CONSENT_VERSION if consent else None
        db.add(
            ConsentAudit(
                user_id=user.id,
                granted=consent,
                consent_version=CONSENT_VERSION,
                source="activation",
            )
        )
    db.flush()


def verify_otp(
    db: Session,
    *,
    email: str,
    code: str,
    remember_device: bool = False,
    registration_intent: str | None = None,
) -> VerifyOutcome:
    """ARCHITECTURE.md §8.2 verification, completing either §8.1 Step 3
    (activation) or the returning-attendee login (M05).

    The purpose is decided by the SERVER from the attendee's state,
    never supplied by the client: a PROVISIONAL attendee can only
    consume an ACTIVATION challenge, an ACTIVE one only a LOGIN
    challenge. That is what §8.2 means by "verification matches on
    purpose as well as value" — an activation code can never be
    replayed as a login code, or vice versa. An unknown email fails
    exactly like a wrong code (OTP_INVALID), so this is no oracle.
    """
    normalized_email = email.strip().lower()

    user = db.execute(select(User).where(User.email == normalized_email)).scalar_one_or_none()
    if user is None:
        return VerifyOutcome(ok=False)
    purpose = OtpPurpose.ACTIVATION if user.status == UserStatus.PROVISIONAL else OtpPurpose.LOGIN

    challenge = db.execute(
        select(OtpChallenge)
        .where(
            OtpChallenge.email == normalized_email,
            OtpChallenge.user_id == user.id,
            OtpChallenge.purpose == purpose,
            OtpChallenge.consumed_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()

    if challenge is None:
        return VerifyOutcome(ok=False)

    now = datetime.now(UTC)
    if challenge.expires_at <= now or challenge.attempt_count >= OTP_MAX_ATTEMPTS:
        return VerifyOutcome(ok=False)

    if not verify_secret(challenge.code_hash, code):
        db.execute(
            update(OtpChallenge)
            .where(OtpChallenge.id == challenge.id, OtpChallenge.consumed_at.is_(None))
            .values(attempt_count=OtpChallenge.attempt_count + 1)
        )
        return VerifyOutcome(ok=False)

    consumed = db.execute(
        update(OtpChallenge)
        .where(OtpChallenge.id == challenge.id, OtpChallenge.consumed_at.is_(None))
        .values(consumed_at=now)
        .returning(OtpChallenge.id)
    ).first()
    if consumed is None:
        # Lost a concurrent race: another transaction consumed this
        # exact challenge between our SELECT ... FOR UPDATE and here.
        # Structurally shouldn't happen (FOR UPDATE should have
        # blocked the other transaction until we committed), kept as a
        # defence-in-depth check matching ARCHITECTURE.md §8.2's
        # described pattern exactly.
        return VerifyOutcome(ok=False)

    if purpose == OtpPurpose.ACTIVATION:
        _bind_activation(db, user, registration_intent, now)
        user.status = UserStatus.ACTIVE
        user.activated_at = now
        db.flush()
        # A card completed at registration earns PROFILE now that the e-mail is
        # proven (competition/profile.py); never before activation.
        from app.modules.competition.profile import maybe_award_profile

        maybe_award_profile(db, user, now)
        # "Activate the contact card": the card issued at registration
        # becomes resolvable purely because users.status is now ACTIVE —
        # card resolution (M06) gates on that via a join, so no row in
        # contact_cards itself needs to change here.

    raw_token = create_attendee_session(
        db, user_id=user.id, now=now, remember_device=remember_device
    )
    return VerifyOutcome(ok=True, user=user, session_token=raw_token)


def own_photo_urls(key: str | None) -> PhotoUrls | None:
    small = avatar_url(key, SMALL)
    large = avatar_url(key, LARGE)
    if small is None or large is None:
        return None
    return PhotoUrls(small=small, large=large)


def attendee_profile(db: Session, user: User) -> AttendeeProfile:
    """`GET /api/attendees/me/profile` (M08) — the ONE exception to this
    module's "every function takes an already-fenced session" rule: a
    pure read, so it takes NO fence (§19.4 exempts pure reads). `user`
    comes from the caller's already-resolved session; this never
    resolves an identity of its own.

    Interests are NOT filtered by `Interest.is_active` — an attendee's
    own historical selection stays visible on their profile even if
    that interest is later retired from the catalogue, the same choice
    M07's matching explanation makes for the same reason."""
    rows = db.execute(
        select(Interest.slug, Interest.name)
        .join(UserInterest, UserInterest.interest_id == Interest.id)
        .where(UserInterest.user_id == user.id)
        .order_by(Interest.sort_order, Interest.name)
    ).all()
    return AttendeeProfile(
        first_name=user.first_name,
        last_name=user.last_name,
        email=user.email,
        phone=user.phone,
        profession=user.profession,
        linkedin_url=user.linkedin_url,
        sponsor_consent=user.sponsor_consent,
        sponsor_consent_at=user.sponsor_consent_at,
        sponsor_consent_version=user.sponsor_consent_version,
        interests=[ProfileInterest(slug=slug, name=name) for slug, name in rows],
        company=user.company,
        interest_card=card_of(
            user.card_sector, user.card_topics, user.card_seeking, user.card_offering
        ),
        version=user.profile_version,
        photo=own_photo_urls(user.photo_key),
    )


def purge_provisional(db: Session) -> int:
    """ARCHITECTURE.md §8.1: "PROVISIONAL users older than 24h are
    purged, together with their interests, contact card and
    challenges... Because they hold a UNIQUE email, leaving them
    forever would permanently block a genuine owner from registering
    that address." Idempotent: a second run with nothing stale simply
    deletes zero rows.

    Deletes children before parents explicitly — none of
    contact_cards/otp_challenges/consent_audit declare ON DELETE
    CASCADE on their users FK (only user_interests does), so relying on
    cascade here would fail with a foreign-key violation.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=PROVISIONAL_TTL_HOURS)
    stale_ids = [
        row[0]
        for row in db.execute(
            select(User.id).where(User.status == UserStatus.PROVISIONAL, User.created_at < cutoff)
        ).all()
    ]
    if not stale_ids:
        return 0

    db.execute(delete(ConsentAudit).where(ConsentAudit.user_id.in_(stale_ids)))
    db.execute(delete(OtpChallenge).where(OtpChallenge.user_id.in_(stale_ids)))
    db.execute(delete(ContactCard).where(ContactCard.user_id.in_(stale_ids)))
    db.execute(delete(UserInterest).where(UserInterest.user_id.in_(stale_ids)))
    db.execute(delete(User).where(User.id.in_(stale_ids)))
    return len(stale_ids)
