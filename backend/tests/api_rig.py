"""A small rig for tests that drive the admin / staff API against the disposable
reset-test database (a scratch database created by this run only, see
tests/suite_db.py): a TestClient whose DB sessions come from that database,
SQL helpers, sign-in helpers and ledger fixtures.

The `rig` fixture (conftest.py) yields one per test, on a freshly seeded
database (`clean_reset_db`).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import ResourceClosedError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.sessions import create_attendee_session, create_staff_session
from app.db.session import get_db
from app.main import app
from app.modules.staff.models import StaffAccount, StaffRole


class Rig:
    def __init__(self, client: TestClient, engine: Engine) -> None:
        self.client = client
        self.engine = engine

    # --- SQL ---------------------------------------------------------------

    def sql(self, statement: str, **params: Any) -> list[Any]:
        with Session(self.engine) as db:
            result = db.execute(text(statement), params)
            try:
                rows = list(result.all())
            except ResourceClosedError:  # a statement that returns no rows
                rows = []
            db.commit()
            return rows

    def scalar(self, statement: str, **params: Any) -> Any:
        return self.sql(statement, **params)[0][0]

    # --- identities --------------------------------------------------------

    def staff_id(self, role: StaffRole, nth: int = 0) -> int:
        with Session(self.engine) as db:
            rows = (
                db.execute(
                    select(StaffAccount.id)
                    .where(StaffAccount.role == role)
                    .order_by(StaffAccount.id)
                )
                .scalars()
                .all()
            )
            return int(rows[nth])

    def session_for(self, staff_id: int) -> str:
        """A fresh live staff session (the raw cookie value)."""
        with Session(self.engine) as db:
            token = create_staff_session(db, staff_account_id=staff_id, now=datetime.now(UTC))
            db.commit()
        return token

    def sign_in_as(self, staff_id: int) -> str:
        token = self.session_for(staff_id)
        self.client.cookies.clear()
        self.client.cookies.set("session", token)
        return token

    def sign_in(self, role: StaffRole = StaffRole.ADMIN, nth: int = 0) -> int:
        staff_id = self.staff_id(role, nth)
        self.sign_in_as(staff_id)
        return staff_id

    def sign_in_attendee(self, user_id: int) -> str:
        with Session(self.engine) as db:
            token = create_attendee_session(db, user_id=user_id, now=datetime.now(UTC))
            db.commit()
        self.client.cookies.clear()
        self.client.cookies.set("session", token)
        return token

    def sign_out(self) -> None:
        self.client.cookies.clear()

    # --- ledger fixtures ---------------------------------------------------

    def make_user(self, tag: str = "u") -> int:
        return int(
            self.scalar(
                "INSERT INTO users (first_name, last_name, email, phone, profession, "
                "sponsor_consent, status, activated_at) VALUES (:f, 'Tester', :e, '+10000000', "
                "'Engineer', false, 'ACTIVE', now()) RETURNING id",
                f=f"Ledger{tag}",
                e=f"ledger-{tag}@mail.test",
            )
        )

    def booth_with_staff(self, nth: int = 0) -> tuple[int, int]:
        """(booth id, the booth staff account's id) of the nth seeded booth."""
        row = self.sql(
            "SELECT booth_id, id FROM staff_accounts WHERE role = 'BOOTH_STAFF' ORDER BY id"
        )[nth]
        return int(row[0]), int(row[1])

    def make_task(self, booth_id: int, *, points: int = 10, repeatable: bool | None = None) -> int:
        return int(
            self.scalar(
                "INSERT INTO tasks (booth_id, title, points, allow_repeat_points, sort_order) "
                "VALUES (:b, :t, :p, :r, 99) RETURNING id",
                b=booth_id,
                t=f"Ledger activity {points}",
                p=points,
                r=repeatable,
            )
        )

    def plant_task_award(
        self,
        *,
        user_id: int,
        booth_id: int,
        staff_id: int,
        task_id: int,
        points: int = 10,
        once_only: bool = True,
    ) -> int:
        key = f"TASK:{user_id}:task:{task_id}" if once_only else None
        return int(
            self.scalar(
                "INSERT INTO points_log (user_id, booth_id, source, points, task_id, "
                "entered_by_staff_id, entered_by_system, dedupe_key) VALUES "
                "(:u, :b, 'TASK', :p, :t, :s, false, :k) RETURNING id",
                u=user_id,
                b=booth_id,
                p=points,
                t=task_id,
                s=staff_id,
                k=key,
            )
        )

    def plant_feedback_award(self, *, user_id: int, booth_id: int, points: int = 5) -> int:
        return int(
            self.scalar(
                "INSERT INTO points_log (user_id, booth_id, source, points, "
                "entered_by_staff_id, entered_by_system) VALUES "
                "(:u, :b, 'FEEDBACK', :p, NULL, true) RETURNING id",
                u=user_id,
                b=booth_id,
                p=points,
            )
        )

    def total_points(self, user_id: int) -> int:
        return int(
            self.scalar(
                "SELECT coalesce(sum(points), 0) FROM points_log WHERE user_id = :u", u=user_id
            )
        )

    def ledger_row(self, entry_id: int) -> dict[str, Any]:
        with Session(self.engine) as db:
            result = db.execute(text("SELECT * FROM points_log WHERE id = :i"), {"i": entry_id})
            row = result.mappings().one()
            return dict(row)

    def audit_rows(self, action: str | None = None) -> list[dict[str, Any]]:
        clause = "WHERE action = :a" if action else ""
        with Session(self.engine) as db:
            result = db.execute(
                text(f"SELECT * FROM admin_audit {clause} ORDER BY id"), {"a": action}
            )
            return [dict(r) for r in result.mappings().all()]


@contextmanager
def open_rig(engine: Engine) -> Iterator[Rig]:
    def isolated_db() -> Iterator[Session]:
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = isolated_db
    try:
        with TestClient(app, headers={"Origin": get_settings().app_origin}) as client:
            yield Rig(client, engine)
    finally:
        app.dependency_overrides.clear()


def dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)
