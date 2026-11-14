"""The attendee's notifications (app/modules/notifications): derived from the real
rows on every read, own items only, people I blocked never shown.

Rig database (disposable, freshly seeded); server clocks replaced per test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.notifications import routers as notification_routers
from app.modules.people import routers as people_routers
from tests.test_competition import Comp, baku, code_of, comp, scan  # noqa: F401
from tests.test_masterclasses import choose


@pytest.fixture(autouse=True)
def _clock(comp: Comp, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(notification_routers, "_now", comp.clock)
    monkeypatch.setattr(people_routers, "_now", comp.clock)


def feed(c: TestClient) -> list[dict[str, Any]]:
    r = c.get("/api/attendees/me/notifications")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    return list(r.json()["items"])


def kinds(items: list[dict[str, Any]]) -> list[str]:
    return [i["kind"] for i in items]


def test_signed_out_gets_nothing() -> None:
    assert TestClient(app).get("/api/attendees/me/notifications").status_code == 401


def test_the_programme_reminds_the_evening_before_and_within_the_hour(comp: Comp) -> None:  # noqa: F811
    (uid,) = comp.users(1)
    c = comp.client(uid)
    mc = comp.masterclass(comp.b1, title="Data for AI", capacity=5)
    comp.clock.now = baku("08:00")
    assert choose(c, comp.b1, mc).status_code == 200
    comp.clock.now = datetime(2026, 11, 12, 13, 0, tzinfo=UTC)  # 12 Nov 17:00 Baku
    assert "programme_eve" not in kinds(feed(c))
    comp.clock.now = datetime(2026, 11, 12, 14, 30, tzinfo=UTC)  # 18:30 the evening before
    (eve,) = [i for i in feed(c) if i["kind"] == "programme_eve"]
    assert eve["params"] == {"count": 1} and eve["href"] == "/programme"
    comp.clock.now = baku("10:00")  # the block starts at 11:30
    assert "programme_soon" not in kinds(feed(c))
    comp.clock.now = baku("10:45")
    (soon,) = [i for i in feed(c) if i["kind"] == "programme_soon"]
    assert soon["params"]["title"] == "Data for AI" and soon["params"]["item"] == "masterclass"
    assert soon["params"]["starts_at"] == "2026-11-13T07:30:00+00:00"


def test_points_are_listed_newest_first_and_a_panel_entry_shows_its_eight(comp: Comp) -> None:  # noqa: F811
    (uid,) = comp.users(1)
    c = comp.client(uid)
    comp.clock.now = baku("10:05")
    assert scan(c, code_of(comp, comp.panel1, "ENTRY")).status_code == 200
    items = feed(c)
    (entry,) = [i for i in items if i["kind"] == "points"]
    assert entry["params"] == {"source": "PANEL", "points": 8, "reversal": False}
    assert entry["href"] == "/points"
    other = comp.client(comp.users(1)[0])
    assert [i for i in feed(other) if i["kind"] == "points"] == []  # own items only


def test_a_scan_asks_to_scan_back_until_both_have_scanned(comp: Comp) -> None:  # noqa: F811
    a, b = comp.users(2)
    ca, cb = comp.client(a), comp.client(b)
    comp.clock.now = baku("09:30")
    r = ca.post("/api/attendees/me/connections/scan", json={"card_token": comp.card(b)})
    assert r.status_code == 200, r.text
    (ask,) = [i for i in feed(cb) if i["kind"] == "scan_back"]
    assert ask["href"] == "/my-card" and ask["params"]["name"].endswith(".")
    assert "scan_back" not in kinds(feed(ca))  # the scanner is not asked
    r = cb.post("/api/attendees/me/connections/scan", json={"card_token": comp.card(a)})
    assert r.status_code == 200, r.text
    assert "scan_back" not in kinds(feed(cb))
    assert "connected" in kinds(feed(ca)) and "connected" in kinds(feed(cb))


def test_requests_and_photos_wait_for_an_answer_and_a_blocked_person_is_not_shown(
    comp: Comp,  # noqa: F811
) -> None:
    a, b = comp.users(2)
    ca, cb = comp.client(a), comp.client(b)
    comp.clock.now = baku("09:30")
    comp.rig.sql(
        "INSERT INTO meeting_requests (requester_id, recipient_id, pair_low_id, pair_high_id, "
        "status, command_id, created_at) VALUES (:a, :b, :lo, :hi, 'PENDING', :c, :t)",
        a=a,
        b=b,
        lo=min(a, b),
        hi=max(a, b),
        c=str(uuid4()),
        t=baku("09:00"),
    )
    comp.rig.sql(
        "INSERT INTO pair_photos (uploader_id, partner_id, photo_key, status, created_at) "
        "VALUES (:a, :b, :k, 'PENDING', :t)",
        a=a,
        b=b,
        k=uuid4().hex,
        t=baku("09:10"),
    )
    items = feed(cb)
    assert kinds(items)[:2] == ["pair_photo", "meeting_request"]  # newest first
    assert {i["href"] for i in items[:2]} == {"/people"}
    assert "meeting_request" not in kinds(feed(ca)) and "pair_photo" not in kinds(feed(ca))

    comp.rig.sql(
        "UPDATE meeting_requests SET status = 'ACCEPTED', responded_at = :t", t=baku("09:20")
    )
    comp.rig.sql("UPDATE pair_photos SET status = 'ACCEPTED', accepted_at = :t", t=baku("09:25"))
    assert "meeting_request" not in kinds(feed(cb)) and "pair_photo" not in kinds(feed(cb))
    answered = {i["kind"]: i for i in feed(ca)}
    assert answered["meeting_answer"]["params"]["accepted"] is True
    assert "pair_photo_accepted" in answered

    comp.rig.sql("INSERT INTO user_blocks (blocker_id, blocked_id) VALUES (:a, :b)", a=a, b=b)
    assert "meeting_answer" not in kinds(feed(ca)) and "pair_photo_accepted" not in kinds(feed(ca))


def test_the_grand_final_code_is_announced_while_it_is_open(comp: Comp) -> None:  # noqa: F811
    (uid,) = comp.users(1)
    c = comp.client(uid)
    comp.rig.sql(
        "INSERT INTO panels (event_id, code, title, starts_at, ends_at) "
        "VALUES (:e, 'grand-final', 'Grand Final', :s, :f)",
        e=comp.event,
        s=baku("17:30"),
        f=baku("17:45"),
    )
    comp.clock.now = baku("17:29")
    assert "grand_final_open" not in kinds(feed(c))
    comp.clock.now = baku("17:31")
    (gf,) = [i for i in feed(c) if i["kind"] == "grand_final_open"]
    assert gf["params"]["ends_at"] == "2026-11-13T13:45:00+00:00"
    comp.clock.now = baku("17:45")
    assert "grand_final_open" not in kinds(feed(c))
