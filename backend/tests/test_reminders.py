"""Programme reminders: switch, consent, dedupe, retries - and never a real email.

Every send goes to `FakeProvider` (tests/outbox_support.py); the suite's
`no_real_email` guard additionally fails any stray network call.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.db.session import mutating_transaction
from app.modules.mentors import routers as mentor_routers
from app.modules.mentors import service as mentor_service
from app.modules.reminders import service
from tests.api_rig import Rig
from tests.outbox_support import FakeProvider, accepted, permanent, retryable
from tests.test_mentors import Clock, Mentors, baku, book, cancel, listing


@pytest.fixture
def rm(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Iterator[Mentors]:
    clock = Clock()
    clock.now = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(mentor_service, "now", clock)
    monkeypatch.setattr(mentor_routers, "_now", clock)
    p = Mentors(rig)
    p.clock = clock
    yield p
    for c in p.clients:
        c.close()


def plan(rm: Mentors, at: datetime) -> dict[str, int]:
    with Session(rm.rig.engine) as db, mutating_transaction(db) as tx:
        return service.plan(tx, at)


def run(rm: Mentors, provider: FakeProvider, at: datetime) -> int:
    return service.run_once(
        lambda: Session(rm.rig.engine), provider=provider, worker_id="w1", now=lambda: at
    )


def rows(rm: Mentors) -> list[tuple[Any, ...]]:
    return [
        tuple(r)
        for r in rm.rig.sql(
            "SELECT item_key, status, attempt_count, reason_code FROM reminder_deliveries "
            "ORDER BY due_at, item_key"
        )
    ]


def switch(rm: Mentors, on: bool) -> None:
    rm.rig.sql("UPDATE event_config SET reminders_enabled = :o", o=on)


def test_nothing_is_planned_without_the_switch_and_the_attendees_consent(rm: Mentors) -> None:
    uid = rm.users(1)[0]
    c = rm.client(uid)
    book(c, rm.session("13:10"))
    assert c.get("/api/attendees/me/reminders").json() == {
        "channel": "email",
        "available": False,
        "enabled": False,
    }
    assert c.put("/api/attendees/me/reminders", json={"enabled": True}).json()["enabled"] is True
    plan(rm, rm.clock.now)
    assert rows(rm) == []  # switched off by the administrator: nothing at all
    switch(rm, True)
    c.put("/api/attendees/me/reminders", json={"enabled": False})
    plan(rm, rm.clock.now)
    assert rows(rm) == []  # switched on, but this person did not opt in


def test_eve_and_ten_minutes_before_each_confirmed_item_once(rm: Mentors) -> None:
    switch(rm, True)
    uid, other, waiting = rm.users(3)
    c, w = rm.client(uid), rm.client(waiting)
    full = rm.session("15:35", capacity=1)
    book(rm.client(other), full)  # `other` did not opt in
    assert book(w, full).json()["status"] == "waitlisted"
    mine = rm.session("13:10")
    book(c, mine)
    for client in (c, w):
        client.put("/api/attendees/me/reminders", json={"enabled": True})
    assert plan(rm, rm.clock.now)["created"] == 3
    assert plan(rm, rm.clock.now) == {"created": 0, "retimed": 0, "cancelled": 0}  # dedupe
    planned = {
        (int(r[0]), str(r[1]))
        for r in rm.rig.sql("SELECT user_id, item_key FROM reminder_deliveries")
    }
    # A waiting place gets no "starts in 10 minutes"; nobody who did not opt in gets anything.
    assert planned == {
        (uid, "eve"),
        (uid, f"mentor:{listing(c, mine)['booking']['id']}"),
        (waiting, "eve"),
    }
    provider = FakeProvider(accepted())
    assert run(rm, provider, datetime(2026, 11, 12, 13, 59, tzinfo=UTC)) == 0  # not due yet
    assert run(rm, provider, datetime(2026, 11, 12, 14, 0, tzinfo=UTC)) == 2  # 18:00 in Baku
    assert run(rm, provider, datetime(2026, 11, 12, 14, 1, tzinfo=UTC)) == 0  # sent once
    assert run(rm, provider, baku("13:00")) == 1
    assert provider.messages[-1].subject == "13:10 · Careers in AI"
    assert "10 dəqiqədən sonra" in provider.messages[-1].text
    assert {r[1] for r in rows(rm)} == {"SENT"}
    assert len({m.idempotency_key for m in provider.messages}) == 3


def test_a_retryable_failure_backs_off_and_a_permanent_one_stops(rm: Mentors) -> None:
    switch(rm, True)
    uid = rm.users(1)[0]
    c = rm.client(uid)
    book(c, rm.session("13:10"))
    c.put("/api/attendees/me/reminders", json={"enabled": True})
    plan(rm, rm.clock.now)
    rm.rig.sql("DELETE FROM reminder_deliveries WHERE item_key = 'eve'")
    due = baku("13:00")
    provider = FakeProvider(retryable(), retryable(), accepted())
    assert run(rm, provider, due) == 1
    assert rows(rm)[0][1:] == ("PENDING", 1, "provider_unavailable")
    assert run(rm, provider, due + timedelta(seconds=30)) == 0  # waits 60 s
    assert run(rm, provider, due + timedelta(seconds=61)) == 1
    assert run(rm, provider, due + timedelta(seconds=61 + 121)) == 1
    assert rows(rm)[0][1:] == ("SENT", 3, None)
    # Every attempt of one reminder carries the same provider idempotency key.
    assert len({m.idempotency_key for m in provider.messages}) == 1

    rm.rig.sql(
        "UPDATE reminder_deliveries SET status = 'PENDING', sent_at = NULL, attempt_count = 0, "
        "next_attempt_at = due_at"
    )
    assert run(rm, FakeProvider(permanent()), due) == 1
    assert rows(rm)[0][1] == "FAILED"


def test_withdrawn_consent_and_cancelled_bookings_send_nothing(rm: Mentors) -> None:
    switch(rm, True)
    uid = rm.users(1)[0]
    c = rm.client(uid)
    s = rm.session("13:10")
    book(c, s)
    c.put("/api/attendees/me/reminders", json={"enabled": True})
    plan(rm, rm.clock.now)
    cancel(c, listing(c, s)["booking"])
    assert plan(rm, rm.clock.now)["cancelled"] == 1  # the booking is gone
    c.put("/api/attendees/me/reminders", json={"enabled": False})
    assert {r[1] for r in rows(rm)} == {"CANCELLED"}
    provider = FakeProvider()
    assert run(rm, provider, baku("13:00")) == 0
    assert provider.messages == []


def test_a_moved_item_is_retimed_and_a_late_reminder_is_dropped(rm: Mentors) -> None:
    switch(rm, True)
    uid = rm.users(1)[0]
    c = rm.client(uid)
    s = rm.session("13:10")
    book(c, s)
    c.put("/api/attendees/me/reminders", json={"enabled": True})
    plan(rm, rm.clock.now)
    rm.rig.sql("DELETE FROM reminder_deliveries WHERE item_key = 'eve'")
    rm.rig.sql(
        "UPDATE mentor_sessions SET starts_at = :s, ends_at = :s + interval '20 minutes' "
        "WHERE id = :i",
        s=baku("13:35"),
        i=s,
    )
    assert plan(rm, rm.clock.now)["retimed"] == 1
    provider = FakeProvider()
    # The worker was down: 20 minutes after it was due, a reminder is no help.
    assert run(rm, provider, baku("13:45")) == 0
    assert rows(rm)[0][1:] == ("CANCELLED", 0, "stale")
    assert provider.messages == []
