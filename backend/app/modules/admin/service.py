"""M16 admin operations. Call mutators only inside the fenced unit of work."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import (
    ColumnElement,
    DateTime,
    Integer,
    Select,
    Text,
    cast,
    func,
    literal,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.orm import Session, aliased, undefer

from app.core.auth_session import AuthSession
from app.core.deps import current_admin
from app.core.errors import DomainError
from app.core.ratelimit import RateLimit, limiter
from app.core.security import hash_password
from app.core.sessions import resolve_staff_session
from app.modules.admin.models import AdminAudit, EventConfig
from app.modules.admin.pagination import decode_cursor, encode_cursor, filter_fingerprint
from app.modules.admin.schemas import (
    AttendeeFilters,
    AttendeeListQuery,
    AttendeePage,
    AttendeeRow,
    BoothEdit,
    BoothWrite,
    ConfigEdit,
    InterestEdit,
    InterestWrite,
    PointsFilters,
    PointsLogPage,
    PointsLogQuery,
    PointsLogRow,
    PresetEdit,
    PresetWrite,
    StaffCreate,
    TaskEdit,
    TaskWrite,
    WeightWrite,
)
from app.modules.attendees.models import User, UserStatus
from app.modules.booths.models import Booth, BoothInterest, Task, TaskPresetPoint
from app.modules.interests.models import Interest
from app.modules.points.models import PointsEntryType, PointsLog, PointsSource
from app.modules.staff.models import StaffAccount, StaffRole


class AdminInvalid(DomainError):
    code = "ADMIN_INVALID"
    status_code = 400


class AdminNotFound(DomainError):
    code = "ADMIN_NOT_FOUND"
    status_code = 404


class AdminConflict(DomainError):
    code = "ADMIN_CONFLICT"
    status_code = 409


# §21.9 requires admin throttling without prescribing a numeric limit.
ADMIN_PER_ACCOUNT = RateLimit("admin:account", 120, 60)
AdminResult = tuple[str, str, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]
AdminOperation = Callable[[Session, StaffAccount], AdminResult]


def authenticate_admin(db: Session, token: str | None) -> StaffAccount:
    staff = current_admin(resolve_staff_session(db, token))
    limiter.check([(ADMIN_PER_ACCOUNT, str(staff.id))])
    return staff


def execute(db: Session, token: str | None, operation: AdminOperation) -> dict[str, Any]:
    """Run after the caller has acquired the reset fence; audit shares its commit."""
    actor = authenticate_admin(db, token)
    action, target_type, before, after, response = operation(db, actor)
    db.add(
        AdminAudit(
            staff_account_id=actor.id,
            action=action,
            target_type=target_type,
            target_id=str(response["id"]) if "id" in response else None,
            before_value=before,
            after_value=after,
        )
    )
    db.flush()
    return response


def required(db: Session, model: type[Any], row_id: int) -> Any:
    row = db.get(model, row_id)
    if row is None:
        raise AdminNotFound("No such record.")
    return row


def staff_view(row: StaffAccount) -> dict[str, Any]:
    return {
        "id": row.id,
        "email": row.email,
        "full_name": row.full_name,
        "role": row.role.value,
        "booth_id": row.booth_id,
        "is_active": row.is_active,
    }


def booth_view(row: Booth) -> dict[str, Any]:
    return {
        "id": row.id,
        "slug": row.slug,
        "name": row.name,
        "description": row.description,
        "location_label": row.location_label,
        "is_active": row.is_active,
        "allow_repeat_task_points": row.allow_repeat_task_points,
    }


def interest_view(row: Interest) -> dict[str, Any]:
    return {
        "id": row.id,
        "slug": row.slug,
        "name": row.name,
        "sort_order": row.sort_order,
        "is_active": row.is_active,
    }


def task_view(row: Task) -> dict[str, Any]:
    return {
        "id": row.id,
        "booth_id": row.booth_id,
        "title": row.title,
        "description": row.description,
        "sort_order": row.sort_order,
        "is_active": row.is_active,
        "points": row.points,
        # None = inherits the booth's, then the event's, setting.
        "allow_repeat_points": row.allow_repeat_points,
    }


def task_rows(db: Session) -> list[dict[str, Any]]:
    """Admin activity list, with the repeat policy each activity RESOLVES to
    so the admin sees the effective rule, not only the override."""
    from app.modules.points.service import resolve_repeat_policy

    event_default = db.execute(
        select(EventConfig.allow_repeat_task_points_default).where(EventConfig.id == 1)
    ).scalar_one()
    booths = {b.id: b.allow_repeat_task_points for b in db.execute(select(Booth)).scalars()}
    rows = db.execute(
        select(Task)
        .options(undefer(Task.points), undefer(Task.allow_repeat_points))
        .order_by(Task.id)
    ).scalars()
    out = []
    for row in rows:
        view = task_view(row)
        view["repeat_allowed_effective"] = resolve_repeat_policy(
            task=row.allow_repeat_points,
            booth=booths.get(row.booth_id),
            event_default=event_default,
        )
        out.append(view)
    return out


def preset_view(row: TaskPresetPoint) -> dict[str, Any]:
    return {
        "id": row.id,
        "booth_id": row.booth_id,
        "points": row.points,
        "label": row.label,
        "sort_order": row.sort_order,
        "is_active": row.is_active,
    }


def config_view(row: EventConfig) -> dict[str, Any]:
    return {
        "feedback_points": row.feedback_points,
        "leaderboard_top_n": row.leaderboard_top_n,
        "allow_repeat_task_points_default": row.allow_repeat_task_points_default,
        "competition_mode": row.competition_mode,
        "reminders_enabled": row.reminders_enabled,
    }


def create_staff(db: Session, actor: StaffAccount, payload: StaffCreate) -> AdminResult:
    if (payload.role == StaffRole.BOOTH_STAFF) != (payload.booth_id is not None):
        # A volunteer (migration 0020) has no booth: only granted scopes.
        raise AdminInvalid("Booth staff need a booth; admins and volunteers cannot own one.")
    if payload.booth_id is not None:
        booth = required(db, Booth, payload.booth_id)
        if not booth.is_active:
            raise AdminInvalid("The booth is inactive.")
    row = StaffAccount(
        email=payload.email.strip().lower(),
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        role=payload.role,
        booth_id=payload.booth_id,
        is_active=True,
    )
    db.add(row)
    db.flush()
    view = staff_view(row)
    return "staff.create", "staff_account", None, view, view


def deactivate_staff(db: Session, actor: StaffAccount, staff_id: int) -> AdminResult:
    if staff_id == actor.id:
        raise AdminInvalid("You cannot deactivate your own admin account.")
    row = required(db, StaffAccount, staff_id)
    before = staff_view(row)
    row.is_active = False
    db.execute(
        update(AuthSession)
        .where(AuthSession.staff_account_id == staff_id)
        .values(revoked_at=func.now())
    )
    db.flush()
    after = staff_view(row)
    return "staff.deactivate", "staff_account", before, after, after


def create_booth(db: Session, actor: StaffAccount, payload: BoothWrite) -> AdminResult:
    row = Booth(**payload.model_dump())
    db.add(row)
    db.flush()
    view = booth_view(row)
    return "booth.create", "booth", None, view, view


def edit_booth(db: Session, actor: StaffAccount, booth_id: int, payload: BoothEdit) -> AdminResult:
    row = required(db, Booth, booth_id)
    before = booth_view(row)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.flush()
    after = booth_view(row)
    return "booth.update", "booth", before, after, after


def create_interest(db: Session, actor: StaffAccount, payload: InterestWrite) -> AdminResult:
    row = Interest(**payload.model_dump())
    db.add(row)
    db.flush()
    view = interest_view(row)
    return "interest.create", "interest", None, view, view


def edit_interest(
    db: Session, actor: StaffAccount, interest_id: int, payload: InterestEdit
) -> AdminResult:
    row = required(db, Interest, interest_id)
    before = interest_view(row)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.flush()
    after = interest_view(row)
    return "interest.update", "interest", before, after, after


def set_weight(
    db: Session, actor: StaffAccount, booth_id: int, interest_id: int, payload: WeightWrite
) -> AdminResult:
    required(db, Booth, booth_id)
    required(db, Interest, interest_id)
    row = db.get(BoothInterest, (booth_id, interest_id))
    before = {"weight": str(row.weight)} if row is not None else None
    if row is None:
        row = BoothInterest(booth_id=booth_id, interest_id=interest_id, weight=payload.weight)
        db.add(row)
    else:
        row.weight = payload.weight
    db.flush()
    after: dict[str, Any] = {"weight": str(row.weight)}
    after.update(
        {"id": f"{booth_id}:{interest_id}", "booth_id": booth_id, "interest_id": interest_id}
    )
    return "booth.weight", "booth_interest", before, after, after


def remove_weight(db: Session, actor: StaffAccount, booth_id: int, interest_id: int) -> AdminResult:
    row = db.get(BoothInterest, (booth_id, interest_id))
    if row is None:
        raise AdminNotFound("No such booth interest weight.")
    before: dict[str, Any] = {
        "id": f"{booth_id}:{interest_id}",
        "booth_id": booth_id,
        "interest_id": interest_id,
        "weight": str(row.weight),
    }
    db.delete(row)
    db.flush()
    return (
        "booth.weight.remove",
        "booth_interest",
        before,
        None,
        {
            "id": f"{booth_id}:{interest_id}",
            "removed": True,
        },
    )


def create_task(db: Session, actor: StaffAccount, payload: TaskWrite) -> AdminResult:
    required(db, Booth, payload.booth_id)
    row = Task(**payload.model_dump())
    db.add(row)
    db.flush()
    view = task_view(row)
    return "task.create", "task", None, view, view


def edit_task(db: Session, actor: StaffAccount, task_id: int, payload: TaskEdit) -> AdminResult:
    row = required(db, Task, task_id)
    before = task_view(row)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.flush()
    after = task_view(row)
    return "task.update", "task", before, after, after


def create_preset(db: Session, actor: StaffAccount, payload: PresetWrite) -> AdminResult:
    if payload.booth_id is not None:
        required(db, Booth, payload.booth_id)
    row = TaskPresetPoint(**payload.model_dump())
    db.add(row)
    db.flush()
    view = preset_view(row)
    return "preset.create", "task_preset_point", None, view, view


def edit_preset(
    db: Session, actor: StaffAccount, preset_id: int, payload: PresetEdit
) -> AdminResult:
    row = required(db, TaskPresetPoint, preset_id)
    before = preset_view(row)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.flush()
    after = preset_view(row)
    return "preset.update", "task_preset_point", before, after, after


def edit_config(db: Session, actor: StaffAccount, payload: ConfigEdit) -> AdminResult:
    row = required(db, EventConfig, 1)
    before = config_view(row)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    row.updated_by_staff_id = actor.id
    db.flush()
    after = config_view(row)
    response = {"id": 1, **after}
    return "event_config.update", "event_config", before, after, response


def booth_rows(db: Session) -> list[dict[str, Any]]:
    booths = db.execute(select(Booth).order_by(Booth.id)).scalars()
    return [
        {
            **booth_view(b),
            "weights": [
                {"interest_id": w.interest_id, "weight": str(w.weight)}
                for w in db.execute(
                    select(BoothInterest)
                    .where(BoothInterest.booth_id == b.id)
                    .order_by(BoothInterest.interest_id)
                ).scalars()
            ],
        }
        for b in booths
    ]


def _escape_like(word: str) -> str:
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def attendee_clauses(f: AttendeeFilters) -> list[ColumnElement[bool]]:
    """The WHERE conditions of the attendee search and filters. The screen's
    list and the CSV export both build their query from this, so a filter can
    never mean one thing on screen and another in the file.

    Search: every whitespace-separated word of `q` must occur (case-insensitive,
    as a substring) in the first name, last name or e-mail; `%` and `_` in the
    search text are literal. Filters combine with AND."""
    clauses: list[ColumnElement[bool]] = []
    if f.q:
        for word in f.q.split():
            pattern = f"%{_escape_like(word)}%"
            clauses.append(
                or_(
                    User.first_name.ilike(pattern, escape="\\"),
                    User.last_name.ilike(pattern, escape="\\"),
                    cast(User.email, Text).ilike(pattern, escape="\\"),
                )
            )
    if f.status is not None:
        clauses.append(User.status == UserStatus(f.status))
    if f.sponsor_consent is not None:
        clauses.append(User.sponsor_consent.is_(f.sponsor_consent))
    if f.registered_from is not None:
        clauses.append(User.created_at >= f.registered_from)
    if f.registered_to is not None:
        clauses.append(User.created_at < f.registered_to)
    return clauses


def attendee_page(db: Session, query: AttendeeListQuery) -> AttendeePage:
    """Attendees, newest registration first, keyset-paginated.

    Search: every whitespace-separated word of `q` must occur (case-insensitive,
    as a substring) in the first name, last name or e-mail; `%` and `_` in the
    search text are literal. Filters combine with AND. Needs no extension, so it
    scans the matching rows: fine for an event-sized table, and the keyset
    condition keeps each page's cost independent of how deep it is."""
    filters = {
        "q": query.q,
        "status": query.status,
        "sponsor_consent": query.sponsor_consent,
        "registered_from": query.registered_from,
        "registered_to": query.registered_to,
    }
    fingerprint = filter_fingerprint("attendees", filters)
    stmt = select(
        User.id,
        User.first_name,
        User.last_name,
        User.email,
        User.status,
        User.sponsor_consent,
        User.created_at,
    )
    for clause in attendee_clauses(query):
        stmt = stmt.where(clause)
    if query.cursor is not None:
        at, row_id = decode_cursor("attendees", query.cursor, fingerprint=fingerprint)
        stmt = stmt.where(
            tuple_(User.created_at, User.id)
            < tuple_(literal(at, DateTime(timezone=True)), literal(row_id, Integer))
        )
    rows = db.execute(
        stmt.order_by(User.created_at.desc(), User.id.desc()).limit(query.limit + 1)
    ).all()
    page, more = rows[: query.limit], len(rows) > query.limit
    next_cursor = (
        encode_cursor(
            "attendees", created_at=page[-1].created_at, row_id=page[-1].id, fingerprint=fingerprint
        )
        if more
        else None
    )
    return AttendeePage(
        items=[
            AttendeeRow(
                id=r.id,
                first_name=r.first_name,
                last_name=r.last_name,
                email=str(r.email),
                status=r.status.value,
                sponsor_consent=r.sponsor_consent,
                created_at=r.created_at,
            )
            for r in page
        ],
        next_cursor=next_cursor,
        limit=query.limit,
    )


