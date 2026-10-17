"""Beyond Now competition: verified participation, scoped staff, the ledger.

Rig database (disposable, freshly seeded); server clocks replaced per test.
Times below are Asia/Baku (UTC+4) on 13 Nov 2026.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import generate_card_token
from app.main import app
from app.modules.competition import ledger, panels
from app.modules.competition import routers as comp_routers
from app.modules.competition.profile import maybe_award_profile
from app.modules.masterclasses import service as mc_service
from app.modules.playground import service as pg_service
from app.modules.points.models import PointsSource
from app.modules.staff.models import StaffRole
from tests.api_rig import Rig
from tests.test_masterclasses import Program, choose


def baku(hhmm: str, seconds: int = 0) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 11, 13, h - 4, m, seconds, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = baku("08:00")

    def __call__(self) -> datetime:
        return self.now


class Comp(Program):
    clock: Clock
    panel1: int
    panel2: int
    tokens: dict[int, str]

    def card(self, uid: int) -> str:
        token = self.tokens.get(uid)
        if token is None:
            token = generate_card_token()
            self.rig.sql(
                "INSERT INTO contact_cards (user_id, public_token, issued_at) "
                "VALUES (:u, :t, now())",
                u=uid,
                t=token,
            )
            self.tokens[uid] = token
        return token

    def volunteer(self, scope: str, target: int | None) -> TestClient:
        """A VOLUNTEER account with one scope, signed in on its own client."""
        sid = int(
            self.rig.scalar(
                "INSERT INTO staff_accounts (email, password_hash, full_name, role, is_active) "
                "VALUES (:e, 'x', 'Volunteer', 'VOLUNTEER', true) RETURNING id",
                e=f"vol-{uuid4().hex[:8]}@staff.test",
            )
        )
        admin_id = self.rig.staff_id(StaffRole.ADMIN)
        self.rig.sql(
            "INSERT INTO staff_scopes (staff_account_id, scope, target_id, granted_by_staff_id) "
            "VALUES (:s, :sc, :t, :a)",
            s=sid,
            sc=scope,
            t=target,
            a=admin_id,
        )
        return self.staff_client(sid)

    def staff_client(self, staff_id: int) -> TestClient:
        c = TestClient(app, headers={"Origin": get_settings().app_origin})
        c.cookies.set("session", self.rig.session_for(staff_id))
        self.clients.append(c)
        return c

    def points(self, uid: int, source: str | None = None) -> int:
        clause = "AND source = :s" if source else ""
        return int(
            self.rig.scalar(
                f"SELECT coalesce(sum(points), 0) FROM points_log WHERE user_id = :u {clause}",
                u=uid,
                s=source,
            )
        )


@pytest.fixture
def comp(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Iterator[Comp]:
    clock = Clock()
    monkeypatch.setattr(comp_routers, "_now", clock)
    monkeypatch.setattr(mc_service, "server_now", clock)
    monkeypatch.setattr(pg_service, "now", clock)
    p = Comp(rig)
    p.clock = clock
    p.tokens = {}
    rig.sql("UPDATE masterclass_events SET competition_closes_at = :c", c=baku("17:30"))
    p.panel1 = int(
        rig.scalar(
            "INSERT INTO panels (event_id, code, title, starts_at, ends_at) "
            "VALUES (:e, 'panel-1', 'Panel 1', :s, :f) RETURNING id",
            e=p.event,
            s=baku("10:00"),
            f=baku("11:30"),
        )
    )
    p.panel2 = int(
        rig.scalar(
            "INSERT INTO panels (event_id, code, title, starts_at, ends_at) "
            "VALUES (:e, 'panel-2', 'Panel 2', :s, :f) RETURNING id",
            e=p.event,
            s=baku("14:00"),
            f=baku("15:30"),
        )
    )
    yield p
    for c in p.clients:
        c.close()


def code_of(comp: Comp, panel_id: int, phase: str) -> str:
    with Session(comp.rig.engine) as db:
        from app.modules.competition.models import Panel

        panel = db.get(Panel, panel_id)
        assert panel is not None
        return panels.screen_code(panel, phase)


def scan(c: TestClient, code: str) -> httpx.Response:
    return cast(httpx.Response, c.post("/api/attendees/me/panels/scan", json={"code": code}))


def reason(r: httpx.Response) -> str:
    return str(r.json()["error"]["details"]["reason"])


# --- the ledger --------------------------------------------------------------------------


def test_the_category_maximums_add_up_to_exactly_100() -> None:
    from app.modules.competition.rules import CATEGORIES

    assert sum(c.maximum for c in CATEGORIES.values()) == 100
    assert {c.source.value: c.maximum for c in CATEGORIES.values()} == {
        "PROFILE": 5,
        "PANEL": 16,
        "QMETER": 4,
        "MASTERCLASS": 24,
        "PLAYGROUND": 24,
        "CONNECTION": 12,
        "MENTOR": 5,
        "FOUNDER": 10,
    }


def _award(comp: Comp, uid: int, source: PointsSource, unit: str, points: int, at: datetime) -> str:
    with Session(comp.rig.engine) as db:
        result = ledger.award(
            db, user_id=uid, source=source, unit=unit, points=points, occurred_at=at
        )
        db.commit()
        return result.outcome


def test_the_ledger_pays_a_unit_once_and_respects_cutoff_cap_and_eligibility(comp: Comp) -> None:
    uid, other = comp.users(2)
    assert _award(comp, uid, PointsSource.PANEL, "panel:1", 8, baku("11:25")) == "awarded"
    assert _award(comp, uid, PointsSource.PANEL, "panel:1", 8, baku("11:26")) == "already"
    assert _award(comp, uid, PointsSource.PANEL, "panel:2", 8, baku("15:25")) == "awarded"
    # a third panel would exceed the category's own maximum of 16
    assert _award(comp, uid, PointsSource.PANEL, "panel:x", 8, baku("15:26")) == "category_full"
    # at and after 17:30 nothing is earned
    assert _award(comp, uid, PointsSource.QMETER, "panel:1", 2, baku("17:30")) == "closed"
    assert _award(comp, uid, PointsSource.QMETER, "panel:1", 2, baku("17:29", 59)) == "awarded"
    comp.rig.sql(
        "UPDATE users SET competition_excluded_reason = 'Organiser team' WHERE id = :u", u=other
    )
    assert _award(comp, other, PointsSource.PANEL, "panel:1", 8, baku("11:25")) == "ineligible"
    assert comp.points(uid) == 18 and comp.points(other) == 0


def test_only_the_first_eight_playground_booths_score(comp: Comp) -> None:
    (uid,) = comp.users(1)
    booths = [
        int(
            comp.rig.scalar(
                "INSERT INTO booths (slug, name) VALUES (:s, :n) RETURNING id", s=f"b{i}", n=f"B{i}"
            )
        )
        for i in range(9)
    ]
    for booth in booths[:8]:
        comp.rig.sql(
            "INSERT INTO points_log (user_id, booth_id, source, points, entered_by_system, "
            "dedupe_key) VALUES (:u, :b, 'PLAYGROUND', 3, true, :k)",
            u=uid,
            b=booth,
            k=f"COMP:PLAYGROUND:{uid}:booth:{booth}",
        )
    with Session(comp.rig.engine) as db:
        ninth = ledger.award(
            db,
            user_id=uid,
            source=PointsSource.PLAYGROUND,
            unit=f"booth:{booths[8]}",
            points=3,
            occurred_at=baku("12:00"),
            booth_id=booths[8],
        )
        db.commit()
    assert ninth.outcome == "not_scored" and comp.points(uid, "PLAYGROUND") == 24


# --- PROFILE ----------------------------------------------------------------------------


def test_a_complete_card_earns_profile_points_once_and_an_incomplete_one_none(comp: Comp) -> None:
    done, partial = comp.users(2)
    comp.rig.sql(
        "UPDATE users SET card_sector = 'energy', card_topics = ARRAY['data'], "
        "card_seeking = ARRAY['partner'], card_offering = ARRAY['mentoring'] WHERE id = :u",
        u=done,
    )
    comp.rig.sql(
        "UPDATE users SET card_sector = 'energy', card_topics = ARRAY['data'] WHERE id = :u",
        u=partial,
    )
    from app.modules.attendees.models import User

    with Session(comp.rig.engine) as db:
        for uid in (done, partial, done):
            user = db.get(User, uid)
            assert user is not None
            maybe_award_profile(db, user, baku("09:00"))
        db.commit()
    assert comp.points(done, "PROFILE") == 5
    assert comp.points(partial, "PROFILE") == 0


# --- panels ----------------------------------------------------------------------------


def test_a_panel_needs_entry_and_exit_in_their_windows(comp: Comp) -> None:
    (uid,) = comp.users(1)
    c = comp.client(uid)
    entry, exit_ = code_of(comp, comp.panel1, "ENTRY"), code_of(comp, comp.panel1, "EXIT")
    comp.clock.now = baku("10:05")
    first = scan(c, entry)
    assert first.status_code == 200 and first.json()["award"] is None
    assert scan(c, entry).status_code == 200  # a repeat changes nothing
    assert reason(scan(c, exit_)) == "outside_window"  # exit opens only at 11:20
    comp.clock.now = baku("11:25")
    done = scan(c, exit_)
    assert done.json()["award"] == {"outcome": "awarded", "points": 8}
    assert scan(c, exit_).json()["award"]["outcome"] == "already"
    assert comp.points(uid, "PANEL") == 8


def test_a_late_entry_earns_nothing_and_codes_cannot_be_swapped_or_forged(comp: Comp) -> None:
    (uid,) = comp.users(1)
    c = comp.client(uid)
    comp.clock.now = baku("10:15")  # the entry window is [10:00, 10:15)
    assert reason(scan(c, code_of(comp, comp.panel1, "ENTRY"))) == "outside_window"
    comp.clock.now = baku("10:05")
    exit_code = code_of(comp, comp.panel1, "EXIT")
    swapped = exit_code.replace(".EXIT.", ".ENTRY.")  # phase substitution
    assert reason(scan(c, swapped)) == "invalid_code"
    other_panel = code_of(comp, comp.panel2, "ENTRY").replace(
        f"P{comp.panel2}.", f"P{comp.panel1}."
    )
    assert reason(scan(c, other_panel)) == "invalid_code"
    # rotating the QR revokes every code shown before
    old = code_of(comp, comp.panel1, "ENTRY")
    comp.rig.sql("UPDATE panels SET qr_version = qr_version + 1 WHERE id = :p", p=comp.panel1)
    assert reason(scan(c, old)) == "invalid_code"
    assert scan(c, code_of(comp, comp.panel1, "ENTRY")).status_code == 200


def _admitted_playground(comp: Comp, uid: int, slot_number: int) -> int:
    """A Playground slot (event booth 1) where `uid` is CHECKED_IN."""
    booth = int(comp.rig.scalar("SELECT id FROM booths ORDER BY id LIMIT 1"))
    comp.rig.sign_in_as(comp.rig.staff_id(StaffRole.ADMIN))
    r = comp.rig.client.post(
        "/api/admin/playground/prepare", json={"event_id": comp.event, "booth_id": booth}
    )
    assert r.status_code == 200, r.text
    slot = int(
        comp.rig.scalar(
            "SELECT id FROM playground_slots WHERE booth_id = :b AND number = :n",
            b=booth,
            n=slot_number,
        )
    )
    comp.rig.sql(
        "INSERT INTO playground_bookings (slot_id, user_id, command_id, status) "
        "VALUES (:s, :u, :c, 'CHECKED_IN')",
        s=slot,
        u=uid,
        c=str(uuid4()),
    )
    return slot


def test_playground_during_a_panel_is_not_also_a_panel(comp: Comp) -> None:
    (uid,) = comp.users(1)
    c = comp.client(uid)
    _admitted_playground(comp, uid, 6)  # P06: 10:15-10:30, inside panel 1
    comp.clock.now = baku("10:05")
    scan(c, code_of(comp, comp.panel1, "ENTRY"))
    comp.clock.now = baku("11:25")
    assert scan(c, code_of(comp, comp.panel1, "EXIT")).json()["award"]["outcome"] == "not_scored"
    assert comp.points(uid, "PANEL") == 0


def test_a_playground_session_verified_after_a_paid_panel_reverses_it(comp: Comp) -> None:
    (uid,) = comp.users(1)
    c = comp.client(uid)
    booth = int(comp.rig.scalar("SELECT id FROM booths ORDER BY id LIMIT 1"))
    comp.clock.now = baku("10:05")
    scan(c, code_of(comp, comp.panel1, "ENTRY"))
    comp.clock.now = baku("11:22")
    assert scan(c, code_of(comp, comp.panel1, "EXIT")).json()["award"]["outcome"] == "awarded"
    slot = _admitted_playground(comp, uid, 10)  # P10: 11:15-11:30
    staff_id = int(
        comp.rig.scalar(
            "SELECT id FROM staff_accounts WHERE role = 'BOOTH_STAFF' AND booth_id = :b "
            "ORDER BY id LIMIT 1",
            b=booth,
        )
    )
    staff = comp.staff_client(staff_id)
    comp.clock.now = baku("11:28")
    r = staff.post(
        f"/api/staff/playground/slots/{slot}/complete", json={"card_token": comp.card(uid)}
    )
    assert r.status_code == 200, r.text
    assert r.json()["award"]["outcome"] == "awarded"
    assert comp.points(uid, "PANEL") == 0  # reversed, never edited
    reversal = comp.rig.sql(
        "SELECT entry_type, points FROM points_log WHERE user_id = :u AND source = 'PANEL' "
        "ORDER BY id",
        u=uid,
    )
    assert [tuple(r) for r in reversal] == [("AWARD", 8), ("REVERSAL", -8)]
    assert comp.points(uid, "PLAYGROUND") == 3


# --- Qmeter ------------------------------------------------------------------------------


def test_qmeter_needs_a_game_master_an_exact_match_and_pays_once(comp: Comp) -> None:
    uid, twin = comp.users(2)
    comp.rig.sql(
        "UPDATE users SET first_name = 'Aysel', last_name = 'Məmmədova', "
        "phone = '+994 50 111 22 33' WHERE id = :u",
        u=uid,
    )
    comp.rig.sql(
        "UPDATE users SET first_name = 'Aysel', last_name = 'Məmmədova', "
        "phone = '+994 55 999 88 77' WHERE id = :u",
        u=twin,
    )
    gm = comp.volunteer("GAME_MASTER", None)
    comp.clock.now = baku("11:40")
    by_name = gm.post("/api/staff/qmeter/identify", json={"full_name": "Aysel", "phone": "1234567"})
    assert by_name.json()["error"]["code"] == "ATTENDEE_NOT_IDENTIFIED"
    found = gm.post(
        "/api/staff/qmeter/identify", json={"full_name": "aysel məmmədova", "phone": "994501112233"}
    )
    assert found.status_code == 200 and found.json()["user_id"] == uid
    assert found.json()["phone"] == "•••2233"
    body = {"user_id": uid, "panel_id": comp.panel1, "reason": "Question 14 in Qmeter"}
    assert gm.post("/api/staff/qmeter/award", json=body).json() == {
        "outcome": "awarded",
        "points": 2,
    }
    assert gm.post("/api/staff/qmeter/award", json=body).json()["outcome"] == "already"
    assert comp.points(uid, "QMETER") == 2
    audit = comp.rig.audit_rows("competition.qmeter")
    assert len(audit) == 1 and audit[0]["after_value"]["reason"] == "Question 14 in Qmeter"


def test_a_revoked_or_missing_scope_stops_the_very_next_request(comp: Comp) -> None:
    (uid,) = comp.users(1)
    gm = comp.volunteer("GAME_MASTER", None)
    door = comp.volunteer("MASTERCLASS_DOOR", 999)
    comp.clock.now = baku("11:40")
    body = {"user_id": uid, "panel_id": comp.panel1, "reason": "Question 2 in Qmeter"}
    assert door.post("/api/staff/qmeter/award", json=body).status_code == 403
    comp.rig.sql("UPDATE staff_scopes SET revoked_at = now() WHERE scope = 'GAME_MASTER'")
    assert gm.post("/api/staff/qmeter/award", json=body).status_code == 403
    assert comp.points(uid) == 0
    # a volunteer has no booth or admin powers at all
    assert gm.get("/api/admin/masterclass-program").status_code == 403


# --- masterclass doors ---------------------------------------------------------------------


def door(c: TestClient, mc: int, token: str, phase: str) -> httpx.Response:
    return cast(
        httpx.Response,
        c.post(f"/api/staff/masterclasses/{mc}/door", json={"card_token": token, "phase": phase}),
    )


def test_masterclass_attendance_needs_entry_and_exit_and_pays_a_block_once(comp: Comp) -> None:
    mc = comp.masterclass(comp.b1, capacity=5)
    (uid,) = comp.users(1)
    assert choose(comp.client(uid), comp.b1, mc).status_code == 200
    vol = comp.volunteer("MASTERCLASS_DOOR", mc)
    token = comp.card(uid)
    comp.clock.now = baku("11:25")
    assert reason(door(vol, mc, token, "ENTRY")) == "outside_window"  # opens at 11:30
    comp.clock.now = baku("11:32")
    assert door(vol, mc, token, "ENTRY").json()["standing"] is False
    assert door(vol, mc, token, "ENTRY").json()["already"] is True
    comp.clock.now = baku("12:55")
    first = door(vol, mc, token, "EXIT")
    assert first.json()["award"] == {"outcome": "awarded", "points": 12}
    assert door(vol, mc, token, "EXIT").json()["award"]["outcome"] == "already"
    assert comp.points(uid, "MASTERCLASS") == 12
    # a volunteer of another masterclass cannot scan this one
    other = comp.volunteer("MASTERCLASS_DOOR", mc + 1000)
    assert door(other, mc, token, "ENTRY").status_code == 403


def test_seats_go_to_reservations_then_the_waitlist_then_the_door_then_standing(
    comp: Comp,
) -> None:
    mc = comp.masterclass(comp.b1, capacity=2, waitlist=2)
    comp.rig.sql("UPDATE masterclasses SET standing_capacity = 1 WHERE id = :m", m=mc)
    a, b, w, d1, d2, d3 = comp.users(6)
    comp.fill(mc, comp.b1, [a, b], "CONFIRMED")
    comp.fill(mc, comp.b1, [w], "WAITLISTED")
    vol = comp.volunteer("MASTERCLASS_DOOR", mc)
    comp.clock.now = baku("11:32")
    # within +5 the seats belong to the reservations
    assert reason(door(vol, mc, comp.card(d1), "ENTRY")) == "seats_held_until_plus_five"
    assert door(vol, mc, comp.card(a), "ENTRY").status_code == 200  # a claims their seat
    comp.clock.now = baku("11:36")
    # b never came: their seat is released. One free seat, one person waiting:
    # a walk-in may not take it ahead of the waitlist...
    assert door(vol, mc, comp.card(d1), "ENTRY").json()["standing"] is True
    # ...the waitlisted attendee gets it
    assert door(vol, mc, comp.card(w), "ENTRY").json()["standing"] is False
    # seats full, the one standing place taken
    assert reason(door(vol, mc, comp.card(d2), "ENTRY")) == "full"
    released = comp.rig.scalar(
        "SELECT cancel_reason FROM masterclass_reservations WHERE user_id = :u", u=b
    )
    assert released == "NO_SHOW"
    # b arrives late: their seat is gone and the room is full
    assert reason(door(vol, mc, comp.card(b), "ENTRY")) == "full"
    del d3


def test_someone_booked_into_another_masterclass_is_refused_at_this_door(comp: Comp) -> None:
    here, there = comp.masterclass(comp.b1), comp.masterclass(comp.b1)
    (uid,) = comp.users(1)
    assert choose(comp.client(uid), comp.b1, there).status_code == 200
    vol = comp.volunteer("MASTERCLASS_DOOR", here)
    comp.clock.now = baku("11:36")
    assert reason(door(vol, here, comp.card(uid), "ENTRY")) == "booked_elsewhere"


def test_parallel_exit_scans_of_one_person_pay_once(comp: Comp) -> None:
    mc = comp.masterclass(comp.b1, capacity=5)
    (uid,) = comp.users(1)
    comp.fill(mc, comp.b1, [uid], "CONFIRMED")
    vol1, vol2 = comp.volunteer("MASTERCLASS_DOOR", mc), comp.volunteer("MASTERCLASS_DOOR", mc)
    token = comp.card(uid)
    comp.clock.now = baku("11:31")
    door(vol1, mc, token, "ENTRY")
    comp.clock.now = baku("12:58")
    with ThreadPoolExecutor(4) as pool:
        replies = list(pool.map(lambda c: door(c, mc, token, "EXIT"), [vol1, vol2, vol1, vol2]))
    assert all(r.status_code == 200 for r in replies), [r.text for r in replies]
    outcomes = sorted(r.json()["award"]["outcome"] for r in replies)
    assert outcomes == ["already", "already", "already", "awarded"]
    assert comp.points(uid, "MASTERCLASS") == 12


def test_a_revoked_attendee_card_cannot_be_scanned(comp: Comp) -> None:
    mc = comp.masterclass(comp.b1, capacity=5)
    (uid,) = comp.users(1)
    comp.fill(mc, comp.b1, [uid], "CONFIRMED")
    vol = comp.volunteer("MASTERCLASS_DOOR", mc)
    token = comp.card(uid)
    comp.rig.sql("UPDATE contact_cards SET revoked_at = now() WHERE user_id = :u", u=uid)
    comp.clock.now = baku("11:31")
    assert door(vol, mc, token, "ENTRY").json()["error"]["code"] == "INVALID_QR"


# --- Playground completion ------------------------------------------------------------------


def test_only_the_slots_own_booth_staff_complete_it_and_late_slots_score_nothing(
    comp: Comp,
) -> None:
    (uid,) = comp.users(1)
    booth = int(comp.rig.scalar("SELECT id FROM booths ORDER BY id LIMIT 1"))
    slot = _admitted_playground(comp, uid, 35)  # P35: 17:30-17:45, walk-in only
    other_staff = int(
        comp.rig.scalar(
            "SELECT id FROM staff_accounts WHERE role = 'BOOTH_STAFF' AND booth_id <> :b "
            "ORDER BY id LIMIT 1",
            b=booth,
        )
    )
    own_staff = int(
        comp.rig.scalar(
            "SELECT id FROM staff_accounts WHERE role = 'BOOTH_STAFF' AND booth_id = :b "
            "ORDER BY id LIMIT 1",
            b=booth,
        )
    )
    body = {"card_token": comp.card(uid)}
    comp.clock.now = baku("17:44")
    assert (
        comp.staff_client(other_staff)
        .post(f"/api/staff/playground/slots/{slot}/complete", json=body)
        .status_code
        == 403
    )
    own = comp.staff_client(own_staff)
    comp.clock.now = baku("17:40")
    assert reason(own.post(f"/api/staff/playground/slots/{slot}/complete", json=body)) == (
        "outside_window"
    )
    comp.clock.now = baku("17:44")
    r = own.post(f"/api/staff/playground/slots/{slot}/complete", json=body)
    assert r.json()["award"]["outcome"] == "not_scored"
    status = comp.rig.scalar(
        "SELECT status FROM playground_bookings WHERE slot_id = :s AND user_id = :u", s=slot, u=uid
    )
    assert status == "COMPLETED" and comp.points(uid) == 0


# --- the ranking ----------------------------------------------------------------------------


def test_competition_mode_counts_only_the_competition_and_gives_every_person_a_place(
    comp: Comp,
) -> None:
    a, b, legacy = comp.users(3)
    booth, staff = comp.rig.booth_with_staff()
    task = comp.rig.make_task(booth, points=50)
    comp.rig.plant_task_award(
        user_id=legacy, booth_id=booth, staff_id=staff, task_id=task, points=50
    )
    for uid, minute in ((b, "10:00"), (a, "11:00")):
        comp.rig.sql(
            "INSERT INTO points_log (user_id, source, points, entered_by_system, dedupe_key, "
            "created_at) VALUES (:u, 'PROFILE', 5, true, :k, :t)",
            u=uid,
            k=f"COMP:PROFILE:{uid}:profile",
            t=baku(minute),
        )
    viewer = comp.client(a)

    def board() -> list[tuple[int, int, int]]:
        rows = viewer.get("/api/leaderboard", params={"limit": 10}).json()["entries"]
        return [
            (e["user_id"], e["total_points"], e["rank"])
            for e in rows
            if e["user_id"] in (a, b, legacy)
        ]

    from app.modules.leaderboard.service import invalidate_ranking_snapshot

    assert board()[0] == (legacy, 50, 1)  # legacy mode: unchanged behaviour
    comp.rig.sql("UPDATE event_config SET competition_mode = true")
    invalidate_ranking_snapshot()
    ranked = board()
    # legacy TASK points do not count; equal points: b reached 5 first, own places
    assert ranked[:2] == [(b, 5, 1), (a, 5, 2)]
    assert next(points for user, points, _ in ranked if user == legacy) == 0
    assert len({place for _, _, place in ranked}) == len(ranked)  # no shared places
    comp.rig.sql("UPDATE users SET competition_excluded_reason = 'Speaker' WHERE id = :u", u=b)
    invalidate_ranking_snapshot()
    assert b not in [r[0] for r in board()]
    comp.rig.sql("UPDATE event_config SET competition_mode = false")
    invalidate_ranking_snapshot()


def test_my_competition_shows_each_category_against_its_own_maximum(comp: Comp) -> None:
    (uid,) = comp.users(1)
    _award(comp, uid, PointsSource.PANEL, "panel:1", 8, baku("11:25"))
    body = comp.client(uid).get("/api/attendees/me/competition").json()
    assert body["total"] == 8 and body["maximum"] == 100 and body["eligible"] is True
    panel = next(c for c in body["categories"] if c["category"] == "PANEL")
    assert panel == {"category": "PANEL", "points": 8, "maximum": 16}


# --- activation ------------------------------------------------------------------------------


def test_the_activation_report_is_read_only_and_the_backfill_pays_once(comp: Comp) -> None:
    from app.modules.competition.activation import backfill_profile, report

    complete, partial = comp.users(2)
    comp.rig.sql(
        "UPDATE users SET card_sector = 'energy', card_topics = ARRAY['data'], "
        "card_seeking = ARRAY['partner'], card_offering = ARRAY['mentoring'] WHERE id IN (:a)",
        a=complete,
    )
    comp.rig.sql("UPDATE users SET card_sector = 'energy' WHERE id = :u", u=partial)
    before = comp.rig.scalar("SELECT count(*) FROM points_log")
    with Session(comp.rig.engine) as db:
        r = report(db)
        db.rollback()
    assert r.profile_backfill_candidates == 1 and r.competition_mode is False
    assert comp.rig.scalar("SELECT count(*) FROM points_log") == before  # nothing written
    for _ in range(2):  # a re-run pays nobody twice
        with Session(comp.rig.engine) as db:
            backfill_profile(db, baku("09:00"))
            db.commit()
    assert comp.points(complete, "PROFILE") == 5 and comp.points(partial) == 0
