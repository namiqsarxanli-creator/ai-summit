"""The initial settings every FACEMARK deployment needs — real or demo.

Deliberately contains NO event content: no booths, tasks, interests,
attendees or staff accounts. Those are either the organiser's own data
(entered through the admin console) or demo fixtures
(``app.seed.demo_fixtures``, DEMO_MODE only).

What it does create, only when absent:

* the ``event_config`` singleton row the application reads on every
  request (ARCHITECTURE.md §7.13), and
* the global point-preset allow-list staff pick from when awarding
  points (§7.10) — without it no award can be made until an admin adds
  presets by hand.

Both are ``INSERT ... ON CONFLICT DO NOTHING`` on their natural key:
re-running never duplicates a row and never overwrites a value an
operator has since changed. Callers own the transaction.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.modules.admin.models import EventConfig
from app.modules.booths.models import TaskPresetPoint


@dataclass(frozen=True)
class PresetPointFixture:
    points: int
    label: str
    sort_order: int


PRESET_POINTS: tuple[PresetPointFixture, ...] = (
    PresetPointFixture(5, "Quick interaction", 0),
    PresetPointFixture(10, "Solid engagement", 1),
    PresetPointFixture(15, "Deep engagement", 2),
)

# The singleton row (ARCHITECTURE.md §7.13). id is always 1.
EVENT_CONFIG_DEFAULTS: dict[str, object] = {
    "id": 1,
    "feedback_points": 5,
    "allow_repeat_task_points_default": False,
    "leaderboard_top_n": 10,
    "dataset_generation": 1,
    "maintenance_mode": False,
}


@dataclass(frozen=True)
class BaselineResult:
    preset_points_created: int
    event_config_created: bool


def seed_baseline(db: Session) -> BaselineResult:
    """Creates whatever of the baseline is missing. Does not commit."""
    presets = 0
    for p in PRESET_POINTS:
        stmt = (
            pg_insert(TaskPresetPoint)
            .values(booth_id=None, points=p.points, label=p.label, sort_order=p.sort_order)
            .on_conflict_do_nothing(index_elements=["points"], index_where=text("booth_id IS NULL"))
        )
        # `RETURNING` yields only rows actually inserted; `rowcount` is
        # unreliable with ON CONFLICT DO NOTHING (see seeder.py).
        presets += len(db.execute(stmt.returning(TaskPresetPoint.id)).all())

    config_stmt = (
        pg_insert(EventConfig)
        .values(**EVENT_CONFIG_DEFAULTS)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    config_created = bool(db.execute(config_stmt.returning(EventConfig.id)).all())
    return BaselineResult(preset_points_created=presets, event_config_created=config_created)
