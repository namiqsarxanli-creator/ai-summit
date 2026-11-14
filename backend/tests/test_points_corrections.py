"""Correcting competition points: the ledger lists every source, and FACEMARK's
Game Master (not only the administrator) can reverse an award with a reason."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.modules.points.models import PointsSource
from app.modules.staff.models import StaffRole
from tests.test_competition import Comp, _award, baku
from tests.test_competition import comp as comp  # noqa: F401 - the fixture


def test_the_ledger_lists_competition_points_not_only_the_old_booth_ones(comp: Comp) -> None:  # noqa: F811
    (uid,) = comp.users(1)
    assert _award(comp, uid, PointsSource.PANEL, "panel:1", 8, baku("11:25")) == "awarded"
    admin = comp.staff_client(comp.rig.staff_id(StaffRole.ADMIN))
    r = admin.get("/api/admin/points-log")
    assert r.status_code == 200, r.text
    (row,) = [i for i in r.json()["items"] if i["user_id"] == uid]
    assert (row["source"], row["points"], row["entry_type"]) == ("PANEL", 8, "AWARD")
    assert admin.get("/api/admin/points-log", params={"source": "PANEL"}).status_code == 200


def test_a_game_master_reverses_a_wrong_award_and_the_score_follows(comp: Comp) -> None:  # noqa: F811
    (uid,) = comp.users(1)
    _award(comp, uid, PointsSource.MASTERCLASS, "masterclass:1", 12, baku("13:00"))
    gm = comp.volunteer("GAME_MASTER", None)
    page = gm.get("/api/staff/points-log", params={"user_id": uid})
    assert page.status_code == 200, page.text
    (entry,) = page.json()["items"]
    body = {"reason": "Scanned the wrong attendee", "idempotency_key": str(uuid4())}
    done = gm.post(f"/api/staff/points-log/{entry['id']}/reverse", json=body)
    assert done.status_code == 200, done.text
    assert comp.points(uid) == 0
    # A replay of the same request changes nothing more.
    assert gm.post(f"/api/staff/points-log/{entry['id']}/reverse", json=body).status_code == 200
    assert comp.points(uid) == 0
    audit = comp.rig.sql(
        "SELECT a.action FROM admin_audit a JOIN staff_accounts s ON s.id = a.staff_account_id "
        "WHERE s.role = 'VOLUNTEER' ORDER BY a.id DESC LIMIT 1"
    )
    assert [tuple(r) for r in audit] == [("points.reverse",)]


def test_only_an_administrator_or_a_game_master_may_correct(comp: Comp) -> None:  # noqa: F811
    (uid,) = comp.users(1)
    _award(comp, uid, PointsSource.PANEL, "panel:1", 8, baku("11:25"))
    booth = comp.staff_client(comp.rig.staff_id(StaffRole.BOOTH_STAFF))
    for c in (booth, comp.client(uid), TestClient(app)):
        assert c.get("/api/staff/points-log").status_code in (401, 403)
        refused = c.post(
            "/api/staff/points-log/1/reverse",
            json={"reason": "Wrong scan", "idempotency_key": str(uuid4())},
        )
        assert refused.status_code in (401, 403)
    assert comp.points(uid) == 8


def test_the_ledger_schema_names_every_points_source() -> None:
    from typing import get_args

    from app.modules.admin.schemas import LedgerSource

    assert set(get_args(LedgerSource)) == {s.value for s in PointsSource}


def test_a_game_master_decides_an_unpaid_connection_from_the_organiser_panel(comp: Comp) -> None:  # noqa: F811
    gm = comp.volunteer("GAME_MASTER", None)
    r = gm.get("/api/staff/connections/unpaid")
    assert r.status_code == 200 and "counts" in r.json()
    (uid,) = comp.users(1)
    assert comp.client(uid).get("/api/staff/connections/unpaid").status_code in (401, 403)
    assert comp.client(uid).post(
        "/api/staff/connections/reconcile",
        json={"items": [{"connection_id": 1, "user_id": uid}], "reason": "x y z", "confirm": True},
    ).status_code in (401, 403)
