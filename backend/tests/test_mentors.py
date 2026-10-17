"""Meet the Mentor: configured groups, bookings, the host's scan and +5 once.

Rig database (disposable, freshly seeded); server clocks replaced per test.
Times below are Asia/Baku (UTC+4) on 13 Nov 2026.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.security import generate_card_token
from app.main import app
from app.modules.masterclasses import service as mc_service
from app.modules.mentors import routers as mentor_routers
from app.modules.mentors import service
from app.modules.playground import service as pg_service
from app.modules.staff.models import StaffRole
from tests.api_rig import Rig
from tests.test_masterclasses import Program, choose, parallel


def baku(hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 11, 13, h - 4, m, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = baku("08:00")

    def __call__(self) -> datetime:
        return self.now


class Mentors(Program):
    clock: Clock

    def session(
        self, start: str, *, capacity: int = 6, waitlist: int = 3, code: str | None = None
    ) -> int:
        return int(
            self.rig.scalar(
                "INSERT INTO mentor_sessions (event_id, code, title, mentor_name, starts_at, "
                "ends_at, capacity, waitlist_capacity) VALUES (:e, :c, 'Careers in AI', "
                "'Configured Mentor', :s, :s + interval '20 minutes', :cap, :w) RETURNING id",
                e=self.event,
                c=code or f"m-{uuid4().hex[:8]}",
                s=baku(start),
                cap=capacity,
                w=waitlist,
            )
        )

    def card(self, uid: int) -> str:
        # One live card per person (uq_contact_card_live_user): issued once, reused.
        existing = self.rig.sql(
            "SELECT public_token FROM contact_cards WHERE user_id = :u AND revoked_at IS NULL",
            u=uid,
        )
        if existing:
            return str(existing[0][0])
        token = generate_card_token()
        self.rig.sql(
            "INSERT INTO contact_cards (user_id, public_token, issued_at) VALUES (:u, :t, now())",
            u=uid,
            t=token,
        )
        return token

    def host(self, session_id: int) -> tuple[int, TestClient]:
        sid = int(
            self.rig.scalar(
                "INSERT INTO staff_accounts (email, password_hash, full_name, role, is_active) "
                "VALUES (:e, 'x', 'Host', 'VOLUNTEER', true) RETURNING id",
                e=f"host-{uuid4().hex[:8]}@staff.test",
            )
        )
        self.rig.sql(
            "INSERT INTO staff_scopes (staff_account_id, scope, target_id, granted_by_staff_id) "
            "VALUES (:s, 'MENTOR_HOST', :t, :a)",
            s=sid,
            t=session_id,
            a=self.rig.staff_id(StaffRole.ADMIN),
        )
        c = TestClient(app, headers={"Origin": get_settings().app_origin})
        c.cookies.set("session", self.rig.session_for(sid))
        self.clients.append(c)
        return sid, c

    def statuses(self, session_id: int) -> list[str]:
        return [
            str(r[0])
            for r in self.rig.sql(
                "SELECT status FROM mentor_bookings WHERE session_id = :s ORDER BY id",
                s=session_id,
            )
        ]

    def mentor_points(self, uid: int) -> int:
        return int(
            self.rig.scalar(
                "SELECT coalesce(sum(points), 0) FROM points_log "
                "WHERE user_id = :u AND source = 'MENTOR'",
                u=uid,
            )
        )


@pytest.fixture
def mt(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Iterator[Mentors]:
    clock = Clock()
    monkeypatch.setattr(service, "now", clock)
    monkeypatch.setattr(mentor_routers, "_now", clock)
    monkeypatch.setattr(mc_service, "server_now", clock)
    monkeypatch.setattr(pg_service, "now", clock)
    p = Mentors(rig)
    p.clock = clock
    rig.sql("UPDATE masterclass_events SET competition_closes_at = :c", c=baku("17:30"))
    yield p
    for c in p.clients:
        c.close()


def listing(c: TestClient, session_id: int) -> dict[str, Any]:
    r = c.get("/api/mentor-sessions")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    return cast(dict[str, Any], next(s for s in r.json()["sessions"] if s["id"] == session_id))


def book(c: TestClient, session_id: int, command: str | None = None) -> httpx.Response:
    return cast(
        httpx.Response,
        c.post(
            f"/api/mentor-sessions/{session_id}/book",
            json={
                "command_id": command or str(uuid4()),
                "expected_revision": listing(c, session_id)["revision"],
            },
        ),
    )


def cancel(c: TestClient, booking: dict[str, Any]) -> httpx.Response:
    return cast(
        httpx.Response,
        c.post(
            f"/api/mentor-bookings/{booking['id']}/cancel",
            json={"expected_version": booking["version"]},
        ),
    )


def reason(r: httpx.Response) -> str:
    return str(r.json()["error"]["details"]["reason"])


def complete(c: TestClient, session_id: int, token: str) -> httpx.Response:
    return cast(
        httpx.Response,
        c.post(f"/api/staff/mentor-sessions/{session_id}/complete", json={"card_token": token}),
    )


def test_seats_then_fifo_waiting_list_then_full_and_cancel_promotes(mt: Mentors) -> None:
    s = mt.session("13:10", capacity=2, waitlist=2)
    a, b, c, d, e = [mt.client(u) for u in mt.users(5)]
    assert book(a, s).json()["status"] == "confirmed"
    assert book(b, s).json()["status"] == "confirmed"
    first_waiting = book(c, s).json()
    assert first_waiting["status"] == "waitlisted"
    assert book(d, s).json()["status"] == "waitlisted"
    assert listing(d, s)["booking"]["position"] == 2
    assert reason(book(e, s)) == "session_full"
    seat = listing(a, s)["booking"]
    assert cancel(a, seat).status_code == 200
    # The first person waiting gets the seat; the second moves up, nobody skips.
    assert listing(c, s)["booking"]["status"] == "confirmed"
    assert listing(d, s)["booking"]["position"] == 1
    assert listing(c, s)["seats_left"] == 0
    assert mt.rig.scalar("SELECT count(*) FROM points_log") == 0  # booking is not attendance


def test_one_active_booking_at_a_time_and_cancel_restores(mt: Mentors) -> None:
    first, second = mt.session("13:10"), mt.session("15:35")
    c = mt.client(mt.users(1)[0])
    mine = book(c, first).json()
    assert reason(book(c, second)) == "one_active_booking"
    assert cancel(c, mine).status_code == 200
    assert book(c, second).json()["status"] == "confirmed"


def test_retry_and_stale_revision(mt: Mentors) -> None:
    s = mt.session("13:10")
    c = mt.client(mt.users(1)[0])
    command = str(uuid4())
    stale = listing(c, s)["revision"]
    one = book(c, s, command).json()
    again = c.post(
        f"/api/mentor-sessions/{s}/book", json={"command_id": command, "expected_revision": stale}
    )
    assert again.status_code == 200 and again.json()["id"] == one["id"]  # a lost response
    assert mt.statuses(s) == ["CONFIRMED"]
    assert cancel(c, one).status_code == 200
    refused = c.post(
        f"/api/mentor-sessions/{s}/book",
        json={"command_id": str(uuid4()), "expected_revision": stale},
    )
    assert reason(cast(httpx.Response, refused)) == "choice_changed"  # an old screen
    # A cancelled place shows as free again, and a fresh screen can rebook.
    assert listing(c, s)["booking"] is None
    assert book(c, s).json()["status"] == "confirmed"


def test_booking_and_cancelling_close_when_the_session_starts(mt: Mentors) -> None:
    s = mt.session("13:10")
    a, b = [mt.client(u) for u in mt.users(2)]
    mine = book(a, s).json()
    mt.clock.now = baku("13:10")
    assert reason(book(b, s)) == "booking_closed"
    assert reason(cancel(a, mine)) == "cancellation_closed"
    assert listing(a, s)["bookable"] is False


def test_a_closed_session_is_hidden_and_takes_no_bookings(mt: Mentors) -> None:
    s = mt.session("13:10")
    mt.rig.sql("UPDATE mentor_sessions SET is_open = false WHERE id = :s", s=s)
    c = mt.client(mt.users(1)[0])
    assert all(x["id"] != s for x in c.get("/api/mentor-sessions").json()["sessions"])
    r = c.post(
        f"/api/mentor-sessions/{s}/book",
        json={"command_id": str(uuid4()), "expected_revision": "0"},
    )
    assert r.status_code == 404
    # Someone already holding a place still sees it, and can still cancel it.
    mt.rig.sql("UPDATE mentor_sessions SET is_open = true WHERE id = :s", s=s)
    mine = book(c, s).json()
    mt.rig.sql("UPDATE mentor_sessions SET is_open = false WHERE id = :s", s=s)
    shown = listing(c, s)
    assert shown["bookable"] is False and shown["booking"]["status"] == "confirmed"
    assert cancel(c, mine).status_code == 200


def test_the_last_seat_goes_to_exactly_one_of_two_parallel_requests(mt: Mentors) -> None:
    s = mt.session("13:10", capacity=1, waitlist=0)
    a, b = [mt.client(u) for u in mt.users(2)]

    def go(c: TestClient) -> int:
        return book(c, s).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(go, (a, b)))
    assert codes == [200, 409]
    assert mt.statuses(s) == ["CONFIRMED"]


def test_masterclass_and_mentor_race_cannot_reserve_overlapping_times(mt: Mentors) -> None:
    uid = mt.users(1)[0]
    masterclass_client, mentor_client = mt.client(uid), mt.client(uid)
    mc = mt.masterclass(mt.b1)
    session_id = mt.session("12:00")
    replies = parallel(
        [lambda: choose(masterclass_client, mt.b1, mc), lambda: book(mentor_client, session_id)]
    )
    assert sorted(r.status_code for r in replies) == [200, 409]
    assert (
        mt.rig.scalar(
            "SELECT (SELECT count(*) FROM masterclass_reservations WHERE user_id=:u "
            "AND status IN ('CONFIRMED','WAITLISTED')) + "
            "(SELECT count(*) FROM mentor_bookings WHERE user_id=:u "
            "AND status IN ('CONFIRMED','WAITLISTED'))",
            u=uid,
        )
        == 1
    )


def test_the_database_keeps_the_group_size_whoever_writes(mt: Mentors) -> None:
    s = mt.session("13:10", capacity=1)
    u1, u2 = mt.users(2)
    insert = (
        "INSERT INTO mentor_bookings (session_id, user_id, status, command_id) "
        "VALUES (:s, :u, 'CONFIRMED', :c)"
    )
    mt.rig.sql(insert, s=s, u=u1, c=str(uuid4()))
    with pytest.raises(IntegrityError) as caught:
        mt.rig.sql(insert, s=s, u=u2, c=str(uuid4()))
    assert caught.value.orig.diag.constraint_name == "ck_mentor_capacity"  # type: ignore[union-attr]
    with pytest.raises(IntegrityError):  # and never two active bookings of one person
        mt.rig.sql(
            "INSERT INTO mentor_bookings (session_id, user_id, status, command_id) "
            "VALUES (:s, :u, 'WAITLISTED', :c)",
            s=mt.session("15:35"),
            u=u1,
            c=str(uuid4()),
        )


def test_mentor_time_is_reserved_against_playground_both_ways(mt: Mentors) -> None:
    mt.rig.sign_in_as(mt.rig.staff_id(StaffRole.ADMIN))
    booth = int(mt.rig.scalar("SELECT id FROM booths ORDER BY id LIMIT 1"))
    prepared = mt.rig.client.post(
        "/api/admin/playground/prepare", json={"event_id": mt.event, "booth_id": booth}
    )
    assert prepared.status_code == 200, prepared.text
    p17 = int(mt.rig.scalar("SELECT id FROM playground_slots WHERE number = 17"))  # 13:00-13:15
    s = mt.session("13:10")
    a, b = [mt.client(u) for u in mt.users(2)]

    def pg_book(c: TestClient) -> httpx.Response:
        slot = next(x for x in c.get("/api/playground").json()["slots"] if x["id"] == p17)
        return cast(
            httpx.Response,
            c.post(
                f"/api/playground/slots/{p17}/book",
                json={"command_id": str(uuid4()), "expected_revision": slot["revision"]},
            ),
        )

    assert pg_book(a).status_code == 200
    assert reason(book(a, s)) == "playground_overlap"
    assert book(b, s).status_code == 200
    assert reason(pg_book(b)) == "mentor_overlap"


def test_mentor_time_is_reserved_against_masterclasses_both_ways(mt: Mentors) -> None:
    # Configurable times: a session placed at 12:50 overlaps block 1 (11:30-13:00).
    s = mt.session("12:50")
    mc = mt.masterclass(mt.b1)
    a, b = mt.users(2)
    ca, cb = mt.client(a), mt.client(b)
    assert choose(ca, mt.b1, mc).status_code == 200
    assert reason(book(ca, s)) == "masterclass_overlap"
    assert book(cb, s).json()["status"] == "confirmed"
    refused = choose(cb, mt.b1, mc)
    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["reason"] == "mentor_overlap"
    # A waiting mentor place reserves the time too.
    mt.rig.sql("UPDATE mentor_bookings SET status = 'WAITLISTED' WHERE user_id = :u", u=b)
    assert choose(cb, mt.b1, mc).json()["error"]["details"]["reason"] == "mentor_overlap"


def test_host_scan_pays_five_once_with_scope_and_window(mt: Mentors) -> None:
    s, later = mt.session("13:10"), mt.session("15:35")
    uid, stranger = mt.users(2)
    c = mt.client(uid)
    book(c, s)
    token = mt.card(uid)
    _, host = mt.host(s)
    _, other_host = mt.host(later)

    mt.clock.now = baku("13:20")
    assert reason(complete(host, s, token)) == "outside_window"  # the group is still talking
    mt.clock.now = baku("13:26")
    assert complete(other_host, s, token).status_code == 403  # another session's host
    assert reason(complete(host, s, mt.card(stranger))) == "not_booked"
    paid = complete(host, s, token)
    assert paid.status_code == 200, paid.text
    assert paid.json()["award"] == {"outcome": "awarded", "points": 5}
    assert paid.json()["name"] == "Guest N."
    again = complete(host, s, token)  # a double tap or a lost response
    assert again.json()["award"]["outcome"] == "already"
    assert mt.mentor_points(uid) == 5

    # A second group later the same day: welcome, but the 5 points are once in total.
    mt.clock.now = baku("14:00")
    assert book(c, later).json()["status"] == "confirmed"
    mt.clock.now = baku("15:52")
    second = complete(other_host, later, token)
    assert second.json()["award"]["outcome"] == "already"
    assert mt.mentor_points(uid) == 5
    assert mt.statuses(later) == ["ATTENDED"]


def test_a_revoked_host_scope_stops_the_next_scan(mt: Mentors) -> None:
    s = mt.session("13:10")
    uid = mt.users(1)[0]
    book(mt.client(uid), s)
    host_id, host = mt.host(s)
    mt.rig.sql("UPDATE staff_scopes SET revoked_at = now() WHERE staff_account_id = :h", h=host_id)
    mt.clock.now = baku("13:28")
    assert complete(host, s, mt.card(uid)).status_code == 403
    assert mt.mentor_points(uid) == 0


def test_waiting_person_present_at_the_end_only_within_the_group_size(mt: Mentors) -> None:
    s = mt.session("13:10", capacity=1, waitlist=2)
    seated, waiting = mt.users(2)
    book(mt.client(seated), s)
    book(mt.client(waiting), s)
    _, host = mt.host(s)
    mt.clock.now = baku("13:27")
    # The confirmed person's seat is theirs until the end: no seat for the waiting one.
    assert reason(complete(host, s, mt.card(waiting))) == "group_full"
    mt.rig.sql(
        "UPDATE mentor_bookings SET status = 'CANCELLED' WHERE user_id = :u", u=seated
    )  # e.g. withdrawn by the attendee before the start
    assert complete(host, s, mt.card(waiting)).json()["award"]["points"] == 5


def test_after_the_competition_close_the_scan_is_kept_without_points(mt: Mentors) -> None:
    s = mt.session("17:20")
    uid = mt.users(1)[0]
    book(mt.client(uid), s)
    _, host = mt.host(s)
    mt.clock.now = baku("17:38")
    r = complete(host, s, mt.card(uid))
    assert r.json()["award"]["outcome"] == "closed"
    assert mt.statuses(s) == ["ATTENDED"] and mt.mentor_points(uid) == 0


def test_a_panel_paid_for_the_same_time_is_reversed(mt: Mentors) -> None:
    panel = int(
        mt.rig.scalar(
            "INSERT INTO panels (event_id, code, title, starts_at, ends_at) "
            "VALUES (:e, 'p2', 'Panel 2', :s, :f) RETURNING id",
            e=mt.event,
            s=baku("14:00"),
            f=baku("15:30"),
        )
    )
    s = mt.session("15:00")  # configured over the panel's end
    uid = mt.users(1)[0]
    book(mt.client(uid), s)
    mt.rig.sql(
        "INSERT INTO points_log (user_id, source, entry_type, points, entered_by_system, "
        "dedupe_key) VALUES (:u, 'PANEL', 'AWARD', 8, true, :k)",
        u=uid,
        k=f"COMP:PANEL:{uid}:panel:{panel}",
    )
    _, host = mt.host(s)
    mt.clock.now = baku("15:18")
    assert complete(host, s, mt.card(uid)).json()["award"]["points"] == 5
    assert (
        mt.rig.scalar(
            "SELECT coalesce(sum(points), 0) FROM points_log "
            "WHERE user_id = :u AND source = 'PANEL'",
            u=uid,
        )
        == 0
    )
    assert mt.rig.scalar("SELECT count(*) FROM points_log WHERE entry_type = 'REVERSAL'") == 1


def test_host_sees_only_short_names_of_their_own_group(mt: Mentors) -> None:
    s, other = mt.session("13:10"), mt.session("15:35")
    uid = mt.users(1)[0]
    book(mt.client(uid), s)
    _, host = mt.host(s)
    r = host.get(f"/api/staff/mentor-sessions/{s}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["people"] == [{"name": "Guest N.", "status": "confirmed"}]
    assert "@" not in r.text and "+1000" not in r.text
    assert host.get(f"/api/staff/mentor-sessions/{other}").status_code == 403
    assert [x["id"] for x in host.get("/api/staff/mentor-sessions").json()["sessions"]] == [s]


def test_admin_configures_sessions_with_audit_and_safe_edits(mt: Mentors) -> None:
    mt.rig.sign_in_as(mt.rig.staff_id(StaffRole.ADMIN))
    admin = mt.rig.client
    created = admin.post(
        "/api/admin/mentor-sessions",
        json={
            "event_id": mt.event,
            "code": "m-1310",
            "title": "Careers in AI",
            "starts_at": "2026-11-13T13:10:00+04:00",
            "capacity": 1,
            "waitlist_capacity": 1,
        },
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    assert created.json()["ends_at"] == "2026-11-13T09:30:00+00:00"
    assert created.json()["mentor_name"] is None  # nobody invented
    assert (
        admin.post(
            "/api/admin/mentor-sessions",
            json={
                "event_id": mt.event,
                "code": "m-1310",
                "title": "x",
                "starts_at": "2026-11-13T13:35:00+04:00",
            },
        ).status_code
        == 409
    )
    assert (
        admin.post(
            "/api/admin/mentor-sessions",
            json={
                "event_id": mt.event,
                "code": "m-big",
                "title": "x",
                "capacity": 7,
                "starts_at": "2026-11-13T13:35:00+04:00",
            },
        ).status_code
        == 422
    )
    a, b = mt.users(2)
    book(mt.client(a), sid)
    book(mt.client(b), sid)
    moved = admin.patch(
        f"/api/admin/mentor-sessions/{sid}", json={"starts_at": "2026-11-13T13:35:00+04:00"}
    )
    assert moved.status_code == 409  # people planned their day around it
    assert admin.patch(f"/api/admin/mentor-sessions/{sid}", json={"capacity": 2}).status_code == 200
    assert mt.statuses(sid) == ["CONFIRMED", "CONFIRMED"]  # the waiting person was seated
    assert admin.patch(f"/api/admin/mentor-sessions/{sid}", json={"capacity": 1}).status_code == 409
    named = admin.patch(f"/api/admin/mentor-sessions/{sid}", json={"mentor_name": "Announced Name"})
    assert named.json()["mentor_name"] == "Announced Name"
    actions = [
        str(r[0])
        for r in mt.rig.sql(
            "SELECT action FROM admin_audit WHERE target_type = 'mentor_session' ORDER BY id"
        )
    ]
    assert actions == ["mentor_session.create", "mentor_session.update", "mentor_session.update"]
    assert len(admin.get("/api/admin/mentor-sessions").json()) == 1


def test_a_block_cannot_be_moved_onto_attendees_mentor_time(mt: Mentors) -> None:
    mc = mt.masterclass(mt.b1)
    uid = mt.users(1)[0]
    c = mt.client(uid)
    assert choose(c, mt.b1, mc).status_code == 200
    s = mt.session("13:10")
    assert book(c, s).status_code == 200
    mt.rig.sign_in_as(mt.rig.staff_id(StaffRole.ADMIN))
    r = mt.rig.client.patch(
        f"/api/admin/masterclass-blocks/{mt.b1}",
        json={"ends_at": "2026-11-13T13:20:00+04:00"},
    )
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "mentor_overlap"


# --- the personal programme (competition/programme.py) ----------------------------------------


def test_the_personal_programme_lists_the_day_in_time_order_with_statuses(mt: Mentors) -> None:
    mt.rig.sign_in_as(mt.rig.staff_id(StaffRole.ADMIN))
    booth = int(mt.rig.scalar("SELECT id FROM booths ORDER BY id LIMIT 1"))
    mt.rig.client.post(
        "/api/admin/playground/prepare", json={"event_id": mt.event, "booth_id": booth}
    )
    p1 = int(mt.rig.scalar("SELECT id FROM playground_slots WHERE number = 1"))  # 09:00
    mc = mt.masterclass(mt.b1, capacity=1)
    late = mt.masterclass(mt.b2)
    uid, other = mt.users(2)
    c = mt.client(uid)
    choose(mt.client(other), mt.b1, mc)
    assert choose(c, mt.b1, mc, join_waitlist=True).status_code == 200  # full: a waiting place
    assert choose(c, mt.b2, late).status_code == 200
    slot = next(x for x in c.get("/api/playground").json()["slots"] if x["id"] == p1)
    c.post(
        f"/api/playground/slots/{p1}/book",
        json={"command_id": str(uuid4()), "expected_revision": slot["revision"]},
    )
    book(c, mt.session("13:35"))
    r = c.get("/api/attendees/me/programme")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    items = r.json()["items"]
    assert [(i["kind"], i["status"]) for i in items] == [
        ("playground", "confirmed"),
        ("masterclass", "waitlisted"),
        ("mentor", "confirmed"),
        ("masterclass", "confirmed"),
    ]
    assert not any(i["conflict"] for i in items)
    assert r.json()["timezone"] == "Asia/Baku"
    # Only this person's own day: the other attendee's seat is not in it.
    assert len(mt.client(other).get("/api/attendees/me/programme").json()["items"]) == 1


def test_the_programme_flags_a_clash_the_booking_paths_would_have_refused(mt: Mentors) -> None:
    uid = mt.users(1)[0]
    c = mt.client(uid)
    s = mt.session("12:50")
    book(c, s)
    mc = mt.masterclass(mt.b1)
    # Written behind the application's back (e.g. a manual database fix).
    mt.rig.sql(
        "INSERT INTO masterclass_reservations (masterclass_id, block_id, user_id, status, "
        "confirmed_at) VALUES (:m, :b, :u, 'CONFIRMED', now())",
        m=mc,
        b=mt.b1,
        u=uid,
    )
    items = c.get("/api/attendees/me/programme").json()["items"]
    assert [i["conflict"] for i in items] == [True, True]
