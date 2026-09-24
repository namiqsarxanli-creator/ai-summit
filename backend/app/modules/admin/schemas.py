"""Explicit M16 admin request/response contracts. No ledger write contract exists."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.modules.staff.models import StaffRole


class Mutation(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StaffCreate(Mutation):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    full_name: str = Field(min_length=1, max_length=200)
    role: StaffRole
    booth_id: int | None = None


class BoothWrite(Mutation):
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    location_label: str | None = None
    is_active: bool = True
    allow_repeat_task_points: bool | None = None


class BoothEdit(Mutation):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    location_label: str | None = None
    is_active: bool | None = None
    allow_repeat_task_points: bool | None = None


class InterestWrite(Mutation):
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=200)
    sort_order: int = 0
    is_active: bool = True


class InterestEdit(Mutation):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    sort_order: int | None = None
    is_active: bool | None = None


class WeightWrite(Mutation):
    weight: Decimal = Field(gt=0, le=1, max_digits=3, decimal_places=2)


class TaskWrite(Mutation):
    booth_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    sort_order: int = 0
    is_active: bool = True


class TaskEdit(Mutation):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class PresetWrite(Mutation):
    booth_id: int | None = Field(default=None, gt=0)
    points: int = Field(ge=1, le=100)
    label: str | None = Field(default=None, max_length=100)
    sort_order: int = 0
    is_active: bool = True


class PresetEdit(Mutation):
    points: int | None = Field(default=None, ge=1, le=100)
    label: str | None = Field(default=None, max_length=100)
    sort_order: int | None = None
    is_active: bool | None = None


class ConfigEdit(Mutation):
    feedback_points: int | None = Field(default=None, ge=1, le=100)
    leaderboard_top_n: int | None = Field(default=None, ge=1, le=100)
    allow_repeat_task_points_default: bool | None = None
