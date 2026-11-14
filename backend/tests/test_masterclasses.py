"""Masterclass programme and reservations (migration 0017).

Runs on the disposable, freshly seeded rig database (tests/api_rig.py). The
server clock is replaced per test (`clock`), so the change deadlines can be
tested exactly at their boundary. Concurrency tests fire real parallel HTTP
requests from separate clients and then check the database itself.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.sessions import create_attendee_session
from app.main import app
from app.modules.masterclasses import service as mc_service
from app.modules.masterclasses.models import Masterclass
from app.modules.masterclasses.skeleton import create_program_skeleton
from app.modules.staff.models import StaffRole
from tests.api_rig import Rig

PROGRAM = "/api/masterclasses"
MINE = "/api/attendees/me/masterclasses"
ADMIN = "/api/admin"

# 10:00 in Baku on the event day: both blocks still open for changes.
BEFORE = datetime(2026, 11, 13, 6, 0, tzinfo=UTC)
B1_DEADLINE = datetime(2026, 11, 13, 7, 0, tzinfo=UTC)  # 11:00 Asia/Baku


class Clock:
    def __init__(self) -> None:
        self.now = BEFORE

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(mc_service, "server_now", c)
    return c


class Program:
    """The skeleton (event, 2 blocks, 5 rooms) plus masterclasses made here."""

    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        with Session(rig.engine) as db:
            create_program_skeleton(db)
            db.commit()
        self.event = int(rig.scalar("SELECT id FROM masterclass_events"))
        self.b1, self.b2 = (
            int(r[0]) for r in rig.sql("SELECT id FROM masterclass_blocks ORDER BY starts_at")
        )
        self.rooms = [int(r[0]) for r in rig.sql("SELECT id FROM masterclass_rooms ORDER BY id")]
        self._next_room = {self.b1: 0, self.b2: 0}
        self._users = 0
        self.clients: list[TestClient] = []

    def masterclass(
        self,
        block: int | None = None,
        *,
        capacity: int = 40,
        waitlist: int = 10,
        published: bool = True,
        title: str = "Masterclass",
    ) -> int:
        room = None
        if block is not None:
            room = self.rooms[self._next_room[block]]
            self._next_room[block] += 1
        return int(
            self.rig.scalar(
                "INSERT INTO masterclasses (event_id, block_id, room_id, title, capacity, "
                "waitlist_capacity, is_published) VALUES (:e, :b, :r, :t, :c, :w, :p) RETURNING id",
                e=self.event,
                b=block,
                r=room,
                t=title,
                c=capacity,
                w=waitlist,
                p=published,
            )
        )

    def users(self, n: int) -> list[int]:
        start = self._users
        self._users += n
        return [
            int(r[0])
            for r in self.rig.sql(
                "INSERT INTO users (first_name, last_name, email, phone, profession, "
                "sponsor_consent, status, activated_at) "
                "SELECT 'Guest', 'N' || g, 'mc-' || g || '@mail.test', '+10000000', 'Engineer', "
                "false, 'ACTIVE', now() "
                "FROM generate_series(CAST(:a AS integer), CAST(:b AS integer)) g RETURNING id",
                a=start + 1,
                b=start + n,
            )
        ]

    def fill(self, masterclass: int, block: int, users: list[int], status: str) -> None:
        for i, uid in enumerate(users):
            self.rig.sql(
                "INSERT INTO masterclass_reservations (masterclass_id, block_id, user_id, status, "
                "queued_at, confirmed_at) VALUES (:m, :b, :u, :s, :q, :c)",
                m=masterclass,
                b=block,
                u=uid,
                s=status,
                q=BEFORE - timedelta(minutes=60 - i) if status == "WAITLISTED" else None,
                c=BEFORE if status == "CONFIRMED" else None,
            )

    def client(self, user_id: int) -> TestClient:
        with Session(self.rig.engine) as db:
            token = create_attendee_session(db, user_id=user_id, now=datetime.now(UTC))
            db.commit()
        c = TestClient(app, headers={"Origin": get_settings().app_origin})
        c.cookies.set("session", token)
        self.clients.append(c)
        return c

    def active(self, user_id: int) -> list[tuple[int, int, str]]:
        return [
            (int(r[0]), int(r[1]), str(r[2]))
            for r in self.rig.sql(
                "SELECT block_id, masterclass_id, status FROM masterclass_reservations "
                "WHERE user_id = :u AND status <> 'CANCELLED' ORDER BY block_id",
                u=user_id,
            )
        ]

    def count(self, masterclass: int, status: str) -> int:
        return int(
            self.rig.scalar(
                "SELECT count(*) FROM masterclass_reservations "
                "WHERE masterclass_id = :m AND status = :s",
                m=masterclass,
                s=status,
            )
        )


@pytest.fixture
def program(rig: Rig, clock: Clock) -> Iterator[Program]:
    p = Program(rig)
    yield p
    for c in p.clients:
        c.close()


def choose(client: TestClient, block: int, masterclass: int, **flags: bool) -> Any:
    revision = client.get(MINE).json().get("revisions", {}).get(str(block), "0")
    return client.put(
        f"{MINE}/{block}",
        json={"masterclass_id": masterclass, "expected_revision": revision, **flags},
    )


def cancel(client: TestClient, block: int) -> Any:
    revision = client.get(MINE).json().get("revisions", {}).get(str(block), "0")
    return client.request("DELETE", f"{MINE}/{block}", json={"expected_revision": revision})


@contextmanager
def refused(constraint: str) -> Iterator[None]:
    """The database refused the write BY THIS CONSTRAINT NAME (what the app's
    error map dispatches on), not merely with some error."""
    with pytest.raises(IntegrityError) as caught:
        yield
    assert caught.value.orig.diag.constraint_name == constraint  # type: ignore[union-attr]


def code(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def parallel(calls: list[Callable[[], Any]]) -> list[Any]:
    """Fire every call at once (a barrier releases them together)."""
    barrier = threading.Barrier(len(calls))
    results: list[Any] = [None] * len(calls)

    def run(i: int, call: Callable[[], Any]) -> None:
        barrier.wait()
        results[i] = call()

    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


# --- the programme ------------------------------------------------------------------------


def test_seat_counts_follow_cancellation_and_promotion_without_stale_cache(
    program: Program,
) -> None:
    mc = program.masterclass(program.b1, capacity=1, waitlist=1)
    other = program.masterclass(program.b1)
    first, second = program.users(2)
    with Session(program.rig.engine) as db:
        assert mc_service._seat_counts(db, mc) == (0, 0)
    program.fill(mc, program.b1, [first], "CONFIRMED")
    program.fill(mc, program.b1, [second], "WAITLISTED")
    with Session(program.rig.engine) as db:
        assert mc_service._seat_counts(db, mc) == (1, 1)
        assert mc_service._seat_counts(db, other) == (0, 0)
    assert cancel(program.client(first), program.b1).status_code == 200
    with Session(program.rig.engine) as db:
        assert mc_service._seat_counts(db, mc) == (1, 0)
    assert cancel(program.client(second), program.b1).status_code == 200
    with Session(program.rig.engine) as db:
        assert mc_service._seat_counts(db, mc) == (0, 0)


def test_the_programme_is_public_and_carries_no_attendee_data(program: Program) -> None:
    mc = program.masterclass(program.b1, title="Data for AI", capacity=3)
    hidden = program.masterclass(program.b1, title="Not yet", published=False)
    later = program.masterclass(None, title="LOGO masterclass")
    (u,) = program.users(1)
    assert choose(program.client(u), program.b1, mc).status_code == 200

    anonymous = TestClient(app)
    response = anonymous.get(PROGRAM)
    assert response.status_code == 200
    body = response.json()
    event = body["events"][0]
    assert (event["timezone"], event["venue"], event["event_date"]) == (
        "Asia/Baku",
        "The Ritz-Carlton Baku",
        "2026-11-13",
    )
    b1 = event["blocks"][0]
    assert (
        b1["starts_at"] == "2026-11-13T07:30:00Z"
        and b1["change_deadline"] == "2026-11-13T07:00:00Z"
    )
    assert b1["changes_open"] is True
    (shown,) = b1["masterclasses"]
    assert shown["id"] == mc and shown["id"] != hidden
    assert (shown["confirmed_count"], shown["seats_left"], shown["availability"]) == (
        1,
        2,
        "available",
    )
    assert shown["room"] == "Zal 1"
    # The masterclass without a slot is shown as "to be scheduled", never invented a slot.
    assert [m["id"] for m in event["unscheduled"]] == [later]
    for leaked in ("mc-1@mail.test", "Guest", "user_id", "email", "phone"):
        assert leaked not in response.text


def test_an_unscheduled_or_unpublished_masterclass_cannot_be_booked(program: Program) -> None:
    later = program.masterclass(None)
    hidden = program.masterclass(program.b1, published=False)
    (u,) = program.users(1)
    c = program.client(u)
    assert code(choose(c, program.b1, later)) == (404, "MASTERCLASS_NOT_FOUND")
    assert code(choose(c, program.b1, hidden)) == (404, "MASTERCLASS_NOT_FOUND")
    other_block = program.masterclass(program.b2)
    assert code(choose(c, program.b1, other_block)) == (404, "MASTERCLASS_NOT_FOUND")
    assert program.active(u) == []


# --- one choice per block -------------------------------------------------------------------


def test_one_active_choice_per_block_and_one_in_each_block(program: Program) -> None:
    a, b = program.masterclass(program.b1), program.masterclass(program.b1)
    c2 = program.masterclass(program.b2)
    (u,) = program.users(1)
    client = program.client(u)
    first = choose(client, program.b1, a)
    assert first.status_code == 200 and first.json()["outcome"] == "confirmed"
    assert choose(client, program.b2, c2).json()["outcome"] == "confirmed"
    changed = choose(client, program.b1, b)
    assert changed.json()["outcome"] == "confirmed"
    assert program.active(u) == [(program.b1, b, "CONFIRMED"), (program.b2, c2, "CONFIRMED")]
    assert program.count(a, "CONFIRMED") == 0
    reason = program.rig.scalar(
        "SELECT cancel_reason FROM masterclass_reservations WHERE masterclass_id = :m", m=a
    )
    assert reason == "REPLACED"
    mine = client.get(MINE).json()["choices"]
    assert [(c["block_id"], c["masterclass_id"], c["status"]) for c in mine] == [
        (program.b1, b, "confirmed"),
        (program.b2, c2, "confirmed"),
    ]


def test_sending_the_same_choice_again_creates_nothing(program: Program) -> None:
    mc = program.masterclass(program.b1)
    (u,) = program.users(1)
    client = program.client(u)
    replies = [choose(client, program.b1, mc) for _ in range(3)]
    assert [r.json()["outcome"] for r in replies] == ["confirmed", "unchanged", "unchanged"]
    # and three at once (a double tap and a network retry)
    again = parallel([lambda: choose(client, program.b1, mc) for _ in range(3)])
    assert all(r.status_code == 200 for r in again)
    total = program.rig.scalar(
        "SELECT count(*) FROM masterclass_reservations WHERE user_id = :u", u=u
    )
    assert total == 1


def test_the_database_holds_one_active_choice_per_block(program: Program) -> None:
    a, b = program.masterclass(program.b1), program.masterclass(program.b1)
    (u,) = program.users(1)
    program.fill(a, program.b1, [u], "CONFIRMED")
    with refused("uq_masterclass_reservations_one_active_per_block"):
        program.fill(b, program.b1, [u], "CONFIRMED")
    with refused("fk_masterclass_reservations_masterclass_block"):
        program.fill(a, program.b2, [program.users(1)[0]], "CONFIRMED")


# --- capacity and the waitlist ----------------------------------------------------------------


def test_independent_choices_reach_their_resource_locks_together(
    program: Program, monkeypatch: pytest.MonkeyPatch
) -> None:
    classes = [program.masterclass(program.b1), program.masterclass(program.b1)]
    clients = [program.client(uid) for uid in program.users(2)]
    rendezvous = threading.Barrier(2, timeout=10)
    original = mc_service._lock_masterclasses

    def lock_resource(db: Session, ids: set[int]) -> dict[int, Masterclass]:
        # A global exclusive schedule lock would prevent the second request
        # reaching this point until the first commits: the barrier would fail.
        rendezvous.wait()
        return original(db, ids)

    monkeypatch.setattr(mc_service, "_lock_masterclasses", lock_resource)
    replies = parallel(
        [
            partial(choose, client, program.b1, mc)
            for client, mc in zip(clients, classes, strict=True)
        ]
    )
    assert [r.status_code for r in replies] == [200, 200]
    assert all(program.count(mc, "CONFIRMED") == 1 for mc in classes)


def test_parallel_requests_for_the_40th_seat_give_it_to_exactly_one(program: Program) -> None:
    mc = program.masterclass(program.b1, capacity=40, waitlist=10)
    program.fill(mc, program.b1, program.users(39), "CONFIRMED")
    racers = program.users(6)
    clients = [program.client(u) for u in racers]
    replies = parallel([partial(choose, c, program.b1, mc) for c in clients])
    outcomes = sorted(
        r.json()["outcome"] if r.status_code == 200 else r.json()["error"]["code"] for r in replies
    )
    assert outcomes == ["MASTERCLASS_FULL"] * 5 + ["confirmed"]
    assert program.count(mc, "CONFIRMED") == 40
    assert program.count(mc, "WAITLISTED") == 0  # nobody was queued without asking
    full = next(r for r in replies if r.status_code == 409)
    assert full.json()["error"]["details"] == {"waitlist_open": True}


def test_parallel_requests_for_the_10th_waitlist_place_give_it_to_exactly_one(
    program: Program,
) -> None:
    mc = program.masterclass(program.b1, capacity=2, waitlist=10)
    program.fill(mc, program.b1, program.users(2), "CONFIRMED")
    program.fill(mc, program.b1, program.users(9), "WAITLISTED")
    clients = [program.client(u) for u in program.users(5)]
    replies = parallel([partial(choose, c, program.b1, mc, join_waitlist=True) for c in clients])
    outcomes = sorted(
        r.json()["outcome"] if r.status_code == 200 else r.json()["error"]["code"] for r in replies
    )
    assert outcomes == ["MASTERCLASS_WAITLIST_FULL"] * 4 + ["waitlisted"]
    assert program.count(mc, "WAITLISTED") == 10
    winner = next(r for r in replies if r.status_code == 200).json()["choices"]["choices"][0]
    assert winner["waitlist_position"] == 10


def test_a_full_masterclass_needs_an_explicit_waitlist_choice(program: Program) -> None:
    mc = program.masterclass(program.b1, capacity=1, waitlist=2)
    program.fill(mc, program.b1, program.users(1), "CONFIRMED")
    (u,) = program.users(1)
    client = program.client(u)
    refused = choose(client, program.b1, mc)
    assert code(refused) == (409, "MASTERCLASS_FULL")
    assert program.active(u) == []
    queued = choose(client, program.b1, mc, join_waitlist=True)
    assert queued.json()["outcome"] == "waitlisted"
    assert queued.json()["choices"]["choices"][0]["waitlist_position"] == 1
    assert queued.json()["choices"]["choices"][0]["status"] == "waitlisted"


def test_no_place_at_all_is_refused_and_writes_nothing(program: Program) -> None:
    mc = program.masterclass(program.b1, capacity=1, waitlist=1)
    program.fill(mc, program.b1, program.users(1), "CONFIRMED")
    program.fill(mc, program.b1, program.users(1), "WAITLISTED")
    (u,) = program.users(1)
    client = program.client(u)
    assert code(choose(client, program.b1, mc, join_waitlist=True)) == (
        409,
        "MASTERCLASS_WAITLIST_FULL",
    )
    assert code(choose(client, program.b1, mc)) == (409, "MASTERCLASS_WAITLIST_FULL")
    assert program.active(u) == []
    shown = TestClient(app).get(PROGRAM).json()["events"][0]["blocks"][0]["masterclasses"][0]
    assert shown["availability"] == "full"


def test_the_queue_moves_up_in_order_and_a_newcomer_cannot_overtake(program: Program) -> None:
    mc = program.masterclass(program.b1, capacity=1, waitlist=5)
    holder, first, second, newcomer = program.users(4)
    c_holder, c_first, c_second, c_new = (
        program.client(u) for u in (holder, first, second, newcomer)
    )
    assert choose(c_holder, program.b1, mc).json()["outcome"] == "confirmed"
    assert choose(c_first, program.b1, mc, join_waitlist=True).json()["outcome"] == "waitlisted"
    assert choose(c_second, program.b1, mc, join_waitlist=True).json()["outcome"] == "waitlisted"

    cancelled = cancel(c_holder, program.b1)
    assert cancelled.json()["outcome"] == "cancelled"
    # the seat went to the head of the queue in the same transaction
    assert program.active(first) == [(program.b1, mc, "CONFIRMED")]
    assert c_second.get(MINE).json()["choices"][0]["waitlist_position"] == 1
    # a newcomer sees no seat and queues behind
    assert code(choose(c_new, program.b1, mc)) == (409, "MASTERCLASS_FULL")
    assert (
        choose(c_new, program.b1, mc, join_waitlist=True).json()["choices"]["choices"][0][
            "waitlist_position"
        ]
        == 2
    )


def test_a_free_seat_with_people_waiting_is_not_given_to_a_newcomer(program: Program) -> None:
    """Even if a seat is free (an administrator just raised the capacity in the
    database by hand), someone is waiting for it: the newcomer queues."""
    mc = program.masterclass(program.b1, capacity=1, waitlist=5)
    program.fill(mc, program.b1, program.users(1), "CONFIRMED")
    program.fill(mc, program.b1, program.users(1), "WAITLISTED")
    program.rig.sql("UPDATE masterclasses SET capacity = 2 WHERE id = :m", m=mc)
    (newcomer,) = program.users(1)
    client = program.client(newcomer)
    assert code(choose(client, program.b1, mc)) == (409, "MASTERCLASS_FULL")
    reply = choose(client, program.b1, mc, join_waitlist=True)
    assert reply.json()["choices"]["choices"][0]["waitlist_position"] == 2


def test_leaving_the_waitlist_takes_you_out_of_the_count(program: Program) -> None:
    mc = program.masterclass(program.b1, capacity=1, waitlist=3)
    program.fill(mc, program.b1, program.users(1), "CONFIRMED")
    a, b = program.users(2)
    ca, cb = program.client(a), program.client(b)
    choose(ca, program.b1, mc, join_waitlist=True)
    choose(cb, program.b1, mc, join_waitlist=True)
    assert cancel(ca, program.b1).json()["outcome"] == "cancelled"
    assert cb.get(MINE).json()["choices"][0]["waitlist_position"] == 1
    assert program.count(mc, "WAITLISTED") == 1


def test_cancel_frees_the_block_and_a_new_choice_can_be_made(program: Program) -> None:
    mc, other = program.masterclass(program.b1, capacity=1), program.masterclass(program.b1)
    (u,) = program.users(1)
    client = program.client(u)
    choose(client, program.b1, mc)
    assert cancel(client, program.b1).json()["outcome"] == "cancelled"
    assert cancel(client, program.b1).json()["outcome"] == "unchanged"
    assert program.count(mc, "CONFIRMED") == 0  # a cancelled row holds no seat
    assert choose(client, program.b1, other).json()["outcome"] == "confirmed"
    assert choose(client, program.b1, mc).json()["outcome"] == "confirmed"
    assert program.active(u) == [(program.b1, mc, "CONFIRMED")]


def test_the_database_refuses_seats_beyond_the_capacity_whoever_writes(program: Program) -> None:
    mc = program.masterclass(program.b1, capacity=2, waitlist=1)
    program.fill(mc, program.b1, program.users(2), "CONFIRMED")
    with refused("ck_masterclass_reservations_capacity"):
        program.fill(mc, program.b1, program.users(1), "CONFIRMED")
    program.fill(mc, program.b1, program.users(1), "WAITLISTED")
    with refused("ck_masterclass_reservations_waitlist_capacity"):
        program.fill(mc, program.b1, program.users(1), "WAITLISTED")
    with refused("ck_masterclasses_capacity_not_below_confirmed"):
        program.rig.sql("UPDATE masterclasses SET capacity = 1 WHERE id = :m", m=mc)
    with refused("ck_masterclasses_waitlist_not_below_waiting"):
        program.rig.sql("UPDATE masterclasses SET waitlist_capacity = 0 WHERE id = :m", m=mc)


# --- changing a choice --------------------------------------------------------------------------


def test_a_change_that_cannot_be_had_keeps_the_old_seat(program: Program) -> None:
    mine, wanted = (
        program.masterclass(program.b1),
        program.masterclass(program.b1, capacity=1, waitlist=0),
    )
    program.fill(wanted, program.b1, program.users(1), "CONFIRMED")
    (u,) = program.users(1)
    client = program.client(u)
    choose(client, program.b1, mine)
    assert code(choose(client, program.b1, wanted, join_waitlist=True)) == (
        409,
        "MASTERCLASS_WAITLIST_FULL",
    )
    assert program.active(u) == [(program.b1, mine, "CONFIRMED")]


def test_giving_up_a_confirmed_seat_for_a_waitlist_needs_its_own_consent(program: Program) -> None:
    mine = program.masterclass(program.b1, capacity=1, waitlist=3)
    wanted = program.masterclass(program.b1, capacity=1, waitlist=3)
    program.fill(wanted, program.b1, program.users(1), "CONFIRMED")
    (u,) = program.users(1)
    (waiting_for_mine,) = program.users(1)
    client = program.client(u)
    choose(client, program.b1, mine)
    choose(program.client(waiting_for_mine), program.b1, mine, join_waitlist=True)

    refused = choose(client, program.b1, wanted, join_waitlist=True)
    assert code(refused) == (409, "MASTERCLASS_SEAT_WOULD_BE_LOST")
    assert program.active(u) == [(program.b1, mine, "CONFIRMED")]

    moved = choose(client, program.b1, wanted, join_waitlist=True, give_up_confirmed_seat=True)
    assert moved.json()["outcome"] == "waitlisted"
    assert program.active(u) == [(program.b1, wanted, "WAITLISTED")]
    # the released seat went to the person waiting for it, in the same transaction
    assert program.active(waiting_for_mine) == [(program.b1, mine, "CONFIRMED")]


def test_two_devices_changing_at_once_end_in_one_defined_choice(program: Program) -> None:
    a = program.masterclass(program.b1, capacity=5)
    b = program.masterclass(program.b1, capacity=5)
    (u,) = program.users(1)
    phone, laptop = program.client(u), program.client(u)
    for _ in range(5):
        replies = parallel(
            [lambda: choose(phone, program.b1, a), lambda: choose(laptop, program.b1, b)]
        )
        assert all(r.status_code in {200, 409} for r in replies)
        assert any(r.status_code == 200 for r in replies)
        active = program.active(u)
        assert len(active) == 1 and active[0][1] in {a, b}
        assert program.count(a, "CONFIRMED") + program.count(b, "CONFIRMED") == 1
        # the later of the two (by commit order) is the one that stands, and its
        # own reply says so
        last = [
            r.json()["choices"]["choices"][0]["masterclass_id"]
            for r in replies
            if r.status_code == 200
        ]
        assert active[0][1] in last


# --- the change deadline --------------------------------------------------------------------------


def test_the_deadline_boundary_is_exact_and_server_side(program: Program, clock: Clock) -> None:
    a, b = program.masterclass(program.b1), program.masterclass(program.b1)
    c2 = program.masterclass(program.b2)
    (u,) = program.users(1)
    client = program.client(u)

    clock.now = B1_DEADLINE - timedelta(microseconds=1)
    assert choose(client, program.b1, a).json()["outcome"] == "confirmed"

    clock.now = B1_DEADLINE  # at the deadline: closed
    assert code(choose(client, program.b1, b)) == (409, "MASTERCLASS_CHANGES_CLOSED")
    assert code(cancel(client, program.b1)) == (409, "MASTERCLASS_CHANGES_CLOSED")
    clock.now = B1_DEADLINE + timedelta(minutes=5)
    assert code(choose(client, program.b1, b)) == (409, "MASTERCLASS_CHANGES_CLOSED")
    assert program.active(u) == [(program.b1, a, "CONFIRMED")]

    # block 2 closes at 15:30 Baku, so it is still open
    assert choose(client, program.b2, c2).json()["outcome"] == "confirmed"
    mine = {c["block_id"]: c["changes_open"] for c in client.get(MINE).json()["choices"]}
    assert mine == {program.b1: False, program.b2: True}
    blocks = TestClient(app).get(PROGRAM).json()["events"][0]["blocks"]
    assert [bl["changes_open"] for bl in blocks] == [False, True]


# --- whose reservation ----------------------------------------------------------------------------


def test_nobody_can_see_or_change_another_attendees_reservation(program: Program) -> None:
    mc = program.masterclass(program.b1)
    owner, other = program.users(2)
    c_owner, c_other = program.client(owner), program.client(other)
    choose(c_owner, program.b1, mc)
    assert c_other.get(MINE).json()["choices"] == []
    assert cancel(c_other, program.b1).json()["outcome"] == "unchanged"
    assert program.active(owner) == [(program.b1, mc, "CONFIRMED")]
    # the request has no way to name a person
    smuggled = c_other.put(f"{MINE}/{program.b1}", json={"masterclass_id": mc, "user_id": owner})
    assert code(smuggled) == (422, "VALIDATION_ERROR")
    anonymous = TestClient(app, headers={"Origin": get_settings().app_origin})
    assert code(anonymous.get(MINE)) == (401, "UNAUTHENTICATED")
    assert code(choose(anonymous, program.b1, mc)) == (401, "UNAUTHENTICATED")


def test_a_provisional_attendee_cannot_book(program: Program) -> None:
    mc = program.masterclass(program.b1)
    (u,) = program.users(1)
    client = program.client(u)
    program.rig.sql(
        "UPDATE users SET status = 'PROVISIONAL', activated_at = NULL WHERE id = :u", u=u
    )
    assert choose(client, program.b1, mc).status_code in {401, 403}
    assert program.active(u) == []


def test_a_reservation_is_not_attendance_and_gives_no_points(program: Program) -> None:
    mc = program.masterclass(program.b1)
    (u,) = program.users(1)
    before = program.rig.scalar("SELECT count(*) FROM points_log")
    reply = choose(program.client(u), program.b1, mc)
    assert program.rig.scalar("SELECT count(*) FROM points_log") == before
    for word in ("attend", "complete", "points", "checked"):
        assert word not in reply.text.lower()


# --- administration ---------------------------------------------------------------------------


@pytest.fixture
def admin_client(program: Program) -> TestClient:
    program.rig.sign_in(StaffRole.ADMIN)
    return program.rig.client


def test_admin_builds_the_programme_and_every_change_is_audited(
    program: Program, admin_client: TestClient
) -> None:
    created = admin_client.post(
        f"{ADMIN}/masterclasses",
        json={
            "event_id": program.event,
            "block_id": program.b1,
            "room_id": program.rooms[0],
            "title": "AI solutions: idea to product",
            "speaker": "To be confirmed",
            "is_published": True,
        },
    )
    assert created.status_code == 201, created.text
    mc = created.json()["id"]
    assert created.json()["capacity"] == 40 and created.json()["waitlist_capacity"] == 10
    edited = admin_client.patch(f"{ADMIN}/masterclasses/{mc}", json={"description": "Hands-on."})
    assert edited.status_code == 200
    room = admin_client.post(
        f"{ADMIN}/masterclass-rooms", json={"event_id": program.event, "name": "LOGO hall"}
    )
    assert room.status_code == 201
    block = admin_client.patch(
        f"{ADMIN}/masterclass-blocks/{program.b2}", json={"title": "Block 2"}
    )
    assert block.status_code == 200
    actions = {r["action"] for r in program.rig.audit_rows()}
    assert {
        "masterclass.create",
        "masterclass.update",
        "masterclass_room.create",
        "masterclass_block.update",
    } <= actions
    listing = admin_client.get(f"{ADMIN}/masterclass-program").json()
    assert {m["id"] for m in listing["masterclasses"]} >= {mc}
    assert len(listing["blocks"]) == 2 and len(listing["rooms"]) == 6


def test_admin_cannot_squeeze_capacity_below_what_attendees_hold(
    program: Program, admin_client: TestClient
) -> None:
    mc = program.masterclass(program.b1, capacity=3, waitlist=2)
    program.fill(mc, program.b1, program.users(3), "CONFIRMED")
    program.fill(mc, program.b1, program.users(2), "WAITLISTED")
    low = admin_client.patch(f"{ADMIN}/masterclasses/{mc}", json={"capacity": 2})
    assert code(low) == (409, "MASTERCLASS_ADMIN_CONFLICT")
    assert low.json()["error"]["details"] == {"reason": "capacity_below_confirmed", "confirmed": 3}
    short = admin_client.patch(f"{ADMIN}/masterclasses/{mc}", json={"waitlist_capacity": 1})
    assert short.json()["error"]["details"]["reason"] == "waitlist_below_waiting"
    hidden = admin_client.patch(f"{ADMIN}/masterclasses/{mc}", json={"is_published": False})
    assert hidden.json()["error"]["details"]["reason"] == "active_reservations"
    gone = admin_client.delete(f"{ADMIN}/masterclasses/{mc}")
    assert gone.json()["error"]["details"]["reason"] == "has_reservations"
    moved = admin_client.patch(f"{ADMIN}/masterclasses/{mc}", json={"block_id": program.b2})
    assert moved.json()["error"]["details"]["reason"] == "has_reservations"
    assert program.rig.scalar("SELECT capacity FROM masterclasses WHERE id = :m", m=mc) == 3
    assert program.count(mc, "CONFIRMED") == 3 and program.count(mc, "WAITLISTED") == 2
    assert program.rig.audit_rows("masterclass.update") == []  # nothing changed, nothing audited


def test_a_larger_capacity_hands_the_new_seats_to_the_queue_in_order(
    program: Program, admin_client: TestClient
) -> None:
    mc = program.masterclass(program.b1, capacity=1, waitlist=5)
    program.fill(mc, program.b1, program.users(1), "CONFIRMED")
    queue = program.users(3)
    program.fill(mc, program.b1, queue, "WAITLISTED")
    raised = admin_client.patch(f"{ADMIN}/masterclasses/{mc}", json={"capacity": 3})
    assert raised.status_code == 200 and raised.json()["promoted_from_waitlist"] == 2
    assert [program.active(u)[0][2] for u in queue] == ["CONFIRMED", "CONFIRMED", "WAITLISTED"]
    lists = admin_client.get(f"{ADMIN}/masterclasses/{mc}/reservations").json()
    assert (lists["confirmed_count"], lists["waitlist_count"]) == (3, 1)
    assert lists["waitlist"][0]["user_id"] == queue[2] and lists["waitlist"][0]["position"] == 1
    assert "phone" not in str(lists)


def test_admin_slot_rules(program: Program, admin_client: TestClient) -> None:
    program.masterclass(program.b1)  # takes room 1 in block 1
    taken = admin_client.post(
        f"{ADMIN}/masterclasses",
        json={
            "event_id": program.event,
            "block_id": program.b1,
            "room_id": program.rooms[0],
            "title": "X",
        },
    )
    assert taken.json()["error"]["details"]["reason"] == "room_taken"
    overlap = admin_client.patch(
        f"{ADMIN}/masterclass-blocks/{program.b2}",
        json={
            "starts_at": "2026-11-13T12:00:00+04:00",
            "change_deadline": "2026-11-13T11:45:00+04:00",
        },
    )
    assert overlap.json()["error"]["details"]["reason"] == "block_overlap"
    naive = admin_client.patch(
        f"{ADMIN}/masterclass-blocks/{program.b2}", json={"starts_at": "2026-11-13T16:00:00"}
    )
    assert code(naive) == (422, "VALIDATION_ERROR")
    late = admin_client.patch(
        f"{ADMIN}/masterclass-blocks/{program.b2}",
        json={"change_deadline": "2026-11-13T16:30:00+04:00"},
    )
    assert code(late) == (400, "ADMIN_INVALID")
    unscheduled = admin_client.post(
        f"{ADMIN}/masterclasses", json={"event_id": program.event, "title": "LOGO masterclass"}
    )
    assert unscheduled.status_code == 201 and unscheduled.json()["block_id"] is None
    empty = program.masterclass(program.b2)
    assert admin_client.delete(f"{ADMIN}/masterclasses/{empty}").status_code == 200


def test_only_administrators_manage_the_programme(program: Program) -> None:
    rig = program.rig
    rig.sign_in(StaffRole.BOOTH_STAFF)
    assert code(rig.client.get(f"{ADMIN}/masterclass-program")) == (403, "STAFF_NOT_AUTHORIZED")
    assert (
        rig.client.post(
            f"{ADMIN}/masterclasses", json={"event_id": program.event, "title": "X"}
        ).status_code
        == 403
    )
    (u,) = program.users(1)
    attendee = program.client(u)
    assert attendee.get(f"{ADMIN}/masterclass-program").status_code in {401, 403}
    assert rig.scalar("SELECT count(*) FROM masterclasses") == 0


def test_the_skeleton_adds_only_the_confirmed_frame_and_can_be_rerun(program: Program) -> None:
    """Event, 2 blocks and 5 rooms in Asia/Baku time - never a masterclass."""
    with Session(program.rig.engine) as db:
        again = create_program_skeleton(db)
        db.commit()
    assert (again.event_created, again.blocks_created, again.rooms_created) == (False, 0, 0)
    blocks = program.rig.sql(
        "SELECT code, starts_at, ends_at, change_deadline "
        "FROM masterclass_blocks ORDER BY starts_at"
    )
    baku = [
        ("b1", "11:30", "13:00", "11:00"),
        ("b2", "16:00", "17:30", "15:30"),
    ]
    shown = [
        (
            code,
            *(t.astimezone(ZoneInfo("Asia/Baku")).strftime("%H:%M") for t in (start, end, dl)),
        )
        for code, start, end, dl in blocks
    ]
    assert shown == baku
    assert program.rig.scalar("SELECT count(*) FROM masterclass_rooms") == 5
    assert program.rig.scalar("SELECT count(*) FROM masterclasses") == 0


def test_an_administrator_creates_the_confirmed_frame_from_the_admin_panel(rig: Rig) -> None:
    """The same frame as the CLI, from the admin panel: audited, re-runnable,
    administrators only, and never a second event beside one entered by hand."""
    assert rig.scalar("SELECT count(*) FROM masterclass_events") == 0
    _, booth_staff = rig.booth_with_staff()
    rig.sign_in_as(booth_staff)
    assert rig.client.post(f"{ADMIN}/masterclass-skeleton").status_code in {401, 403}
    rig.sign_in(StaffRole.ADMIN)
    first = rig.client.post(f"{ADMIN}/masterclass-skeleton")
    assert first.status_code == 200, first.text
    assert first.json() == {"event_created": True, "blocks_created": 2, "rooms_created": 5}
    again = rig.client.post(f"{ADMIN}/masterclass-skeleton").json()
    assert again == {"event_created": False, "blocks_created": 0, "rooms_created": 0}
    assert rig.scalar("SELECT count(*) FROM masterclass_events") == 1
    assert rig.scalar("SELECT count(*) FROM masterclasses") == 0  # never a masterclass
    assert rig.scalar("SELECT competition_closes_at FROM masterclass_events") is None
    assert len(rig.audit_rows("masterclass.skeleton")) == 2


def test_the_frame_is_refused_beside_an_event_entered_by_hand(rig: Rig) -> None:
    rig.sql(
        "INSERT INTO masterclass_events (code, title, timezone, event_date) "
        "VALUES ('by-hand', 'Entered by hand', 'Asia/Baku', '2026-11-13')"
    )
    rig.sign_in(StaffRole.ADMIN)
    assert rig.client.post(f"{ADMIN}/masterclass-skeleton").status_code == 409
    assert rig.scalar("SELECT count(*) FROM masterclass_events") == 1


# Preconditions are captured by a device, not re-read when a delayed request arrives.
def test_old_choice_retry_cannot_overwrite_newer_choice(program: Program) -> None:
    a, b = program.masterclass(program.b1), program.masterclass(program.b1)
    (uid,) = program.users(1)
    client = program.client(uid)
    request = {"masterclass_id": a, "expected_revision": "0"}
    assert client.put(f"{MINE}/{program.b1}", json=request).status_code == 200
    assert choose(client, program.b1, b).status_code == 200
    stale = client.put(f"{MINE}/{program.b1}", json=request)
    assert code(stale) == (409, "MASTERCLASS_CHOICE_CONFLICT")
    assert program.active(uid) == [(program.b1, b, "CONFIRMED")]


def test_old_create_retry_cannot_resurrect_cancelled_choice(program: Program) -> None:
    a = program.masterclass(program.b1)
    (uid,) = program.users(1)
    client = program.client(uid)
    request = {"masterclass_id": a, "expected_revision": "0"}
    assert client.put(f"{MINE}/{program.b1}", json=request).status_code == 200
    assert cancel(client, program.b1).status_code == 200
    assert code(client.put(f"{MINE}/{program.b1}", json=request)) == (
        409,
        "MASTERCLASS_CHOICE_CONFLICT",
    )
    assert program.active(uid) == []
    assert choose(client, program.b1, a).status_code == 200


def test_stale_cancel_does_not_cancel_a_new_choice(program: Program) -> None:
    a, b = program.masterclass(program.b1), program.masterclass(program.b1)
    (uid,) = program.users(1)
    client = program.client(uid)
    choose(client, program.b1, a)
    revision = client.get(MINE).json()["revisions"][str(program.b1)]
    choose(client, program.b1, b)
    stale = client.request("DELETE", f"{MINE}/{program.b1}", json={"expected_revision": revision})
    assert code(stale) == (409, "MASTERCLASS_CHOICE_CONFLICT")
    assert program.active(uid) == [(program.b1, b, "CONFIRMED")]


def test_devices_using_same_revision_cannot_both_change_the_choice(program: Program) -> None:
    a, b = program.masterclass(program.b1), program.masterclass(program.b1)
    (uid,) = program.users(1)
    phone, laptop = program.client(uid), program.client(uid)
    replies = parallel(
        [
            lambda: phone.put(
                f"{MINE}/{program.b1}", json={"masterclass_id": a, "expected_revision": "0"}
            ),
            lambda: laptop.put(
                f"{MINE}/{program.b1}", json={"masterclass_id": b, "expected_revision": "0"}
            ),
        ]
    )
    assert sorted(r.status_code for r in replies) == [200, 409]
    winner = next(r for r in replies if r.status_code == 200).json()["choices"]["choices"][0][
        "masterclass_id"
    ]
    assert program.active(uid) == [(program.b1, winner, "CONFIRMED")]


def test_promotion_invalidates_waitlisted_revision(program: Program) -> None:
    old, other = program.masterclass(program.b1, capacity=1), program.masterclass(program.b1)
    holder, waiter = program.users(2)
    ca, cb = program.client(holder), program.client(waiter)
    choose(ca, program.b1, old)
    choose(cb, program.b1, old, join_waitlist=True)
    before = cb.get(MINE).json()["revisions"][str(program.b1)]
    cancel(ca, program.b1)
    after = cb.get(MINE).json()
    assert after["choices"][0]["status"] == "confirmed"
    assert after["revisions"][str(program.b1)] != before
    stale = cb.put(
        f"{MINE}/{program.b1}", json={"masterclass_id": other, "expected_revision": before}
    )
    assert code(stale) == (409, "MASTERCLASS_CHOICE_CONFLICT")
    assert program.active(waiter) == [(program.b1, old, "CONFIRMED")]


def test_revision_is_required_for_writes(program: Program) -> None:
    a = program.masterclass(program.b1)
    (uid,) = program.users(1)
    client = program.client(uid)
    assert client.put(f"{MINE}/{program.b1}", json={"masterclass_id": a}).status_code == 422
    assert client.delete(f"{MINE}/{program.b1}").status_code == 422
    assert program.active(uid) == []


def test_standard_label_metadata_and_custom_names(program: Program) -> None:
    mc = program.masterclass(program.b1)
    client = TestClient(app)
    event = client.get(PROGRAM).json()["events"][0]
    assert event["blocks"][0]["display_number"] == 1
    assert event["blocks"][0]["masterclasses"][0]["room_display_number"] == 1
    program.rig.sql(
        "UPDATE masterclass_blocks SET title = 'Custom session', display_number = NULL "
        "WHERE id = :id",
        id=program.b1,
    )
    program.rig.sql(
        "UPDATE masterclass_rooms SET name = 'Executive suite', display_number = NULL WHERE "
        "id = :id",
        id=program.rooms[0],
    )
    event = client.get(PROGRAM).json()["events"][0]
    assert event["blocks"][0]["title"] == "Custom session"
    assert event["blocks"][0]["display_number"] is None
    assert event["blocks"][0]["masterclasses"][0]["id"] == mc
    assert event["blocks"][0]["masterclasses"][0]["room"] == "Executive suite"
    assert event["blocks"][0]["masterclasses"][0]["room_display_number"] is None


def test_admin_can_choose_standard_or_custom_labels(
    program: Program, admin_client: TestClient
) -> None:
    for path, identifier, field, name in [
        ("masterclass-blocks", program.b1, "title", "Special block"),
        ("masterclass-rooms", program.rooms[0], "name", "Special hall"),
    ]:
        url = f"{ADMIN}/{path}/{identifier}"
        clear = admin_client.patch(url, json={field: name, "display_number": None})
        assert clear.status_code == 200 and clear.json()["display_number"] is None
        numbered = admin_client.patch(url, json={"display_number": 7})
        assert numbered.status_code == 200 and numbered.json()["display_number"] == 7
        assert numbered.json()[field] == name
