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


class BoothDetail(_Strict):
    """`GET /api/booths/{slug}` — the list shape plus the description a
    detail view needs. No tasks yet: task listing is not part of M07's
    scope (tasks are booth-authored config consumed starting M10/M11)."""

    slug: str
    name: str
    description: str | None
    location_label: str | None
