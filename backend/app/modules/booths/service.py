"""Read access to the booth catalogue — ARCHITECTURE.md §7.4, §10.

Pure reads: no reset fence (§19.4).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.booths.models import Booth, Task

# "Curated order": ARCHITECTURE.md §11.4 says booths without a
# personalised score are shown "in curated order" and gives no other
# ordering rule anywhere for the plain catalogue. Booths have no
# sort_order column (schema changes are out of scope for M07), so the
# order curators actually control — insertion order, `id ASC` — is used
# for both the plain list and the no-interest recommendation case. The
# seed data deliberately inserts AIWorks first as the flagship example
# (app/seed/demo_fixtures.py), so this is not an arbitrary choice.
CURATED_ORDER = Booth.id.asc()


def list_active_booths(db: Session) -> list[Booth]:
    return list(
        db.execute(select(Booth).where(Booth.is_active.is_(True)).order_by(CURATED_ORDER)).scalars()
    )


def get_active_booth_by_slug(db: Session, slug: str) -> Booth | None:
    return db.execute(
        select(Booth).where(Booth.slug == slug, Booth.is_active.is_(True))
    ).scalar_one_or_none()


def list_active_tasks(db: Session, booth_id: int) -> list[Task]:
    """M08's booth detail screen (§7.9: "tasks exist so the booth
    detail screen can tell attendees what to do")."""
    return list(
        db.execute(
            select(Task)
            .where(Task.booth_id == booth_id, Task.is_active.is_(True))
            .order_by(Task.sort_order, Task.id)
        ).scalars()
    )
