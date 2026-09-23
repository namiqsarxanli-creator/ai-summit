"""Public booth response shapes — ARCHITECTURE.md §11.7, §10.

Explicit output schemas (§21.4): `is_active` and `allow_repeat_task_points`
are internal configuration, never returned to an attendee.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BoothOut(_Strict):
    """`GET /api/booths` list entry and the `booth` object inside a
    recommendation (§11.7's exact shape: slug, name, location_label)."""

    slug: str
    name: str
    location_label: str | None


class BoothTask(_Strict):
    """A descriptive activity at the booth (§7.9: "tasks exist so the
    booth detail screen can tell attendees what to do") — never a point
    value, which is a staff-side preset allow-list, not part of a
    task's own description, and not owned by M08."""

    title: str
    description: str | None


class BoothDetail(_Strict):
    """`GET /api/booths/{slug}` — the list shape plus the description
    and task list IMPLEMENTATION_PLAN.md M08's booth detail screen
    needs ("booth detail with tasks and location")."""

    slug: str
    name: str
    description: str | None
    location_label: str | None
    tasks: list[BoothTask]
