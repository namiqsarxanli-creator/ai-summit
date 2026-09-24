"""M16 admin operations. Call mutators only inside the fenced unit of work."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.auth_session import AuthSession
from app.core.deps import current_admin
from app.core.errors import DomainError
from app.core.ratelimit import RateLimit, limiter
from app.core.security import hash_password
from app.core.sessions import resolve_staff_session
from app.modules.admin.models import AdminAudit, EventConfig
from app.modules.admin.schemas import (
    BoothEdit,
    BoothWrite,
    ConfigEdit,
    InterestEdit,
    InterestWrite,
    PresetEdit,
    PresetWrite,
    StaffCreate,
    TaskEdit,
    TaskWrite,
    WeightWrite,
)
from app.modules.attendees.models import User
from app.modules.booths.models import Booth, BoothInterest, Task, TaskPresetPoint
from app.modules.interests.models import Interest
from app.modules.points.models import PointsLog
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
    }


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
    }


def create_staff(db: Session, actor: StaffAccount, payload: StaffCreate) -> AdminResult:
    if (payload.role == StaffRole.ADMIN) != (payload.booth_id is None):
        raise AdminInvalid("Booth staff need a booth; admins cannot own a booth.")
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


def attendee_rows(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(select(User).order_by(User.id).limit(500)).scalars()
    return [
        {
            "id": r.id,
            "first_name": r.first_name,
            "last_name": r.last_name,
            "email": r.email,
            "status": r.status.value,
        }
        for r in rows
    ]


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


def points_rows(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        select(PointsLog).order_by(PointsLog.created_at.desc(), PointsLog.id.desc()).limit(200)
    ).scalars()
    return [
        {
            "id": r.id,
            "user_id": r.user_id,
            "booth_id": r.booth_id,
            "source": r.source.value,
            "entry_type": r.entry_type.value,
            "points": r.points,
            "entered_by_staff_id": r.entered_by_staff_id,
            "entered_by_system": r.entered_by_system,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
