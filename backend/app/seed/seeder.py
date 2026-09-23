"""Turns app.seed.demo_fixtures into rows — IMPLEMENTATION_PLAN.md M03.

Idempotency strategy: every write is a natural-key
``INSERT ... ON CONFLICT (<natural key>) DO NOTHING``, so a second run
against an already-seeded database inserts zero rows and raises
nothing — "safe to re-run" (M03 scope) without needing a separate
read-then-write branch anywhere. ``tasks`` is the one exception: the
schema gives it no natural-key constraint (only the surrogate-id-based
``uq_tasks_id_booth_id``, which exists solely to support
``points_log``'s composite FK — see app/modules/booths/models.py), so
task idempotency is an explicit existence check instead of a database
constraint.

Not wrapped in ``mutating_transaction`` (ARCHITECTURE.md §19.4). That
helper's own docstring scopes it to reset-sensitive mutations "from M04
onward", and its coverage table enumerates request-time paths only
(task award, feedback, registration, login, ...). An offline seed run
against a freshly migrated, not-yet-serving database is the same
category of operation as the M15 reset CLI itself, which also owns its
own connection rather than going through this helper.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert as sa_insert
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import Insert as PgInsert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.core.security import hash_password
from app.modules.admin.models import EventConfig
from app.modules.attendees.models import User, UserInterest, UserStatus
from app.modules.booths.models import Booth, BoothInterest, Task, TaskPresetPoint
from app.modules.contact_cards.models import ContactCard
from app.modules.interests.models import Interest
from app.modules.staff.models import StaffAccount
from app.seed import demo_fixtures as fx


@dataclass(frozen=True)
class SeedResult:
    """How many rows THIS call actually inserted, per entity — zero
    across the board on a re-run against an already-seeded database."""

    interests_created: int
    booths_created: int
    booth_interests_created: int
    tasks_created: int
    preset_points_created: int
    staff_accounts_created: int
    attendees_created: int
    contact_cards_created: int
    event_config_created: bool


def seed_all(db: Session, *, admin_password: str, staff_password: str) -> SeedResult:
    """Seeds the full demo catalogue. Commits on success; the caller
    decides what to do on an exception (this function does not catch
    or roll back)."""
    if not admin_password:
        raise ValueError("admin_password must not be empty")
    if not staff_password:
        raise ValueError("staff_password must not be empty")

    interests_created = _seed_interests(db)
    interest_ids = _slug_id_map(db, Interest)

    booths_created = _seed_booths(db)
    booth_ids = _slug_id_map(db, Booth)

    booth_interests_created = _seed_booth_interests(db, booth_ids, interest_ids)
    tasks_created = _seed_tasks(db, booth_ids)
    preset_points_created = _seed_preset_points(db)
    staff_accounts_created = _seed_staff(db, booth_ids, admin_password, staff_password)
    attendees_created = _seed_attendees(db)
    user_ids = _email_id_map(db, User)
    _seed_user_interests(db, user_ids, interest_ids)
    contact_cards_created = _seed_contact_cards(db, user_ids)
    event_config_created = _seed_event_config(db)

    db.commit()

    return SeedResult(
        interests_created=interests_created,
        booths_created=booths_created,
        booth_interests_created=booth_interests_created,
        tasks_created=tasks_created,
        preset_points_created=preset_points_created,
        staff_accounts_created=staff_accounts_created,
        attendees_created=attendees_created,
        contact_cards_created=contact_cards_created,
        event_config_created=event_config_created,
    )


def _execute_on_conflict_do_nothing(
    db: Session, stmt: PgInsert, pk_column: InstrumentedAttribute[Any]
) -> int:
    """Runs an ON CONFLICT DO NOTHING insert and returns how many rows
    it actually inserted.

    Not `result.rowcount`: PostgreSQL's ON CONFLICT DO NOTHING makes
    the driver-reported rowcount unreliable (observed as -1 here, per
    DBAPI convention for "not determined", rather than 0 for a skipped
    conflict). `RETURNING` only yields the rows PostgreSQL actually
    inserted — a skipped conflict returns nothing — so counting the
    result set is exact regardless of driver rowcount behaviour.
    """
    return len(db.execute(stmt.returning(pk_column)).all())


def _slug_id_map(db: Session, model: type[Interest] | type[Booth]) -> dict[str, int]:
    rows = db.execute(select(model.slug, model.id)).all()
    return {slug: id_ for slug, id_ in rows}


def _email_id_map(db: Session, model: type[User]) -> dict[str, int]:
    rows = db.execute(select(model.email, model.id)).all()
    return {email: id_ for email, id_ in rows}


def _seed_interests(db: Session) -> int:
    created = 0
    for i in fx.INTERESTS:
        stmt = (
            pg_insert(Interest)
            .values(slug=i.slug, name=i.name, sort_order=i.sort_order)
            .on_conflict_do_nothing(index_elements=["slug"])
        )
        created += _execute_on_conflict_do_nothing(db, stmt, Interest.id)
    return created


def _seed_booths(db: Session) -> int:
    created = 0
    for b in fx.BOOTHS:
        stmt = (
            pg_insert(Booth)
            .values(
                slug=b.slug,
                name=b.name,
                description=b.description,
                location_label=b.location_label,
            )
            .on_conflict_do_nothing(index_elements=["slug"])
        )
        created += _execute_on_conflict_do_nothing(db, stmt, Booth.id)
    return created


def _seed_booth_interests(
    db: Session, booth_ids: dict[str, int], interest_ids: dict[str, int]
) -> int:
    created = 0
    for b in fx.BOOTHS:
        for bi in b.interests:
            stmt = (
                pg_insert(BoothInterest)
                .values(
                    booth_id=booth_ids[b.slug],
                    interest_id=interest_ids[bi.interest_slug],
                    weight=bi.weight,
                )
                .on_conflict_do_nothing(index_elements=["booth_id", "interest_id"])
            )
            created += _execute_on_conflict_do_nothing(db, stmt, BoothInterest.booth_id)
    return created


def _seed_tasks(db: Session, booth_ids: dict[str, int]) -> int:
    """No natural-key constraint exists on `tasks` (see module
    docstring) — idempotency is an explicit existence check rather
    than ON CONFLICT."""
    created = 0
    for b in fx.BOOTHS:
        booth_id = booth_ids[b.slug]
        for t in b.tasks:
            exists = db.execute(
                select(Task.id).where(Task.booth_id == booth_id, Task.title == t.title)
            ).first()
            if exists is not None:
                continue
            db.execute(
                sa_insert(Task).values(
                    booth_id=booth_id,
                    title=t.title,
                    description=t.description,
                    sort_order=t.sort_order,
                )
            )
            created += 1
    return created


def _seed_preset_points(db: Session) -> int:
    created = 0
    for p in fx.PRESET_POINTS:
        stmt = (
            pg_insert(TaskPresetPoint)
            .values(booth_id=None, points=p.points, label=p.label, sort_order=p.sort_order)
            .on_conflict_do_nothing(index_elements=["points"], index_where=text("booth_id IS NULL"))
        )
        created += _execute_on_conflict_do_nothing(db, stmt, TaskPresetPoint.id)
    return created


def _seed_staff(
    db: Session, booth_ids: dict[str, int], admin_password: str, staff_password: str
) -> int:
    created = 0
    for s in fx.STAFF:
        password = admin_password if s.booth_slug is None else staff_password
        stmt = (
            pg_insert(StaffAccount)
            .values(
                email=s.email,
                password_hash=hash_password(password),
                full_name=s.full_name,
                role=s.role,
                booth_id=booth_ids[s.booth_slug] if s.booth_slug is not None else None,
            )
            .on_conflict_do_nothing(index_elements=["email"])
        )
        created += _execute_on_conflict_do_nothing(db, stmt, StaffAccount.id)
    return created


def _seed_attendees(db: Session) -> int:
    created = 0
    now = datetime.now(UTC)
    for a in fx.ATTENDEES:
        stmt = (
            pg_insert(User)
            .values(
                first_name=a.first_name,
                last_name=a.last_name,
                email=a.email,
                phone=a.phone,
                profession=a.profession,
                sponsor_consent=False,
                status=UserStatus.ACTIVE,
                activated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["email"])
        )
        created += _execute_on_conflict_do_nothing(db, stmt, User.id)
    return created


def _seed_user_interests(
    db: Session, user_ids: dict[str, int], interest_ids: dict[str, int]
) -> int:
    created = 0
    for a in fx.ATTENDEES:
        user_id = user_ids[a.email]
        for slug in a.interest_slugs:
            stmt = (
                pg_insert(UserInterest)
                .values(user_id=user_id, interest_id=interest_ids[slug])
                .on_conflict_do_nothing(index_elements=["user_id", "interest_id"])
            )
            created += _execute_on_conflict_do_nothing(db, stmt, UserInterest.user_id)
    return created


def _seed_contact_cards(db: Session, user_ids: dict[str, int]) -> int:
    created = 0
    now = datetime.now(UTC)
    for a in fx.ATTENDEES:
        stmt = (
            pg_insert(ContactCard)
            .values(
                user_id=user_ids[a.email],
                public_token=a.public_token,
                issued_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["user_id"], index_where=text("revoked_at IS NULL")
            )
        )
        created += _execute_on_conflict_do_nothing(db, stmt, ContactCard.id)
    return created


def _seed_event_config(db: Session) -> bool:
    stmt = (
        pg_insert(EventConfig)
        .values(**fx.EVENT_CONFIG_DEFAULTS)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    return bool(_execute_on_conflict_do_nothing(db, stmt, EventConfig.id))