def points_statement() -> Select[*tuple[Any, ...]]:
    """The ledger rows with the names joined in for reading. Outer joins: a
    row whose activity or staff account is gone still lists. A reversal and the
    entry it reverses are linked both ways: `reverses_points_log_id` on the
    REVERSAL, `reversed_by_id` (the reversal's id, through the unique
    `uq_points_reverses`) on the original."""
    reversal = aliased(PointsLog)
    return (
        select(
            PointsLog.id,
            PointsLog.user_id,
            User.first_name,
            User.last_name,
            PointsLog.booth_id,
            Booth.name.label("booth_name"),
            PointsLog.source,
            PointsLog.entry_type,
            PointsLog.points,
            PointsLog.task_id,
            Task.title.label("task_title"),
            PointsLog.entered_by_staff_id,
            StaffAccount.full_name.label("staff_name"),
            PointsLog.entered_by_system,
            PointsLog.created_at,
            PointsLog.reverses_points_log_id,
            reversal.id.label("reversed_by_id"),
            PointsLog.note,
        )
        .join(User, User.id == PointsLog.user_id)
        .outerjoin(reversal, reversal.reverses_points_log_id == PointsLog.id)
        .outerjoin(Booth, Booth.id == PointsLog.booth_id)
        .outerjoin(Task, Task.id == PointsLog.task_id)
        .outerjoin(StaffAccount, StaffAccount.id == PointsLog.entered_by_staff_id)
    )


