from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.modules.playground import service
from app.modules.staff.models import StaffRole
from tests.api_rig import Rig
from tests.test_masterclasses import Program, choose, parallel


class PlaygroundProgram(Program):
    pg_slots: list[int]


@pytest.fixture
def pg(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Iterator[PlaygroundProgram]:
    monkeypatch.setattr(service, "now", lambda: datetime(2026, 11, 13, 4, 0, tzinfo=UTC))
    p = PlaygroundProgram(rig)
    rig.sign_in_as(rig.staff_id(StaffRole.ADMIN))
    booth = int(rig.scalar("SELECT id FROM booths ORDER BY id LIMIT 1"))
    r = rig.client.post(
        "/api/admin/playground/prepare", json={"event_id": p.event, "booth_id": booth}
    )
    assert r.status_code == 200, r.text
    p.pg_slots = [int(r[0]) for r in rig.sql("SELECT id FROM playground_slots ORDER BY number")]
    yield p
    for c in p.clients:
        c.close()


def book(c: TestClient, slot: int, **flags: bool) -> httpx.Response:
    mine = next(s for s in c.get("/api/playground").json()["slots"] if s["id"] == slot)
    return cast(
        httpx.Response,
        c.post(
            f"/api/playground/slots/{slot}/book",
            json={"command_id": str(uuid4()), "expected_revision": mine["revision"], **flags},
        ),
    )


def test_three_active_and_cancel_restores_limit(pg: PlaygroundProgram) -> None:
    c = pg.client(pg.users(1)[0])
    slots = pg.pg_slots
    rows = [book(c, s).json() for s in slots[:3]]
    r = book(c, slots[3])
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "active_booking_limit"
    assert (
        c.post(
            f"/api/playground/bookings/{rows[0]['id']}/cancel",
            json={"expected_version": rows[0]["version"]},
        ).status_code
        == 200
    )
    assert book(c, slots[3]).status_code == 200
    assert pg.rig.scalar("SELECT count(*) FROM points_log") == 0


def test_capacity_invitation_hold_expiry_and_accept(
    pg: PlaygroundProgram, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, c, d = [pg.client(u) for u in pg.users(4)]
    slot = pg.pg_slots[5]
    assert book(a, slot).status_code == 200
    hold = a.post(f"/api/playground/slots/{slot}/invite", json={"command_id": str(uuid4())}).json()
    assert book(c, slot).status_code == 200
    assert book(d, slot).status_code == 409
    assert (
        b.post("/api/playground/invitations/accept", json={"token": hold["token"]}).status_code
        == 200
    )
    assert (
        b.post("/api/playground/invitations/accept", json={"token": hold["token"]}).status_code
        == 200
    )
    assert pg.rig.scalar("SELECT count(*) FROM playground_bookings WHERE status='CONFIRMED'") == 3


def test_expired_hold_frees_seat_and_cannot_accept(
    pg: PlaygroundProgram, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, c, d = [pg.client(u) for u in pg.users(4)]
    slot = pg.pg_slots[5]
    book(a, slot)
    book(b, slot)
    h = a.post(f"/api/playground/slots/{slot}/invite", json={"command_id": str(uuid4())}).json()
    monkeypatch.setattr(service, "now", lambda: datetime(2026, 11, 13, 4, 31, tzinfo=UTC))
    assert book(c, slot).status_code == 200
    assert (
        d.post("/api/playground/invitations/accept", json={"token": h["token"]}).status_code == 409
    )


def test_invitee_limit_checked_before_accept_and_captain_cancel_independent(
    pg: PlaygroundProgram,
) -> None:
    a, b = [pg.client(u) for u in pg.users(2)]
    slots = pg.pg_slots
    own = book(a, slots[5]).json()
    h = a.post(f"/api/playground/slots/{slots[5]}/invite", json={"command_id": str(uuid4())}).json()
    for sid in slots[:3]:
        assert book(b, sid).status_code == 200
    assert (
        b.post("/api/playground/invitations/accept", json={"token": h["token"]}).status_code == 409
    )
    row = next(s["mine"] for s in b.get("/api/playground").json()["slots"] if s["id"] == slots[0])
    b.post(
        f"/api/playground/bookings/{row['id']}/cancel", json={"expected_version": row["version"]}
    )
    assert (
        b.post("/api/playground/invitations/accept", json={"token": h["token"]}).status_code == 200
    )
    a.post(
        f"/api/playground/bookings/{own['id']}/cancel", json={"expected_version": own["version"]}
    )
    assert (
        next(
            s["mine"]["status"]
            for s in b.get("/api/playground").json()["slots"]
            if s["id"] == slots[5]
        )
        == "confirmed"
    )


def test_cancel_deadline_and_late_slots(
    pg: PlaygroundProgram, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = pg.client(pg.users(1)[0])
    slot = pg.pg_slots[2]
    row = book(c, slot).json()
    start = pg.rig.scalar("SELECT starts_at FROM playground_slots WHERE id=:s", s=slot)
    monkeypatch.setattr(service, "now", lambda: start - timedelta(minutes=15))
    assert (
        c.post(
            f"/api/playground/bookings/{row['id']}/cancel",
            json={"expected_version": row["version"]},
        ).status_code
        == 409
    )
    assert book(c, pg.pg_slots[34]).status_code == 409
    assert book(c, pg.pg_slots[35], queue=True).status_code == 200


def test_masterclass_conflict_both_directions(pg: PlaygroundProgram) -> None:
    uid = pg.users(1)[0]
    c = pg.client(uid)
    mc = pg.masterclass(pg.b1)
    assert book(c, pg.pg_slots[10]).status_code == 200
    assert choose(c, pg.b1, mc).status_code == 409
    uid2 = pg.users(1)[0]
    d = pg.client(uid2)
    assert choose(d, pg.b1, mc).status_code == 200
    assert book(d, pg.pg_slots[11]).status_code == 409


def test_no_show_release_and_staff_scope(
    pg: PlaygroundProgram, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, d = [pg.client(u) for u in pg.users(2)]
    slot = pg.pg_slots[0]
    book(c, slot)
    q = book(d, slot, queue=True).json()
    start = pg.rig.scalar("SELECT starts_at FROM playground_slots WHERE id=:s", s=slot)
    monkeypatch.setattr(service, "now", lambda: start + timedelta(minutes=3, seconds=1))
    pg.rig.sign_in_as(pg.rig.staff_id(StaffRole.ADMIN))
    r = pg.rig.client.post(
        f"/api/staff/playground/bookings/{q['id']}/admit", json={"expected_version": q["version"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "checked_in"
    # 13 Nov 2026: a place is released only when the session needs it (2 of 3 held here)
    assert pg.rig.scalar("SELECT count(*) FROM playground_bookings WHERE status='NO_SHOW'") == 0


def test_parallel_last_seat_and_idempotency(pg: PlaygroundProgram) -> None:
    a, b, c, d = [pg.client(u) for u in pg.users(4)]
    sid = pg.pg_slots[0]
    book(a, sid)
    book(b, sid)
    payloads = [{"command_id": str(uuid4()), "expected_revision": "0"} for _ in range(2)]
    with ThreadPoolExecutor(2) as pool:
        rs = list(
            pool.map(
                lambda pair: pair[0].post(f"/api/playground/slots/{sid}/book", json=pair[1]),
                zip([c, d], payloads, strict=True),
            )
        )
    assert sorted(r.status_code for r in rs) == [200, 409]
    winner = 0 if rs[0].status_code == 200 else 1
    assert [c, d][winner].post(f"/api/playground/slots/{sid}/book", json=payloads[winner]).json()[
        "id"
    ] == rs[winner].json()["id"]


def test_private_invites_and_stale_recreate(pg: PlaygroundProgram) -> None:
    a, b = [pg.client(u) for u in pg.users(2)]
    sid = pg.pg_slots[0]
    r = book(a, sid).json()
    inv = a.post(f"/api/playground/slots/{sid}/invite", json={"command_id": str(uuid4())}).json()
    data = b.get("/api/playground").json()
    assert inv["token"] not in str(data)
    assert "email" not in str(data) and "phone" not in str(data)
    assert (
        b.post(
            "/api/playground/invitations/accept", json={"token": inv["token"][:-1] + "x"}
        ).status_code
        == 404
    )
    a.post(f"/api/playground/bookings/{r['id']}/cancel", json={"expected_version": r["version"]})
    assert (
        a.post(
            f"/api/playground/slots/{sid}/book",
            json={"command_id": str(uuid4()), "expected_revision": "0"},
        ).status_code
        == 409
    )


def test_other_booth_staff_cannot_read_or_admit(pg: PlaygroundProgram) -> None:
    c = pg.client(pg.users(1)[0])
    row = book(c, pg.pg_slots[0]).json()
    staff_ids = pg.rig.sql(
        "SELECT id FROM staff_accounts WHERE role='BOOTH_STAFF' AND booth_id <> "
        "(SELECT booth_id FROM playground_slots LIMIT 1) ORDER BY id"
    )
    assert staff_ids
    pg.rig.sign_in_as(int(staff_ids[0][0]))
    assert pg.rig.client.get(f"/api/staff/playground/slots/{pg.pg_slots[0]}").status_code == 403
    assert (
        pg.rig.client.post(
            f"/api/staff/playground/bookings/{row['id']}/admit",
            json={"expected_version": row["version"]},
        ).status_code
        == 403
    )


def test_invite_expires_at_fifteen_minute_boundary(
    pg: PlaygroundProgram, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = [pg.client(u) for u in pg.users(2)]
    sid = pg.pg_slots[2]
    start = pg.rig.scalar("SELECT starts_at FROM playground_slots WHERE id=:s", s=sid)
    monkeypatch.setattr(service, "now", lambda: start - timedelta(minutes=20))
    assert book(a, sid).status_code == 200
    inv = a.post(f"/api/playground/slots/{sid}/invite", json={"command_id": str(uuid4())}).json()
    assert datetime.fromisoformat(inv["expires_at"].replace("Z", "+00:00")) == start - timedelta(
        minutes=15
    )
    monkeypatch.setattr(service, "now", lambda: start - timedelta(minutes=15))
    assert (
        b.post("/api/playground/invitations/accept", json={"token": inv["token"]}).status_code
        == 409
    )


def _admit(pg: PlaygroundProgram, row: dict[str, Any]) -> httpx.Response:
    pg.rig.sign_in_as(pg.rig.staff_id(StaffRole.ADMIN))
    return cast(
        httpx.Response,
        pg.rig.client.post(
            f"/api/staff/playground/bookings/{row['id']}/admit",
            json={"expected_version": row["version"]},
        ),
    )


def _slot_start(pg: PlaygroundProgram, slot: int) -> datetime:
    return cast(
        datetime, pg.rig.scalar("SELECT starts_at FROM playground_slots WHERE id=:s", s=slot)
    )


def _queue_position(client: TestClient, slot: int) -> int | None:
    rows = client.get("/api/playground").json()["slots"]
    return cast(int | None, next(s["queue_position"] for s in rows if s["id"] == slot))


def test_a_reserved_attendee_within_three_minutes_is_checked_in(
    pg: PlaygroundProgram, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = pg.client(pg.users(1)[0])
    slot = pg.pg_slots[0]
    row = book(c, slot).json()
    start = _slot_start(pg, slot)
    monkeypatch.setattr(service, "now", lambda: start + timedelta(minutes=3))
    r = _admit(pg, row)
    assert r.status_code == 200 and r.json()["status"] == "checked_in"
    assert r.json()["admitted"] is True
    assert pg.rig.scalar("SELECT count(*) FROM points_log") == 0  # admission is not points


def test_the_booth_admits_whoever_is_there_in_any_order_with_no_three_person_cap(
    pg: PlaygroundProgram, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner, 13 Nov 2026 (event day): reserved people did not come, sessions got
    mixed up and more than three played. Staff confirm anyone queued at once, in any
    order, beyond three; nobody's reservation is released for it."""
    reserved_uids = pg.users(3)
    queue_uids = pg.users(3)
    slot = pg.pg_slots[0]
    reserved = [book(pg.client(u), slot).json() for u in reserved_uids]
    queued = [book(pg.client(u), slot, queue=True).json() for u in queue_uids]
    start = _slot_start(pg, slot)
    monkeypatch.setattr(service, "now", lambda: start + timedelta(minutes=1))
    last = _admit(pg, queued[-1])  # the last in the queue, inside the first 3 minutes
    assert last.status_code == 200, last.text
    assert last.json()["status"] == "checked_in"
    for row in [*queued[:-1], *reserved]:
        assert _admit(pg, row).json()["status"] == "checked_in"
    assert (
        pg.rig.scalar(
            "SELECT count(*) FROM playground_bookings WHERE slot_id = :s AND status = 'CHECKED_IN'",
            s=slot,
        )
        == 6
    )
    assert pg.rig.scalar("SELECT count(*) FROM playground_bookings WHERE status = 'NO_SHOW'") == 0


def test_an_admission_with_a_stale_version_changes_nothing(
    pg: PlaygroundProgram, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = pg.client(pg.users(1)[0])
    slot = pg.pg_slots[0]
    row = book(c, slot).json()
    start = _slot_start(pg, slot)
    monkeypatch.setattr(service, "now", lambda: start + timedelta(minutes=5))
    stale = _admit(pg, {**row, "version": row["version"] + 7})
    assert stale.json()["error"]["details"]["reason"] == "choice_changed"
    status = pg.rig.scalar("SELECT status FROM playground_bookings WHERE id=:b", b=row["id"])
    assert status == "CONFIRMED"  # a refused request releases nothing


def test_parallel_acceptance_of_one_invitation_gives_it_to_one_person(
    pg: PlaygroundProgram,
) -> None:
    a, b, c = [pg.client(u) for u in pg.users(3)]
    slot = pg.pg_slots[4]
    book(a, slot)
    token = a.post(
        f"/api/playground/slots/{slot}/invite", json={"command_id": str(uuid4())}
    ).json()["token"]
    with ThreadPoolExecutor(2) as pool:
        replies = list(
            pool.map(
                lambda client: client.post(
                    "/api/playground/invitations/accept", json={"token": token}
                ),
                [b, c],
            )
        )
    assert sorted(r.status_code for r in replies) == [200, 409]
    confirmed = pg.rig.scalar(
        "SELECT count(*) FROM playground_bookings WHERE slot_id=:s AND status='CONFIRMED'", s=slot
    )
    assert confirmed == 2


def test_an_invitation_retry_returns_the_same_hold(pg: PlaygroundProgram) -> None:
    a = pg.client(pg.users(1)[0])
    slot = pg.pg_slots[4]
    book(a, slot)
    command = str(uuid4())
    first = a.post(f"/api/playground/slots/{slot}/invite", json={"command_id": command}).json()
    again = a.post(f"/api/playground/slots/{slot}/invite", json={"command_id": command}).json()
    assert (first["id"], first["token"]) == (again["id"], again["token"])
    held = pg.rig.scalar(
        "SELECT count(*) FROM playground_bookings WHERE slot_id=:s AND status='INVITED'", s=slot
    )
    assert held == 1


def test_an_invitee_with_an_overlapping_masterclass_cannot_accept(pg: PlaygroundProgram) -> None:
    captain_uid, invitee_uid = pg.users(2)
    captain, invitee = pg.client(captain_uid), pg.client(invitee_uid)
    mc = pg.masterclass(pg.b1)
    assert choose(invitee, pg.b1, mc).status_code == 200
    slot = pg.pg_slots[11]  # 11:45 Baku, inside block 1 (11:30-13:00)
    assert book(captain, slot).status_code == 200
    token = captain.post(
        f"/api/playground/slots/{slot}/invite", json={"command_id": str(uuid4())}
    ).json()["token"]
    refused = invitee.post("/api/playground/invitations/accept", json={"token": token})
    assert refused.json()["error"]["details"]["reason"] == "masterclass_overlap"


def test_masterclass_and_playground_race_cannot_reserve_overlapping_times(
    pg: PlaygroundProgram,
) -> None:
    uid = pg.users(1)[0]
    masterclass_client, playground_client = pg.client(uid), pg.client(uid)
    mc = pg.masterclass(pg.b1)
    slot = pg.pg_slots[11]
    replies = parallel(
        [lambda: choose(masterclass_client, pg.b1, mc), lambda: book(playground_client, slot)]
    )
    assert sorted(r.status_code for r in replies) == [200, 409]
    assert (
        pg.rig.scalar(
            "SELECT (SELECT count(*) FROM masterclass_reservations WHERE user_id=:u "
            "AND status IN ('CONFIRMED','WAITLISTED')) + "
            "(SELECT count(*) FROM playground_bookings WHERE user_id=:u "
            "AND status IN ('CONFIRMED','WAITLISTED'))",
            u=uid,
        )
        == 1
    )


def test_the_database_holds_fifty_places_per_slot_whoever_writes(pg: PlaygroundProgram) -> None:
    # Migration 0028 (13 Nov 2026): a safety net of 50 places, no 3-person cap.
    slot = pg.pg_slots[6]
    users = pg.users(51)
    for i, uid in enumerate(users[:50]):
        pg.rig.sql(
            "INSERT INTO playground_bookings (slot_id, user_id, command_id, status) "
            "VALUES (:s, :u, :c, 'CONFIRMED')",
            s=slot,
            u=uid,
            c=f"direct-{i}",
        )
    with pytest.raises(IntegrityError) as caught:
        pg.rig.sql(
            "INSERT INTO playground_bookings (slot_id, user_id, command_id, status) "
            "VALUES (:s, :u, 'direct-4', 'CONFIRMED')",
            s=slot,
            u=users[50],
        )
    assert caught.value.orig.diag.constraint_name == "ck_playground_capacity"  # type: ignore[union-attr]
    # a queue entry is not a seat, so it is accepted
    pg.rig.sql(
        "INSERT INTO playground_bookings (slot_id, user_id, command_id, status) "
        "VALUES (:s, :u, 'direct-q', 'WAITLISTED')",
        s=slot,
        u=users[50],
    )


def _move_block(pg: PlaygroundProgram, start: str, end: str, deadline: str) -> httpx.Response:
    pg.rig.sign_in_as(pg.rig.staff_id(StaffRole.ADMIN))
    return cast(
        httpx.Response,
        pg.rig.client.patch(
            f"/api/admin/masterclass-blocks/{pg.b2}",
            json={
                "starts_at": f"2026-11-13T{start}:00+04:00",
                "ends_at": f"2026-11-13T{end}:00+04:00",
                "change_deadline": f"2026-11-13T{deadline}:00+04:00",
            },
        ),
    )


def test_a_masterclass_block_cannot_move_onto_its_attendees_playground_places(
    pg: PlaygroundProgram,
) -> None:
    uid = pg.users(1)[0]
    c = pg.client(uid)
    mc = pg.masterclass(pg.b2)
    assert choose(c, pg.b2, mc).status_code == 200
    assert book(c, pg.pg_slots[16]).status_code == 200  # P17: 13:00-13:15 Baku
    clear = _move_block(pg, "13:30", "14:30", "13:00")  # after P17, after block 1
    assert clear.status_code == 200, clear.text
    refused = _move_block(pg, "13:10", "14:30", "13:00")  # onto P17
    assert refused.json()["error"]["details"]["reason"] == "playground_overlap"
    assert (
        pg.rig.scalar(
            "SELECT to_char(starts_at AT TIME ZONE 'Asia/Baku', 'HH24:MI') "
            "FROM masterclass_blocks WHERE id=:b",
            b=pg.b2,
        )
        == "13:30"
    )


def test_a_demo_reset_clears_bookings_and_keeps_retained_booth_schedules(
    pg: PlaygroundProgram, reset_db_url: str
) -> None:
    from app.modules.admin.reset import run_demo_reset
    from tests.reset_support import SEED_ADMIN_PASSWORD, SEED_STAFF_PASSWORD

    c = pg.client(pg.users(1)[0])
    book(c, pg.pg_slots[3])
    extra = int(
        pg.rig.scalar(
            "INSERT INTO booths (slug, name) VALUES ('rehearsal-booth', 'Rehearsal') RETURNING id"
        )
    )
    pg.rig.sign_in_as(pg.rig.staff_id(StaffRole.ADMIN))
    prepared = pg.rig.client.post(
        "/api/admin/playground/prepare", json={"event_id": pg.event, "booth_id": extra}
    )
    assert prepared.status_code == 200
    kept = pg.rig.scalar("SELECT booth_id FROM playground_slots WHERE id=:s", s=pg.pg_slots[0])

    run_demo_reset(
        admin_password=SEED_ADMIN_PASSWORD,
        staff_password=SEED_STAFF_PASSWORD,
        database_url=reset_db_url,
    )

    assert pg.rig.scalar("SELECT count(*) FROM playground_bookings") == 0
    assert pg.rig.scalar("SELECT count(*) FROM playground_slots WHERE booth_id=:b", b=kept) == 36
    assert pg.rig.scalar("SELECT count(*) FROM playground_slots WHERE booth_id=:b", b=extra) == 0
    assert pg.rig.scalar("SELECT count(*) FROM booths WHERE id=:b", b=extra) == 0


def test_each_slot_carries_its_booths_one_sentence_card(pg: PlaygroundProgram) -> None:
    booth = int(pg.rig.scalar("SELECT id FROM booths ORDER BY id LIMIT 1"))
    pg.rig.sql(
        "UPDATE booths SET description = 'Spin the wheel and try your luck!' WHERE id = :b", b=booth
    )
    (uid,) = pg.users(1)
    slots = pg.client(uid).get("/api/playground").json()["slots"]
    assert slots and {s["booth_description"] for s in slots} == {
        "Spin the wheel and try your luck!"
    }
