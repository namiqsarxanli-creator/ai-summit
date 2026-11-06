"""Beyond Now competition: verified participation, scoped staff, the ledger.

Rig database (disposable, freshly seeded); server clocks replaced per test.
Times below are Asia/Baku (UTC+4) on 13 Nov 2026.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
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
        "FOUNDER": 15,  # 2 Nov 2026: Meet the Mentor's 5 points moved here
    }
    assert PointsSource.MENTOR not in CATEGORIES


def _award(comp: Comp, uid: int, source: PointsSource, unit: str, points: int, at: datetime) -> str:
    with Session(comp.rig.engine) as db:
        result = ledger.award(
            db, user_id=uid, source=source, unit=unit, points=points, occurred_at=at
        )
        db.commit()
        return result.outcome


def test_a_screen_link_shows_one_panels_codes_without_a_session_until_rotated(
    comp: Comp,
) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    anonymous = TestClient(app)
    assert anonymous.get("/api/staff/panel-screen-links").status_code == 401
    door = comp.volunteer("MASTERCLASS_DOOR", comp.masterclass(comp.b1, capacity=5))
    assert door.get("/api/staff/panel-screen-links").status_code == 403  # not an organiser
    facemark = comp.volunteer("GAME_MASTER", None)
    assert facemark.get("/api/staff/panel-screen-links").status_code == 200
    comp.rig.sign_in_as(comp.rig.staff_id(StaffRole.ADMIN))
    links = comp.rig.client.get("/api/staff/panel-screen-links")
    assert links.status_code == 200
    assert links.headers["cache-control"] == "no-store"
    key = next(x["key"] for x in links.json() if x["id"] == comp.panel1)
    assert links.json()[0]["expires_at"].startswith("2026-11-14T00:00:00")

    comp.clock.now = baku("10:05")
    shown = anonymous.get("/api/screen/panel", params={"k": key})
    assert shown.status_code == 200, shown.text
    body = shown.json()
    assert body["id"] == comp.panel1 and len(body["entry_short"]) == 6
    assert body["entry_code"].startswith(f"P{comp.panel1}.ENTRY.")
    # Another panel's id under this signature, a changed expiry, garbage: refused.
    pid, stamp, sig = key.split(".")
    for bad in (f"{comp.panel2}.{stamp}.{sig}", f"{pid}.{int(stamp) + 86400}.{sig}", "x" * 20):
        r = anonymous.get("/api/screen/panel", params={"k": bad})
        assert r.status_code in (404, 422) and "entry_code" not in r.text
    # After midnight following the event day the link stops.
    comp.clock.now = baku("23:59") + timedelta(minutes=2)
    assert anonymous.get("/api/screen/panel", params={"k": key}).status_code == 404
    # Rotating the panel's codes revokes the link too.
    comp.clock.now = baku("10:05")
    comp.rig.sql("UPDATE panels SET qr_version = qr_version + 1 WHERE id = :p", p=comp.panel1)
    gone = anonymous.get("/api/screen/panel", params={"k": key})
    assert gone.status_code == 404
    assert gone.json()["error"]["code"] == "SCREEN_LINK_INVALID"


def test_the_id_reference_names_booths_and_staff_for_organisers_only(comp: Comp) -> None:
    door = comp.volunteer("MASTERCLASS_DOOR", comp.masterclass(comp.b1, capacity=5))
    assert door.get("/api/staff/id-reference").status_code == 403
    body = comp.volunteer("GAME_MASTER", None).get("/api/staff/id-reference").json()
    assert body["booths"] and body["staff"]
    assert all(set(row) == {"id", "name"} for row in body["booths"] + body["staff"])
    assert "@" not in str(body)  # never an e-mail address


def test_the_card_short_code_turns_into_the_card_for_a_signed_in_caller_only(
    comp: Comp,
) -> None:
    from fastapi.testclient import TestClient

    from app.main import app
    from app.modules.contact_cards.service import card_short_code

    owner, other = comp.users(2)
    token = comp.card(owner)
    code = card_short_code(token)
    assert len(code) == 6 and not set(code) & set("01OIL")
    mine = comp.client(owner).get("/api/attendees/me/card").json()
    assert mine["short_code"] == code
    loose = f"{code[:3].lower()} {code[3:]}"  # typed loosely
    # A door volunteer gets the token, exactly what the QR would have given.
    door = comp.volunteer("GAME_MASTER", None)
    found = door.post("/api/staff/cards/lookup", json={"code": loose})
    assert found.status_code == 200 and found.json() == {"card_token": token}
    assert found.headers["cache-control"] == "no-store"
    # Another attendee too (People: connect by hand).
    them = comp.client(other).post("/api/attendees/me/cards/lookup", json={"code": code})
    assert them.json() == {"card_token": token}
    # Nobody signed in: refused; a wrong code: the same INVALID_QR as a bad scan.
    anon = TestClient(app).post("/api/attendees/me/cards/lookup", json={"code": code})
    assert anon.status_code in (401, 403)  # no session (or no same-origin proof)
    wrong = "".join("2" if c != "2" else "3" for c in code)
    assert door.post("/api/staff/cards/lookup", json={"code": wrong}).json()["error"]["code"] == (
        "INVALID_QR"
    )
    # A revoked card's code names no one.
    comp.rig.sql("UPDATE contact_cards SET revoked_at = now() WHERE user_id = :u", u=owner)
    gone = door.post("/api/staff/cards/lookup", json={"code": code})
    assert gone.json()["error"]["code"] == "INVALID_QR"


def test_the_six_digit_code_scans_like_the_qr_and_wrong_digits_do_not(comp: Comp) -> None:
    from app.modules.competition import panels as panels_module
    from app.modules.competition.models import Panel

    uid = comp.users(1)[0]
    with Session(comp.rig.engine) as db:
        row = db.get(Panel, comp.panel1)
        assert row is not None
        entry = panels_module.short_code(row, "ENTRY")
        exit_ = panels_module.short_code(row, "EXIT")
    assert entry != exit_ and len(entry) == 6 and entry.isdigit()
    c = comp.client(uid)
    comp.clock.now = baku("10:05")
    wrong = f"{(int(entry) + 1) % 1_000_000:06d}"
    bad = c.post("/api/attendees/me/panels/scan", json={"code": wrong})
    assert bad.json()["error"]["details"]["reason"] == "invalid_code"
    ok = c.post("/api/attendees/me/panels/scan", json={"code": entry})
    assert ok.status_code == 200 and ok.json()["phase"] == "entry"
    comp.clock.now = baku("11:25")
    done = c.post("/api/attendees/me/panels/scan", json={"code": f"{exit_[:3]}-{exit_[3:]}"})
    assert done.json()["award"] == {"outcome": "awarded", "points": 8}
    # Rotating the panel's codes revokes the digits too.
    comp.rig.sql("UPDATE panels SET qr_version = qr_version + 1 WHERE id = :p", p=comp.panel1)
    comp.clock.now = baku("10:05")
    other = comp.client(comp.users(1)[0])
    stale = other.post("/api/attendees/me/panels/scan", json={"code": entry})
    assert stale.json()["error"]["details"]["reason"] == "invalid_code"


def test_a_mentor_award_scores_nothing_since_meet_the_mentor_left(comp: Comp) -> None:
    uid = comp.users(1)[0]
    assert _award(comp, uid, PointsSource.MENTOR, "once", 5, baku("13:30")) == "not_scored"
    assert comp.rig.scalar("SELECT count(*) FROM points_log WHERE user_id = :u", u=uid) == 0


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
    # Only after the panel (10:00-11:30): during it the Game Master is refused.
    comp.clock.now = baku("11:00")
    during = gm.post("/api/staff/qmeter/award", json=body)
    assert during.status_code >= 400 and "panel_not_ended" in during.text
    assert comp.points(uid, "QMETER") == 0
    comp.clock.now = baku("11:40")
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


def test_one_facemark_login_works_every_booth_but_only_for_admitted_people(comp: Comp) -> None:
    uid, stranger = comp.users(2)
    slot = _admitted_playground(comp, uid, 10)  # P10: 11:15-11:30
    mc = comp.masterclass(comp.b1, capacity=5)
    scanner = comp.volunteer("MASTERCLASS_DOOR", mc)  # no booth of its own
    outsider = comp.volunteer("MENTOR_HOST", 1)  # no door, no Game Master
    url = f"/api/staff/playground/slots/{slot}"
    comp.clock.now = baku("11:28")

    assert outsider.get(url).status_code == 403
    assert outsider.post(f"{url}/complete", json={"card_token": comp.card(uid)}).status_code == 403
    assert scanner.get(url).status_code == 200  # the arrivals list

    # A wrong session or person is harmless: only someone admitted to it scores.
    wrong = scanner.post(f"{url}/complete", json={"card_token": comp.card(stranger)})
    assert reason(wrong) == "not_admitted"
    done = scanner.post(f"{url}/complete", json={"card_token": comp.card(uid)})
    assert done.json()["award"] == {"outcome": "awarded", "points": 3}

    # Revoking the door scope stops the next request.
    comp.rig.sql("UPDATE staff_scopes SET revoked_at = now() WHERE scope = 'MASTERCLASS_DOOR'")
    assert scanner.get(url).status_code == 403


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


# --- the tie-break: when a total was reached (PDF conformance audit 2026-10-18, P1) ---------


def _ledger_row(
    comp: Comp,
    uid: int,
    source: str,
    points: int,
    created: str,
    occurred: str | None = None,
    reverses: int | None = None,
) -> int:
    """One ledger row as the writers leave it: `created` is when it was recorded,
    `occurred` when the activity happened (NULL on rows from before 0027)."""
    return int(
        comp.rig.scalar(
            "INSERT INTO points_log (user_id, source, entry_type, points, entered_by_system, "
            "dedupe_key, reverses_points_log_id, created_at, occurred_at) "
            "VALUES (:u, :s, :e, :p, true, :k, :r, :c, :o) RETURNING id",
            u=uid,
            s=source,
            e="REVERSAL" if reverses is not None else "AWARD",
            p=points,
            k=None if reverses is not None else f"COMP:{source}:{uid}:{uuid4().hex}",
            r=reverses,
            c=baku(created),
            o=baku(occurred) if occurred else None,
        )
    )


def _orders(comp: Comp, uids: list[int]) -> dict[str, list[int]]:
    """The competition order of `uids` in every place a ranking is read: the live
    top N, every page of the full list, the pushed (SSE) payload and the final
    snapshot."""
    from app.modules.competition.final import build_payload
    from app.modules.leaderboard.service import (
        invalidate_ranking_snapshot,
        leaderboard_push_payload,
    )

    comp.rig.sql("UPDATE event_config SET competition_mode = true, leaderboard_top_n = 100")
    invalidate_ranking_snapshot()
    try:
        viewer = comp.client(uids[0])
        top = viewer.get("/api/leaderboard").json()["entries"]
        paged: list[int] = []
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {"view": "all", "limit": 2}
            if cursor:
                params["cursor"] = cursor
            page = viewer.get("/api/leaderboard", params=params).json()
            paged += [e["user_id"] for e in page["entries"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        with Session(comp.rig.engine) as db:
            pushed = leaderboard_push_payload(db)["entries"]
            final = build_payload(db)["ranking"]
        assert isinstance(pushed, list)
        return {
            "top": [e["user_id"] for e in top if e["user_id"] in uids],
            "pages": [u for u in paged if u in uids],
            "pushed": [e["user_id"] for e in pushed if e["user_id"] in uids],
            "final": [e["user_id"] for e in final if e["user_id"] in uids],
        }
    finally:
        comp.rig.sql("UPDATE event_config SET competition_mode = false, leaderboard_top_n = 10")
        invalidate_ranking_snapshot()


def _everywhere(comp: Comp, uids: list[int], expected: list[int]) -> None:
    orders = _orders(comp, uids)
    assert orders == dict.fromkeys(orders, expected), orders


def test_a_reversed_award_never_dates_the_total(comp: Comp) -> None:
    a, b = comp.users(2)
    _ledger_row(comp, a, "PANEL", 8, "11:25", "11:25")
    mentor = _ledger_row(comp, a, "MENTOR", 5, "13:56", "13:55")
    _ledger_row(comp, a, "MENTOR", -5, "16:30", reverses=mentor)  # a correction
    _ledger_row(comp, b, "PANEL", 8, "11:28", "11:28")
    # Both hold 8. A has held it since 11:25 by the awards that still stand; the
    # correction at 16:30 and the award it cancelled date nothing.
    _everywhere(comp, [a, b], [a, b])


def test_a_better_connection_replacing_a_weaker_one_dates_the_total_by_the_replacement(
    comp: Comp,
) -> None:
    a, b, c = comp.users(3)
    weakest = 0
    for minute in ("10:00", "10:01", "10:02", "10:03"):
        weakest = _ledger_row(comp, a, "CONNECTION", 1, minute, minute)
    _ledger_row(comp, a, "CONNECTION", -1, "12:00", reverses=weakest)  # best four: replaced
    _ledger_row(comp, a, "CONNECTION", 3, "12:00", "12:00")  # A: 6 since 12:00
    _ledger_row(comp, b, "CONNECTION", 3, "11:00", "11:00")
    _ledger_row(comp, b, "CONNECTION", 3, "11:30", "11:30")  # B: 6 since 11:30
    _ledger_row(comp, c, "CONNECTION", 3, "12:10", "12:10")
    _ledger_row(comp, c, "CONNECTION", 3, "12:30", "12:30")  # C: 6 since 12:30
    _everywhere(comp, [a, b, c], [b, a, c])


def test_old_rows_founder_corrections_zero_points_and_identical_moments(comp: Comp) -> None:
    old, late, founder, same1, same2, cancelled, never = comp.users(7)
    # A row from before 0027 has no occurrence time: it is dated by its recording.
    _ledger_row(comp, old, "PROFILE", 5, "10:00")
    # Verified as having happened at 09:30, recorded only after the close.
    _ledger_row(comp, late, "PROFILE", 5, "17:40", "09:30")
    # A Founder bonus later reversed by a correction: what is left was reached 10:30.
    _ledger_row(comp, founder, "PROFILE", 5, "10:30", "10:30")
    bonus = _ledger_row(comp, founder, "FOUNDER", 10, "12:00", "12:00")
    _ledger_row(comp, founder, "FOUNDER", -10, "14:00", reverses=bonus)
    # The very same moment: the stable user id decides.
    _ledger_row(comp, same1, "PROFILE", 5, "11:00", "11:00")
    _ledger_row(comp, same2, "PROFILE", 5, "11:00", "11:00")
    # Zero points: a fully reversed person stands exactly like one who never
    # scored (no moment of reaching anything), then the user id decides.
    gone = _ledger_row(comp, cancelled, "PROFILE", 5, "08:30", "08:30")
    _ledger_row(comp, cancelled, "PROFILE", -5, "09:00", reverses=gone)
    _everywhere(
        comp,
        [old, late, founder, same1, same2, cancelled, never],
        [late, old, founder, same1, same2, cancelled, never],
    )


# --- the personal report (PDF conformance audit 2026-10-18, P2) ----------------------------


def test_the_report_tells_a_completed_masterclass_from_one_only_entered(comp: Comp) -> None:
    first = comp.masterclass(comp.b1, capacity=5)
    second = comp.masterclass(comp.b2, capacity=5)
    (uid,) = comp.users(1)
    attendee = comp.client(uid)
    assert choose(attendee, comp.b1, first).status_code == 200
    assert choose(attendee, comp.b2, second).status_code == 200
    token = comp.card(uid)
    vol1 = comp.volunteer("MASTERCLASS_DOOR", first)
    vol2 = comp.volunteer("MASTERCLASS_DOOR", second)
    comp.clock.now = baku("11:32")
    door(vol1, first, token, "ENTRY")
    comp.clock.now = baku("12:55")
    assert door(vol1, first, token, "EXIT").json()["award"]["outcome"] == "awarded"
    comp.clock.now = baku("16:02")
    door(vol2, second, token, "ENTRY")  # the exit of this one is never scanned

    body = attendee.get("/api/attendees/me/report").json()
    assert [(i["kind"], i["id"], i["status"]) for i in body["attended"]] == [
        ("masterclass", first, "attended")
    ]
    assert [(i["kind"], i["id"], i["status"]) for i in body["not_completed"]] == [
        ("masterclass", second, "entered")
    ]
    # Unchanged point rule: only the completed one paid.
    assert comp.points(uid, "MASTERCLASS") == 12


# --- event readiness (PDF conformance audit 2026-10-18, item 6) ---------------------------


_COUNTED_TABLES = (
    "masterclass_events",
    "masterclasses",
    "panels",
    "playground_slots",
    "mentor_sessions",
    "match_tables",
    "staff_scopes",
    "points_log",
    "admin_audit",
    "users",
)


def _row_counts(comp: Comp) -> dict[str, int]:
    return {t: int(comp.rig.scalar(f"SELECT count(*) FROM {t}")) for t in _COUNTED_TABLES}


def test_the_readiness_report_names_what_is_missing_and_changes_nothing(
    comp: Comp, capsys: pytest.CaptureFixture[str]
) -> None:
    from app import cli
    from app.modules.competition.readiness import check, ready

    comp.rig.sql(
        "UPDATE users SET first_name = 'Anar', last_name = 'Nəsirov', "
        "email = 'anar.private@mail.test' WHERE id = :u",
        u=comp.users(1)[0],
    )
    before = _row_counts(comp)
    with Session(comp.rig.engine) as db:
        checks = {c.key: c for c in check(db)}
        assert not db.new and not db.dirty and not db.deleted
    assert _row_counts(comp) == before  # read-only
    # The fixture: the skeleton (one event, 2 blocks, 5 rooms), 2 panels, the
    # 17:30 close - but no published masterclass, no Playground, no mentors.
    status = {k: c.status for k, c in checks.items()}
    assert status["event"] == "ok" and "Asia/Baku" in checks["event"].detail
    assert status["masterclass_blocks"] == "ok" and status["masterclass_rooms"] == "ok"
    assert status["panels"] == "ok" and status["competition_close"] == "ok"
    assert "17:30" in checks["competition_close"].detail
    for missing in (
        "masterclasses_published",
        "competition_mode",
        "playground_slots",
        "game_masters",
    ):
        assert status[missing] == "missing", missing
    # No mentor group and no Match Corner table: not in the 13 Nov programme.
    for unused in ("mentor_sessions", "match_corner_tables"):
        assert status[unused] == "info", unused
    assert "mentor_schedule" not in status and "mentor_hosts" not in status
    assert status["founder_rule"] == status["reminders"] == "info"
    assert not ready(list(checks.values()))

    # The command prints the same, exits 1 while something is missing, and never
    # names a person or prints contact data.
    with pytest.raises(SystemExit) as exit_:
        cli.main(["competition-report"])
    assert exit_.value.code == 1
    out = capsys.readouterr().out
    assert "event readiness: NOT READY" in out and "[missing] playground_slots" in out
    for private in ("Anar", "Nəsirov", "anar.private", "@", "password"):
        assert private not in out, private
    assert _row_counts(comp) == before


def test_the_readiness_report_sees_a_configured_door_host_booth_and_close_time(
    comp: Comp,
) -> None:
    from app.modules.competition.readiness import check

    mc = comp.masterclass(comp.b1, capacity=5)
    comp.rig.sql("UPDATE masterclasses SET is_published = true WHERE id = :m", m=mc)
    comp.volunteer("MASTERCLASS_DOOR", mc)
    comp.volunteer("GAME_MASTER", None)
    comp.rig.sql("UPDATE masterclass_events SET competition_closes_at = :c", c=baku("17:00"))
    with Session(comp.rig.engine) as db:
        checks = {c.key: c for c in check(db)}
    assert checks["masterclass_door_scopes"].status == "ok"
    assert checks["game_masters"].status == "ok"
    # Block 2 still has nothing published.
    assert checks["masterclasses_published"].status == "missing"
    # A close at the wrong time is not "set".
    assert checks["competition_close"].status == "missing"
    assert "expected 17:30" in checks["competition_close"].detail


def test_readiness_rejects_wrong_event_date_and_a_count_correct_but_wrong_timeline(
    comp: Comp,
) -> None:
    from sqlalchemy import text

    from app.modules.competition.readiness import check

    comp.rig.sql("UPDATE masterclass_events SET event_date = '2026-11-14'")
    comp.rig.sql("UPDATE masterclass_blocks SET change_deadline = starts_at")
    comp.rig.sql("UPDATE panels SET starts_at = starts_at + interval '1 minute'")
    with Session(comp.rig.engine) as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        checks = {c.key: c for c in check(db)}
    assert checks["event"].status == "missing"
    assert checks["masterclass_blocks"].status == "ok"  # two rows is not enough
    assert checks["masterclass_schedule"].status == "missing"
    assert checks["panels"].status == "ok"
    assert checks["panel_schedule"].status == "missing"


def test_readiness_does_not_accept_one_booth_one_table_or_unpublished_mentor_times(
    comp: Comp,
) -> None:
    from app.modules.competition.readiness import check

    booth = int(comp.rig.scalar("SELECT id FROM booths WHERE is_active ORDER BY id LIMIT 1"))
    with Session(comp.rig.engine) as db:
        pg_service.prepare(db, comp.event, booth)
        db.commit()
    comp.rig.sql(
        "INSERT INTO match_tables (event_id, label, is_active) VALUES (:e, 'Table 1', true)",
        e=comp.event,
    )
    with Session(comp.rig.engine) as db:
        checks = {c.key: c for c in check(db)}
    assert checks["playground_slots"].status == "missing"  # PDF needs 8-15 booths
    assert checks["playground_schedule"].status == "ok"
    assert checks["match_corner_tables"].status == "missing"  # PDF suggests 4-6
    comp.rig.sql(
        "INSERT INTO mentor_sessions "
        "(event_id, code, title, starts_at, ends_at, capacity, is_open) "
        "VALUES (:e, 'mentor-x', 'Test mentor session', :s, :f, 6, false)",
        e=comp.event,
        s=baku("13:10"),
        f=baku("13:30"),
    )
    with Session(comp.rig.engine) as db:
        checks = {c.key: c for c in check(db)}
    assert checks["mentor_schedule"].status == "missing"
    comp.rig.sql(
        "UPDATE playground_slots SET starts_at = starts_at + interval '1 minute', "
        "ends_at = ends_at + interval '1 minute' WHERE number = 1"
    )
    with Session(comp.rig.engine) as db:
        checks = {c.key: c for c in check(db)}
    assert checks["playground_schedule"].status == "missing"


def test_readiness_accepts_a_complete_programme_without_writing(comp: Comp) -> None:
    from sqlalchemy import text

    from app.modules.competition.readiness import check, ready

    comp.rig.sql("UPDATE event_config SET competition_mode = true")
    # The 13 Nov agenda: Panel 1 at 10:20-11:30.
    comp.rig.sql("UPDATE panels SET starts_at = :s WHERE id = :p", s=baku("10:20"), p=comp.panel1)
    comp.rig.sql(
        "INSERT INTO panels (event_id, code, title, starts_at, ends_at) "
        "VALUES (:e, 'grand-final', 'Grand Final', :s, :f)",
        e=comp.event,
        s=baku("17:25"),
        f=baku("17:45"),
    )
    for block in (comp.b1, comp.b2):
        for _ in range(5):
            comp.volunteer("MASTERCLASS_DOOR", comp.masterclass(block))
    comp.volunteer("GAME_MASTER", None)
    # All resources below belong only to this test's disposable database.
    for n in range(8):
        booth = int(
            comp.rig.scalar(
                "INSERT INTO booths (slug, name, is_active) "
                "VALUES (:s, 'Test booth', true) RETURNING id",
                s=f"readiness-{n}",
            )
        )
        with Session(comp.rig.engine) as db:
            pg_service.prepare(db, comp.event, booth)
            db.commit()
        comp.rig.sql(
            "INSERT INTO staff_accounts "
            "(email, password_hash, full_name, role, booth_id, is_active) "
            "VALUES (:e, 'x', 'Test staff', 'BOOTH_STAFF', :b, true)",
            e=f"readiness-{n}@staff.test",
            b=booth,
        )
    for n in range(4):
        comp.rig.sql(
            "INSERT INTO match_tables (event_id, label, is_active) VALUES (:e, :l, true)",
            e=comp.event,
            l=f"Table {n + 1}",
        )
    for n, at in enumerate(("13:10", "13:35", "15:35")):
        sid = int(
            comp.rig.scalar(
                "INSERT INTO mentor_sessions "
                "(event_id, code, title, starts_at, ends_at, capacity, is_open) "
                "VALUES (:e, :c, 'Test mentor session', :s, :f, 6, true) RETURNING id",
                e=comp.event,
                c=f"mentor-{n}",
                s=baku(at),
                f=baku(at) + timedelta(minutes=20),
            )
        )
        comp.volunteer("MENTOR_HOST", sid)
    before = _row_counts(comp)
    with Session(comp.rig.engine) as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        checks = check(db)
    assert ready(checks), [(c.key, c.detail) for c in checks if c.status == "missing"]
    assert _row_counts(comp) == before


# --- the FACEMARK organiser workspace -----------------------------------------------------

ORGANISER = "/api/staff/organiser"


def _workspace(c: TestClient) -> httpx.Response:
    return cast(httpx.Response, c.get(ORGANISER))


def test_only_administrators_and_game_masters_open_the_organiser_workspace(comp: Comp) -> None:
    anonymous = TestClient(app)
    assert _workspace(anonymous).status_code == 401
    (uid,) = comp.users(1)
    attendee = comp.client(uid)
    assert _workspace(attendee).status_code in (401, 403)
    assert "sections" not in _workspace(attendee).text
    _, booth_staff = comp.rig.booth_with_staff()
    assert _workspace(comp.staff_client(booth_staff)).json()["error"]["code"] == (
        "STAFF_NOT_AUTHORIZED"
    )
    mc = comp.masterclass(comp.b1, capacity=5)
    door_volunteer = comp.volunteer("MASTERCLASS_DOOR", mc)
    assert _workspace(door_volunteer).status_code == 403

    gm = comp.volunteer("GAME_MASTER", None)
    body = _workspace(gm).json()
    assert body["title"] == "FACEMARK — Organiser Panel" and body["game_master"] is True
    assert body["sections"] == {
        "overview": True,
        "scanning": True,
        "qmeter": True,
        "corrections": True,  # a Game Master corrects points on the day (25 Oct 2026)
        "final": True,
        "screens": True,
    }
    assert body["panel_codes"] is True  # FACEMARK runs the Main Stage screens
    assert _workspace(gm).headers["cache-control"] == "no-store"

    admin = comp.staff_client(comp.rig.staff_id(StaffRole.ADMIN))
    sections = _workspace(admin).json()["sections"]
    assert sections["corrections"] is True and sections["final"] is True
    assert sections["qmeter"] is False  # Qmeter needs the Game Master scope itself
    assert _workspace(admin).json()["panel_codes"] is True


def test_the_workspace_grants_nothing_each_tool_keeps_its_own_check(comp: Comp) -> None:
    gm = comp.volunteer("GAME_MASTER", None)
    assert _workspace(gm).status_code == 200
    (uid,) = comp.users(1)
    entry = int(
        comp.rig.scalar(
            "INSERT INTO points_log (user_id, source, points, entered_by_system, dedupe_key) "
            "VALUES (:u, 'PROFILE', 5, true, :k) RETURNING id",
            u=uid,
            k=f"COMP:PROFILE:{uid}:profile",
        )
    )
    # A Game Master in the workspace still cannot reverse points (administrators only).
    refused = gm.post(
        f"/api/admin/points-log/{entry}/reverse",
        json={"reason": "Wrong person", "idempotency_key": str(uuid4())},
    )
    assert refused.status_code in (401, 403)
    assert int(comp.rig.scalar("SELECT count(*) FROM points_log WHERE user_id = :u", u=uid)) == 1
    # An administrator without the Game Master scope cannot record Qmeter.
    admin = comp.staff_client(comp.rig.staff_id(StaffRole.ADMIN))
    q = admin.post(
        "/api/staff/qmeter/identify", json={"full_name": "Guest N1", "phone": "+10000000"}
    )
    assert q.status_code == 403


def test_a_revoked_game_master_scope_or_account_closes_the_workspace_at_once(comp: Comp) -> None:
    gm = comp.volunteer("GAME_MASTER", None)
    assert _workspace(gm).status_code == 200
    comp.rig.sql("UPDATE staff_scopes SET revoked_at = now() WHERE scope = 'GAME_MASTER'")
    assert _workspace(gm).status_code == 403
    other = comp.volunteer("GAME_MASTER", None)
    assert _workspace(other).status_code == 200
    comp.rig.sql(
        "UPDATE staff_accounts SET is_active = false WHERE id IN "
        "(SELECT staff_account_id FROM staff_scopes WHERE revoked_at IS NULL "
        "AND scope = 'GAME_MASTER')"
    )
    assert _workspace(other).status_code in (401, 403)


def test_the_admin_readiness_endpoint_is_read_only_admin_only_and_private(comp: Comp) -> None:
    (uid,) = comp.users(1)
    comp.rig.sql(
        "UPDATE users SET last_name = 'Nəsirov', email = 'anar.private@mail.test' WHERE id = :u",
        u=uid,
    )
    before = _row_counts(comp)
    admin = comp.staff_client(comp.rig.staff_id(StaffRole.ADMIN))
    r = admin.get("/api/admin/readiness")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    keys = {c["key"]: c["status"] for c in body["checks"]}
    assert body["ready"] is False
    assert keys["event"] == "ok" and keys["playground_slots"] == "missing"
    assert keys["founder_rule"] == "info"
    assert "Nəsirov" not in r.text and "@" not in r.text
    assert _row_counts(comp) == before
    for c in (comp.volunteer("GAME_MASTER", None), comp.client(uid), TestClient(app)):
        assert c.get("/api/admin/readiness").status_code in (401, 403)