def reversal_reason(entry_type: str, note: str | None) -> str | None:
    """The reason of a REVERSAL (kept in `note`); an AWARD has none to show."""
    return note if entry_type == "REVERSAL" else None


def points_clauses(f: PointsFilters) -> list[ColumnElement[bool]]:
    """The WHERE conditions of the ledger filters; shared by the list and the
    CSV export (see `attendee_clauses`)."""
    clauses: list[ColumnElement[bool]] = []
    if f.user_id is not None:
        clauses.append(PointsLog.user_id == f.user_id)
    if f.booth_id is not None:
        clauses.append(PointsLog.booth_id == f.booth_id)
    if f.task_id is not None:
        clauses.append(PointsLog.task_id == f.task_id)
    if f.staff_id is not None:
        clauses.append(PointsLog.entered_by_staff_id == f.staff_id)
    if f.source is not None:
        clauses.append(PointsLog.source == PointsSource(f.source))
    if f.entry_type is not None:
        clauses.append(PointsLog.entry_type == PointsEntryType(f.entry_type))
    if f.created_from is not None:
        clauses.append(PointsLog.created_at >= f.created_from)
    if f.created_to is not None:
        clauses.append(PointsLog.created_at < f.created_to)
    return clauses


def points_page(db: Session, query: PointsLogQuery) -> PointsLogPage:
    """The points ledger, newest entry first, keyset-paginated. Read-only: no
    ledger row is ever changed. Names are joined in for reading; a row whose
    activity or staff account is gone still lists (outer joins)."""
    filters = {
        "user_id": query.user_id,
        "booth_id": query.booth_id,
        "task_id": query.task_id,
        "staff_id": query.staff_id,
        "source": query.source,
        "entry_type": query.entry_type,
        "created_from": query.created_from,
        "created_to": query.created_to,
    }
    fingerprint = filter_fingerprint("points-log", filters)
    stmt = points_statement()
    for clause in points_clauses(query):
        stmt = stmt.where(clause)
    if query.cursor is not None:
        at, row_id = decode_cursor("points-log", query.cursor, fingerprint=fingerprint)
        stmt = stmt.where(
            tuple_(PointsLog.created_at, PointsLog.id)
            < tuple_(literal(at, DateTime(timezone=True)), literal(row_id, Integer))
        )
    rows = db.execute(
        stmt.order_by(PointsLog.created_at.desc(), PointsLog.id.desc()).limit(query.limit + 1)
    ).all()
    page, more = rows[: query.limit], len(rows) > query.limit
    next_cursor = (
        encode_cursor(
            "points-log",
            created_at=page[-1].created_at,
            row_id=page[-1].id,
            fingerprint=fingerprint,
        )
        if more
        else None
    )
    return PointsLogPage(
        items=[
            PointsLogRow(
                id=r.id,
                user_id=r.user_id,
                attendee_name=f"{r.first_name} {r.last_name}",
                booth_id=r.booth_id,
                booth_name=r.booth_name,
                source=r.source.value,
                entry_type=r.entry_type.value,
                points=r.points,
                task_id=r.task_id,
                activity=_activity_label(
                    r.source.value,
                    r.task_id,
                    {r.task_id: r.task_title} if r.task_id is not None and r.task_title else {},
                ),
                entered_by_staff_id=r.entered_by_staff_id,
                staff_name=r.staff_name,
                entered_by_system=r.entered_by_system,
                created_at=r.created_at,
                reverses_entry_id=r.reverses_points_log_id,
                reversed_by_entry_id=r.reversed_by_id,
                reason=reversal_reason(r.entry_type.value, r.note),
            )
            for r in page
        ],
        next_cursor=next_cursor,
        limit=query.limit,
    )


def _activity_label(source: str, task_id: int | None, titles: dict[int, str]) -> str:
    """What the ledger shows in the Activity column. Rows from before
    activities existed have no task_id and are labelled as what they are,
    never attached to an invented activity."""
    if task_id is not None:
        return titles.get(task_id, f"Activity #{task_id}")
    if source == "FEEDBACK":
        return "Feedback reward"
    return "Booth-level award (before activities)"
