"""Minimal admin API. Mutations resolve authority only after the reset fence."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.deps import CurrentAdmin, SessionToken
from app.db.session import DbSession, mutating_transaction
from app.modules.admin import service as admin
from app.modules.admin.models import EventConfig
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
from app.modules.auth.otp_delivery import recent_demo_otps
from app.modules.booths.models import Task, TaskPresetPoint
from app.modules.interests.models import Interest
from app.modules.points.service import maintenance_fast_path_check
from app.modules.staff.models import StaffAccount

router = APIRouter(prefix="/admin", tags=["admin"])


def _write(db: Session, token: str | None, operation: admin.AdminOperation) -> dict[str, Any]:
    maintenance_fast_path_check(db)
    try:
        with mutating_transaction(db) as tx:
            return admin.execute(tx, token, operation)
    except IntegrityError as err:
        sqlstate = getattr(err.orig, "sqlstate", None)
        if sqlstate == "23505":
            raise admin.AdminConflict("A record with that value already exists.") from err
        if sqlstate in {"23503", "23514", "23502"}:
            raise admin.AdminInvalid("That change violates a data constraint.") from err
        raise


def _read(admin_user: StaffAccount) -> None:
    from app.core.ratelimit import limiter
    from app.modules.admin.service import ADMIN_PER_ACCOUNT

    limiter.check([(ADMIN_PER_ACCOUNT, str(admin_user.id))])


@router.get("/attendees")
def attendees(admin_user: CurrentAdmin, db: DbSession) -> list[dict[str, Any]]:
    _read(admin_user)
    return admin.attendee_rows(db)


@router.get("/booths")
def booths(admin_user: CurrentAdmin, db: DbSession) -> list[dict[str, Any]]:
    _read(admin_user)
    return admin.booth_rows(db)


@router.get("/interests")
def interests(admin_user: CurrentAdmin, db: DbSession) -> list[dict[str, Any]]:
    _read(admin_user)
    return [
        admin.interest_view(r) for r in db.execute(select(Interest).order_by(Interest.id)).scalars()
    ]


@router.get("/staff")
def staff(admin_user: CurrentAdmin, db: DbSession) -> list[dict[str, Any]]:
    _read(admin_user)
    return [
        admin.staff_view(r)
        for r in db.execute(select(StaffAccount).order_by(StaffAccount.id)).scalars()
    ]


@router.get("/tasks")
def tasks(admin_user: CurrentAdmin, db: DbSession) -> list[dict[str, Any]]:
    _read(admin_user)
    return [admin.task_view(r) for r in db.execute(select(Task).order_by(Task.id)).scalars()]


@router.get("/presets")
def presets(admin_user: CurrentAdmin, db: DbSession) -> list[dict[str, Any]]:
    _read(admin_user)
    return [
        admin.preset_view(r)
        for r in db.execute(select(TaskPresetPoint).order_by(TaskPresetPoint.id)).scalars()
    ]


@router.get("/event-config")
def event_config(admin_user: CurrentAdmin, db: DbSession) -> dict[str, Any]:
    _read(admin_user)
    return admin.config_view(admin.required(db, EventConfig, 1))


@router.get("/points-log")
def points_log(admin_user: CurrentAdmin, db: DbSession) -> list[dict[str, Any]]:
    _read(admin_user)
    return admin.points_rows(db)


@router.get("/demo-otps")
def demo_otps(admin_user: CurrentAdmin, response: Response) -> list[dict[str, str]]:
    _read(admin_user)
    if not get_settings().demo_mode:
        raise admin.AdminNotFound("Demo OTP visibility is unavailable.")
    response.headers["Cache-Control"] = "no-store"
    return recent_demo_otps()


@router.post("/staff", status_code=201)
def create_staff(payload: StaffCreate, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.create_staff(tx, actor, payload))


@router.post("/staff/{staff_id}/deactivate")
def deactivate_staff(staff_id: int, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.deactivate_staff(tx, actor, staff_id))


@router.post("/booths", status_code=201)
def create_booth(payload: BoothWrite, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.create_booth(tx, actor, payload))


@router.patch("/booths/{booth_id}")
def edit_booth(
    booth_id: int, payload: BoothEdit, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.edit_booth(tx, actor, booth_id, payload))


@router.post("/interests", status_code=201)
def create_interest(payload: InterestWrite, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.create_interest(tx, actor, payload))


@router.patch("/interests/{interest_id}")
def edit_interest(
    interest_id: int, payload: InterestEdit, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.edit_interest(tx, actor, interest_id, payload))


@router.put("/booths/{booth_id}/weights/{interest_id}")
def set_weight(
    booth_id: int, interest_id: int, payload: WeightWrite, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _write(
        db, token, lambda tx, actor: admin.set_weight(tx, actor, booth_id, interest_id, payload)
    )


@router.delete("/booths/{booth_id}/weights/{interest_id}")
def remove_weight(
    booth_id: int, interest_id: int, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _write(
        db, token, lambda tx, actor: admin.remove_weight(tx, actor, booth_id, interest_id)
    )


@router.post("/tasks", status_code=201)
def create_task(payload: TaskWrite, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.create_task(tx, actor, payload))


@router.patch("/tasks/{task_id}")
def edit_task(
    task_id: int, payload: TaskEdit, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.edit_task(tx, actor, task_id, payload))


@router.post("/presets", status_code=201)
def create_preset(payload: PresetWrite, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.create_preset(tx, actor, payload))


@router.patch("/presets/{preset_id}")
def edit_preset(
    preset_id: int, payload: PresetEdit, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.edit_preset(tx, actor, preset_id, payload))


@router.patch("/event-config")
def edit_config(payload: ConfigEdit, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _write(db, token, lambda tx, actor: admin.edit_config(tx, actor, payload))
