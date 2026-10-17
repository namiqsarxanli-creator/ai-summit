"""People compatibility, meetings, blocks and mutual-scan connections (0021)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.people import routers as people_routers
from app.modules.people.compatibility import compatibility
from app.modules.staff.models import StaffRole
from tests.test_competition import Comp, baku
from tests.test_competition import comp as comp  # noqa: F401 - the fixture

PEOPLE = "/api/attendees/me/people"
REQUESTS = "/api/attendees/me/meeting-requests"


@pytest.fixture
def people(comp: Comp, monkeypatch: pytest.MonkeyPatch) -> Comp:  # noqa: F811
    monkeypatch.setattr(people_routers, "_now", comp.clock)
    return comp


def card(p: Comp, uid: int, **fields: Any) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    p.rig.sql(f"UPDATE users SET {sets} WHERE id = :u", u=uid, **fields)


def ask(c: TestClient, recipient: int, command: str | None = None) -> httpx.Response:
    return cast(
        httpx.Response,
        c.post(REQUESTS, json={"recipient_id": recipient, "command_id": command or str(uuid4())}),
    )


def reason(r: httpx.Response) -> str:
    return str(r.json()["error"]["details"]["reason"])


# --- compatibility -------------------------------------------------------------------


class _U:
    def __init__(self, **kw: Any) -> None:
        self.card_sector = kw.get("sector")
        self.card_topics = kw.get("topics", [])
        self.card_seeking = kw.get("seeking", [])
        self.card_offering = kw.get("offering", [])


def test_the_score_follows_the_document_and_is_bounded() -> None:
    a = _U(sector="energy", topics=["data", "erp", "hr", "design"], seeking=["mentor", "partner"])
    b = _U(
        sector="energy",
        topics=["data", "erp", "hr", "design"],
        offering=["mentoring", "partnership"],
    )
    m = compatibility(a, b, 2)  # type: ignore[arg-type]
    # complement 40 ONCE (two matching pairs do not add up), topics capped at 30,
    # two shared masterclasses 15, same sector 15
    assert m.score == 100 and m.complement == ("mentor", "mentoring")
    assert compatibility(_U(topics=["data"]), _U(topics=["data"]), 1).score == 20  # type: ignore[arg-type]
    assert compatibility(_U(), _U(), 0).score == 0  # type: ignore[arg-type]
    friendship = compatibility(_U(seeking=["friendship"]), _U(offering=["friendship_hobby"]), 0)  # type: ignore[arg-type]
    assert friendship.score == 40 and friendship.reasons == ("complement",)


def test_suggestions_skip_self_same_company_blocked_and_weak_and_never_pad(people: Comp) -> None:
    me, colleague, strong, weak, blocked = people.users(5)
    card(people, me, company="Acme LLC", card_topics=["data", "erp"], card_seeking=["mentor"])
    card(people, colleague, company="  acme   llc ", card_offering=["mentoring"])
    card(people, strong, company="Other", card_offering=["mentoring"], card_topics=["data"])
    card(people, weak, card_topics=["data"])  # 10: below 30
    card(people, blocked, card_offering=["mentoring"])
    people.rig.sql(
        "INSERT INTO user_blocks (blocker_id, blocked_id) VALUES (:a, :b)", a=blocked, b=me
    )
    found = people.client(me).get(PEOPLE).json()["people"]
    assert [p["user_id"] for p in found] == [strong]
    assert found[0]["score"] == 50 and found[0]["reasons"] == ["complement", "topics"]
    assert "email" not in str(found) and "phone" not in str(found) and "linkedin" not in str(found)


def test_a_shared_confirmed_masterclass_raises_the_score(people: Comp) -> None:
    a, b = people.users(2)
    card(people, a, card_topics=["data", "erp"])
    card(people, b, card_topics=["data", "erp"])
    mc = people.masterclass(people.b1)
    people.fill(mc, people.b1, [a, b], "CONFIRMED")
    (found,) = people.client(a).get(PEOPLE).json()["people"]
    assert (found["score"], found["shared_masterclasses"]) == (30, 1)


# --- requests and meetings ----------------------------------------------------------------


def _tables(p: Comp, n: int) -> list[int]:
    return [
        int(
            p.rig.scalar(
                "INSERT INTO match_tables (event_id, label) VALUES (:e, :l) RETURNING id",
                e=p.event,
                l=f"M{i + 1}",
            )
        )
        for i in range(n)
    ]


def test_a_request_is_idempotent_one_per_pair_and_limited_to_ten_a_day(people: Comp) -> None:
    me, *others = people.users(12)
    c = people.client(me)
    people.clock.now = baku("09:30")
    command = str(uuid4())
    first = ask(c, others[0], command)
    assert (
        first.status_code == 200 and ask(c, others[0], command).json()["id"] == first.json()["id"]
    )
    assert reason(ask(c, others[0])) == "already_open"
    assert reason(ask(people.client(others[0]), me)) == "they_asked_you"
    for other in others[1:10]:
        assert ask(c, other).status_code == 200
    assert reason(ask(c, others[10])) == "daily_limit"
    assert reason(ask(c, me)) == "self"


def test_accepting_books_a_free_table_and_never_double_books(people: Comp) -> None:
    _tables(people, 1)
    a, b, c_ = people.users(3)
    people.clock.now = baku("10:02")
    ca, cb, cc = people.client(a), people.client(b), people.client(c_)
    r1 = ask(ca, b).json()
    r2 = ask(cc, b).json()
    one = cb.post(f"{REQUESTS}/{r1['id']}/respond", json={"accept": True})
    two = cb.post(f"{REQUESTS}/{r2['id']}/respond", json={"accept": True})
    assert one.status_code == 200 and two.status_code == 200
    mine = cb.get("/api/attendees/me/meetings").json()["requests"]
    times = sorted(m["meeting"]["starts_at"] for m in mine)
    # the next quarter hour, then the next one: b cannot be in two places at once
    assert times == ["2026-11-13T06:15:00Z", "2026-11-13T06:30:00Z"]
    prompts = mine[0]["meeting"]["prompts"]
    assert 2 <= len(prompts) <= 3
    assert all(m["meeting"]["table"] == "M1" for m in mine)


def test_parallel_acceptances_never_share_a_table_slot(people: Comp) -> None:
    _tables(people, 1)
    pairs = [people.users(2) for _ in range(4)]
    people.clock.now = baku("10:00")
    requests = []
    for asker, answerer in pairs:
        requests.append((people.client(answerer), ask(people.client(asker), answerer).json()["id"]))
    with ThreadPoolExecutor(4) as pool:
        replies = list(
            pool.map(
                lambda t: t[0].post(f"{REQUESTS}/{t[1]}/respond", json={"accept": True}), requests
            )
        )
    assert all(r.status_code == 200 for r in replies), [r.text for r in replies]
    slots = people.rig.sql(
        "SELECT starts_at FROM meetings WHERE cancelled_at IS NULL ORDER BY starts_at"
    )
    assert len({s[0] for s in slots}) == 4  # one table: four different quarter hours


def test_no_table_configured_leaves_the_request_pending(people: Comp) -> None:
    a, b = people.users(2)
    people.clock.now = baku("10:00")
    rid = ask(people.client(a), b).json()["id"]
    refused = people.client(b).post(f"{REQUESTS}/{rid}/respond", json={"accept": True})
    assert reason(refused) == "no_tables"
    assert (
        people.rig.scalar("SELECT status FROM meeting_requests WHERE id = :r", r=rid) == "PENDING"
    )


def test_a_block_hides_the_person_and_cancels_what_is_open(people: Comp) -> None:
    _tables(people, 1)
    a, b = people.users(2)
    people.clock.now = baku("10:00")
    ca, cb = people.client(a), people.client(b)
    rid = ask(ca, b).json()["id"]
    cb.post(f"{REQUESTS}/{rid}/respond", json={"accept": True})
    assert cb.post("/api/attendees/me/blocks", json={"user_id": a}).json() == {"blocked": True}
    assert (
        people.rig.scalar("SELECT status FROM meeting_requests WHERE id = :r", r=rid) == "CANCELLED"
    )
    assert people.rig.scalar("SELECT count(*) FROM meetings WHERE cancelled_at IS NULL") == 0
    # a blocked person looks exactly like an unknown one
    assert ask(ca, b).json()["error"]["code"] == "PEOPLE_NOT_FOUND"


def test_only_the_recipient_answers_and_only_the_requester_withdraws_a_pending_request(
    people: Comp,
) -> None:
    a, b, stranger = people.users(3)
    rid = ask(people.client(a), b).json()["id"]
    assert (
        people.client(stranger).post(f"{REQUESTS}/{rid}/respond", json={"accept": True}).status_code
        == 404
    )
    assert reason(people.client(b).post(f"{REQUESTS}/{rid}/cancel")) == "decline_instead"
    assert (
        people.client(b).post(f"{REQUESTS}/{rid}/respond", json={"accept": False}).json()["status"]
        == "declined"
    )


# --- connections and the contact gate ---------------------------------------------------------


def scan(c: TestClient, token: str) -> httpx.Response:
    return cast(
        httpx.Response, c.post("/api/attendees/me/connections/scan", json={"card_token": token})
    )


def test_a_mutual_scan_connects_once_and_opens_the_contact_to_the_pair_only(people: Comp) -> None:
    a, b, other = people.users(3)
    ca, cb, co = people.client(a), people.client(b), people.client(other)
    people.clock.now = baku("12:00")
    assert scan(ca, people.card(a)).json()["error"]["details"]["reason"] == "self"
    assert scan(ca, people.card(b)).json()["connected"] is False
    card_url = f"/api/cards/{people.card(b)}"
    assert ca.get(card_url).json()["contact"] is None  # one direction is not enough
    done = scan(cb, people.card(a)).json()
    assert done["connected"] is True and done["kind"] == "other" and done["points"] == 1
    assert ca.get(card_url).json()["contact"] is not None
    assert co.get(card_url).json()["contact"] is None
    assert TestClient(app).get(card_url).json()["contact"] is None  # anonymous
    assert scan(ca, people.card(b)).json()["outcome"] == "already"
    assert people.points(a, "CONNECTION") == 1 and people.points(b, "CONNECTION") == 1
    contacts = ca.get("/api/attendees/me/connections").json()["connections"]
    assert [c["user_id"] for c in contacts] == [b] and contacts[0]["contact"]["type"] == "EMAIL"


def test_an_accepted_app_meeting_makes_a_match_and_same_company_earns_nothing(people: Comp) -> None:
    _tables(people, 1)
    a, b, c_, d = people.users(4)
    card(people, c_, company="Same Co")
    card(people, d, company="same co")
    people.clock.now = baku("10:00")
    rid = ask(people.client(a), b).json()["id"]
    people.client(b).post(f"{REQUESTS}/{rid}/respond", json={"accept": True})
    scan(people.client(a), people.card(b))
    assert scan(people.client(b), people.card(a)).json()["kind"] == "match"
    assert people.points(a, "CONNECTION") == 3
    scan(people.client(c_), people.card(d))
    colleagues = scan(people.client(d), people.card(c_)).json()
    assert colleagues["same_company"] is True and colleagues["points"] == 0
    assert people.points(c_, "CONNECTION") == 0
    assert people.rig.scalar("SELECT count(*) FROM connections") == 2  # history kept


def _connect(p: Comp, a: int, b: int) -> None:
    scan(p.client(a), p.card(b))
    scan(p.client(b), p.card(a))


def test_only_the_best_four_count_and_a_better_fifth_replaces_the_weakest(people: Comp) -> None:
    me, *others = people.users(7)
    people.clock.now = baku("12:00")
    for other in others[:4]:  # four ordinary connections: 1 each
        _connect(people, me, other)
    assert people.points(me, "CONNECTION") == 4
    _connect(people, me, others[4])  # a fifth ordinary one is kept, not counted
    assert people.points(me, "CONNECTION") == 4
    # a fifth that is a match (3) replaces one counted 1 - by a reversal entry
    _tables(people, 1)
    rid = ask(people.client(me), others[5]).json()["id"]
    people.client(others[5]).post(f"{REQUESTS}/{rid}/respond", json={"accept": True})
    _connect(people, me, others[5])
    assert people.points(me, "CONNECTION") == 6
    rows = people.rig.sql(
        "SELECT entry_type, points FROM points_log "
        "WHERE user_id = :u AND source = 'CONNECTION' ORDER BY id",
        u=me,
    )
    assert [tuple(r) for r in rows].count(("REVERSAL", -1)) == 1
    assert (
        people.rig.scalar(
            "SELECT count(*) FROM connections WHERE user_low_id = :u OR user_high_id = :u", u=me
        )
        == 6
    )


def test_after_the_close_a_connection_is_kept_but_nothing_is_reversed_or_paid(people: Comp) -> None:
    me, *others = people.users(6)
    people.clock.now = baku("12:00")
    for other in others[:4]:
        _connect(people, me, other)
    _tables(people, 1)
    rid = ask(people.client(me), others[4]).json()["id"]
    people.client(others[4]).post(f"{REQUESTS}/{rid}/respond", json={"accept": True})
    people.clock.now = baku("17:31")
    _connect(people, me, others[4])
    assert people.points(me, "CONNECTION") == 4  # unchanged: no reversal without the new award


def test_admin_configures_match_corner_tables_with_audit(people: Comp) -> None:
    people.rig.sign_in(StaffRole.ADMIN)
    made = people.rig.client.post(
        "/api/admin/match-tables", json={"event_id": people.event, "label": "M1"}
    )
    assert made.status_code == 201
    dup = people.rig.client.post(
        "/api/admin/match-tables", json={"event_id": people.event, "label": "M1"}
    )
    assert dup.json()["error"]["code"] == "ADMIN_CONFLICT"
    off = people.rig.client.patch(
        f"/api/admin/match-tables/{made.json()['id']}", json={"is_active": False}
    )
    assert off.json()["is_active"] is False
    assert {r["action"] for r in people.rig.audit_rows()} >= {
        "match_table.create",
        "match_table.update",
    }
