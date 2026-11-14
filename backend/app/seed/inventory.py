"""Read-only inventory of demo data inside an existing database.

A deployment that started life as a demo has fictional staff accounts,
booths and attendees mixed in with whatever real data was added since.
This report lists what looks like demo data, what depends on it, and
what is real — so an operator can decide, record by record, what to
replace. It NEVER deletes, deactivates or edits anything:

* the transaction is opened ``READ ONLY`` — PostgreSQL itself rejects
  any write issued through this session — and is always rolled back;
* it identifies demo records by the fixture identities
  (``app.seed.demo_fixtures``) and the reserved ``facemark.demo`` email
  domains, then says whether each record is still *identical* to its
  fixture or has been edited since (an edited ``aiworks`` booth is
  probably a real one that merely reuses the slug).

Attendee personal data is never listed — only demo attendees' fixture
emails and aggregate counts of everyone else.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.seed import demo_fixtures as fx

_DEMO_EMAIL_PREDICATE = "(email LIKE '%@facemark.demo' OR email LIKE '%.facemark.demo')"


@dataclass(frozen=True)
class DemoStaff:
    id: int
    email: str
    full_name: str
    role: str
    is_active: bool
    booth_slug: str | None
    identical_to_fixture: bool
    points_awarded_rows: int


@dataclass(frozen=True)
class DemoBooth:
    id: int
    slug: str
    name: str
    is_active: bool
    identical_to_fixture: bool
    tasks: int
    points_rows: int
    feedback_rows: int
    staff_accounts: int
    non_demo_staff_accounts: int

    @property
    def has_history(self) -> bool:
        return self.points_rows > 0 or self.feedback_rows > 0


@dataclass(frozen=True)
class DemoAttendee:
    id: int
    email: str
    status: str
    points_rows: int
    feedback_rows: int


@dataclass(frozen=True)
class RealStaff:
    id: int
    email: str
    role: str
    is_active: bool
    booth_slug: str | None


@dataclass(frozen=True)
class InventoryReport:
    demo_staff: list[DemoStaff] = field(default_factory=list)
    demo_booths: list[DemoBooth] = field(default_factory=list)
    demo_attendees: list[DemoAttendee] = field(default_factory=list)
    other_staff: list[RealStaff] = field(default_factory=list)
    other_booth_count: int = 0
    other_attendee_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_inventory(db: Session) -> InventoryReport:
    """Needs a session that has not run a query yet (the READ ONLY
    setting must open the transaction). Always rolls back."""
    try:
        db.execute(text("SET TRANSACTION READ ONLY"))
        return _collect(db)
    finally:
        db.rollback()


def _collect(db: Session) -> InventoryReport:
    fixture_staff = {s.email: s for s in fx.STAFF}
    fixture_booths = {b.slug: b for b in fx.BOOTHS}
    demo_staff_emails = list(fixture_staff)
    demo_slugs = list(fixture_booths)
    demo_attendee_emails = [a.email for a in fx.ATTENDEES]

    staff_rows: Sequence[Any] = db.execute(
        text(
            "SELECT s.id, s.email::text, s.full_name, s.role::text, s.is_active, b.slug, "
            "(SELECT count(*) FROM points_log p WHERE p.entered_by_staff_id = s.id) "
            "FROM staff_accounts s LEFT JOIN booths b ON b.id = s.booth_id "
            "WHERE s.email::text = ANY(CAST(:emails AS text[])) OR "
            + _DEMO_EMAIL_PREDICATE.replace("email", "s.email::text")
            + " ORDER BY s.id"
        ),
        {"emails": demo_staff_emails},
    ).all()
    demo_staff: list[DemoStaff] = []
    demo_staff_ids: set[int] = set()
    for sid, email, name, role, active, booth_slug, awarded in staff_rows:
        fixture = fixture_staff.get(str(email).lower())
        same = (
            fixture is not None
            and fixture.full_name == name
            and fixture.role.value == role
            and fixture.booth_slug == booth_slug
        )
        demo_staff_ids.add(sid)
        demo_staff.append(DemoStaff(sid, email, name, role, active, booth_slug, same, int(awarded)))

    booth_rows: Sequence[Any] = db.execute(
        text(
            "SELECT b.id, b.slug, b.name, b.description, b.location_label, b.is_active, "
            "(SELECT count(*) FROM tasks t WHERE t.booth_id = b.id), "
            "(SELECT count(*) FROM points_log p WHERE p.booth_id = b.id), "
            "(SELECT count(*) FROM feedback f WHERE f.booth_id = b.id), "
            "(SELECT count(*) FROM staff_accounts s WHERE s.booth_id = b.id), "
            "(SELECT count(*) FROM staff_accounts s WHERE s.booth_id = b.id "
            "   AND NOT (s.id = ANY(CAST(:demo_staff_ids AS int[])))) "
            "FROM booths b WHERE b.slug = ANY(CAST(:slugs AS text[])) ORDER BY b.id"
        ),
        {"slugs": demo_slugs, "demo_staff_ids": sorted(demo_staff_ids)},
    ).all()
    demo_booths: list[DemoBooth] = []
    for bid, slug, name, desc, location, active, tasks, pts, fb, staff, other_staff in booth_rows:
        fixture_booth = fixture_booths[slug]
        same = (
            fixture_booth.name == name
            and fixture_booth.description == desc
            and fixture_booth.location_label == location
        )
        demo_booths.append(
            DemoBooth(
                bid,
                slug,
                name,
                active,
                same,
                int(tasks),
                int(pts),
                int(fb),
                int(staff),
                int(other_staff),
            )
        )

    attendee_rows: Sequence[Any] = db.execute(
        text(
            "SELECT u.id, u.email::text, u.status::text, "
            "(SELECT count(*) FROM points_log p WHERE p.user_id = u.id), "
            "(SELECT count(*) FROM feedback f WHERE f.user_id = u.id) "
            "FROM users u WHERE u.email::text = ANY(CAST(:emails AS text[])) OR "
            + _DEMO_EMAIL_PREDICATE.replace("email", "u.email::text")
            + " ORDER BY u.id"
        ),
        {"emails": demo_attendee_emails},
    ).all()
    demo_attendees = [
        DemoAttendee(uid, email, status, int(pts), int(fb))
        for uid, email, status, pts, fb in attendee_rows
    ]

    other_staff_rows: Sequence[Any] = db.execute(
        text(
            "SELECT s.id, s.email::text, s.role::text, s.is_active, b.slug "
            "FROM staff_accounts s LEFT JOIN booths b ON b.id = s.booth_id "
            "WHERE NOT (s.id = ANY(CAST(:demo_staff_ids AS int[]))) ORDER BY s.id"
        ),
        {"demo_staff_ids": sorted(demo_staff_ids)},
    ).all()
    other_staff = [RealStaff(*row) for row in other_staff_rows]

    other_booths: Any = db.execute(
        text("SELECT count(*) FROM booths WHERE NOT (slug = ANY(CAST(:slugs AS text[])))"),
        {"slugs": demo_slugs},
    ).scalar_one()
    demo_attendee_ids = [a.id for a in demo_attendees]
    other_attendees: Any = db.execute(
        text("SELECT count(*) FROM users WHERE NOT (id = ANY(CAST(:ids AS int[])))"),
        {"ids": demo_attendee_ids},
    ).scalar_one()

    return InventoryReport(
        demo_staff=demo_staff,
        demo_booths=demo_booths,
        demo_attendees=demo_attendees,
        other_staff=other_staff,
        other_booth_count=int(other_booths),
        other_attendee_count=int(other_attendees),
    )


def format_report(report: InventoryReport) -> str:
    lines: list[str] = ["DEMO DATA INVENTORY (read-only; nothing was changed)", ""]

    lines.append(f"Demo staff accounts: {len(report.demo_staff)}")
    for s in report.demo_staff:
        lines.append(
            f"  #{s.id} {s.email}  {s.role}  booth={s.booth_slug or '-'}  "
            f"{'active' if s.is_active else 'inactive'}  "
            f"{'matches fixture' if s.identical_to_fixture else 'EDITED since seeding'}  "
            f"points awarded: {s.points_awarded_rows}"
        )

    lines += ["", f"Demo booths: {len(report.demo_booths)}"]
    for b in report.demo_booths:
        notes = []
        if not b.identical_to_fixture:
            notes.append("EDITED since seeding - may be a real booth reusing the slug")
        if b.has_history:
            notes.append(f"has history: {b.points_rows} points rows, {b.feedback_rows} feedback")
        if b.non_demo_staff_accounts:
            notes.append(f"{b.non_demo_staff_accounts} non-demo staff assigned")
        lines.append(
            f"  #{b.id} {b.slug} ({b.name})  {'active' if b.is_active else 'inactive'}  "
            f"tasks={b.tasks}  staff={b.staff_accounts}"
            + (f"  <- {'; '.join(notes)}" if notes else "")
        )

    lines += ["", f"Demo attendees: {len(report.demo_attendees)}"]
    for a in report.demo_attendees:
        lines.append(
            f"  #{a.id} {a.email}  {a.status}  "
            f"points rows={a.points_rows}  feedback={a.feedback_rows}"
        )

    lines += [
        "",
        "Everything else (not listed as demo):",
        f"  attendees: {report.other_attendee_count} (details intentionally not shown)",
        f"  booths:    {report.other_booth_count}",
        f"  staff:     {len(report.other_staff)}",
    ]
    for r in report.other_staff:
        lines.append(
            f"    #{r.id} {r.email}  {r.role}  booth={r.booth_slug or '-'}  "
            f"{'active' if r.is_active else 'inactive'}"
        )
    lines += [
        "",
        "Nothing here is removed or deactivated automatically. Replace demo records",
        "through the admin console (create the real ones first, then deactivate).",
    ]
    return "\n".join(lines)
