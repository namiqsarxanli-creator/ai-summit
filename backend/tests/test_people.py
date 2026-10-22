"""People compatibility, meetings, blocks and mutual-scan connections (0021)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
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
    m = compatibility(a, b, 2)
    # complement 40 ONCE (two matching pairs do not add up), topics capped at 30,
    # two shared masterclasses 15, same sector 15
    assert m.score == 100 and m.complement == ("mentor", "mentoring")
    assert compatibility(_U(topics=["data"]), _U(topics=["data"]), 1).score == 20
    assert compatibility(_U(), _U(), 0).score == 0
    friendship = compatibility(_U(seeking=["friendship"]), _U(offering=["friendship_hobby"]), 0)
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
    assert scan(ca, people.card(b)).json()["outcome"] == "already_paid"
    assert people.points(a, "CONNECTION") == 1 and people.points(b, "CONNECTION") == 1
    contacts = ca.get("/api/attendees/me/connections").json()["connections"]
    assert [c["user_id"] for c in contacts] == [b] and contacts[0]["contact"]["type"] == "EMAIL"


def test_recommendation_without_meeting_makes_a_match_and_same_company_earns_nothing(
    people: Comp,
) -> None:
    a, b, c_, d = people.users(4)
    card(people, a, card_seeking=["mentor"])
    card(people, b, card_offering=["mentoring"])
    card(people, c_, company="Same Co")
    card(people, d, company="same co")
    people.clock.now = baku("10:00")
    assert people.rig.scalar("SELECT count(*) FROM meetings") == 0
    assert b in [p["user_id"] for p in people.client(a).get(PEOPLE).json()["people"]]
    scan(people.client(a), people.card(b))
    assert scan(people.client(b), people.card(a)).json()["kind"] == "match"
    assert people.points(a, "CONNECTION") == 3
    scan(people.client(c_), people.card(d))
    colleagues = scan(people.client(d), people.card(c_)).json()
    assert colleagues["same_company"] is True and colleagues["points"] == 0
    assert people.points(c_, "CONNECTION") == 0
    assert people.rig.scalar("SELECT count(*) FROM connections") == 2  # history kept


def test_meeting_alone_does_not_replace_a_recommendation(people: Comp) -> None:
    _tables(people, 1)
    a, b = people.users(2)
    people.clock.now = baku("10:00")
    rid = ask(people.client(a), b).json()["id"]
    assert (
        people.client(b).post(f"{REQUESTS}/{rid}/respond", json={"accept": True}).status_code == 200
    )
    scan(people.client(a), people.card(b))
    result = scan(people.client(b), people.card(a)).json()
    assert result["kind"] == "other" and result["points"] == 1
    assert people.points(a, "CONNECTION") == 1


def test_recommendation_changes_do_not_reclassify_an_existing_connection(people: Comp) -> None:
    a, b = people.users(2)
    people.clock.now = baku("12:00")
    _connect(people, a, b)
    card(people, a, card_seeking=["mentor"])
    card(people, b, card_offering=["mentoring"])
    result = scan(people.client(a), people.card(b)).json()
    assert result["kind"] == "other" and result["outcome"] == "already_paid"
    assert people.points(a, "CONNECTION") == 1


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
    card(people, me, card_seeking=["mentor"])
    card(people, others[5], card_offering=["mentoring"])
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


# --- honest scan outcomes (configuration and networking audit, 20 Oct 2026) -----------------


def test_a_one_sided_scan_earns_nothing_and_says_it_waits(people: Comp) -> None:
    a, b = people.users(2)
    people.clock.now = baku("12:00")
    first = scan(people.client(a), people.card(b)).json()
    assert first["connected"] is False and first["mutual"] is False
    assert first["outcome"] == "waiting" and first["points"] == 0
    assert people.points(a, "CONNECTION") == 0


def test_without_a_close_time_a_connection_says_not_configured_never_closed(
    people: Comp,
) -> None:
    a, b = people.users(2)
    people.rig.sql("UPDATE masterclass_events SET competition_closes_at = NULL")
    people.clock.now = baku("12:00")
    scan(people.client(a), people.card(b))
    done = scan(people.client(b), people.card(a)).json()
    assert done["connected"] is True and done["mutual"] is True
    assert done["outcome"] == "not_configured" and done["points"] == 0
    # A later scan does not pretend the connection was paid, and pays nothing.
    again = scan(people.client(a), people.card(b)).json()
    assert again["outcome"] == "unpaid" and again["points"] == 0
    assert people.points(a, "CONNECTION") == people.points(b, "CONNECTION") == 0
    mine = people.client(a).get("/api/attendees/me/connections").json()["connections"]
    assert mine[0]["points"] == 0 and mine[0]["paid"] is False


def test_points_are_what_this_person_holds_and_a_retry_pays_nothing_twice(people: Comp) -> None:
    a, b = people.users(2)
    people.clock.now = baku("12:00")
    _connect(people, a, b)
    again = scan(people.client(a), people.card(b)).json()
    assert again["outcome"] == "already_paid" and again["points"] == 1
    assert (
        people.rig.scalar(
            "SELECT count(*) FROM points_log WHERE user_id = :u AND source = 'CONNECTION'", u=a
        )
        == 1
    )
    excluded, other = people.users(2)
    card(people, excluded, competition_excluded_reason="Speaker")
    scan(people.client(excluded), people.card(other))
    done = scan(people.client(other), people.card(excluded)).json()
    assert done["outcome"] == "awarded" and done["points"] == 1  # the other one earns
    mine = scan(people.client(excluded), people.card(other)).json()
    assert mine["outcome"] == "unpaid" and mine["reason"] == "ineligible" and mine["points"] == 0


# --- unpaid connections: the administrator's review (people/reconcile.py) ------------------

UNPAID = "/api/admin/connections/unpaid"
RECONCILE = "/api/admin/connections/reconcile"


def _admin(p: Comp) -> TestClient:
    return p.staff_client(p.rig.staff_id(StaffRole.ADMIN))


def _writes(p: Comp) -> tuple[int, int]:
    return (
        int(p.rig.scalar("SELECT count(*) FROM points_log")),
        int(p.rig.scalar("SELECT count(*) FROM admin_audit")),
    )


def _unpaid_while_unconfigured(p: Comp, a: int, b: int, at: str = "12:00") -> None:
    p.rig.sql("UPDATE masterclass_events SET competition_closes_at = NULL")
    p.clock.now = baku(at)
    _connect(p, a, b)
    p.rig.sql("UPDATE masterclass_events SET competition_closes_at = :c", c=baku("17:30"))


def _sides(body: dict[str, Any]) -> dict[int, str]:
    return {i["user_id"]: i["category"] for i in body["items"]}


def test_the_review_is_read_only_and_says_why_each_side_is_unpaid(people: Comp) -> None:
    a, b, c_, d, excluded, e = people.users(6)
    _unpaid_while_unconfigured(people, a, b)
    card(people, c_, company="Same Co")
    card(people, d, company="same co")
    _connect(people, c_, d)  # same company: 0 by rule
    card(people, excluded, competition_excluded_reason="Speaker")
    _connect(people, excluded, e)  # e is paid, the excluded side is not
    people.clock.now = baku("17:31")
    late1, late2 = people.users(2)
    _connect(people, late1, late2)  # after the close
    admin = _admin(people)
    before = _writes(people)
    body = admin.get(UNPAID).json()
    assert _writes(people) == before  # a dry run writes nothing
    sides = _sides(body)
    assert sides[a] == sides[b] == "eligible"
    assert sides[c_] == sides[d] == "same_company"
    assert sides[excluded] == "ineligible" and e not in sides  # e was paid
    assert sides[late1] == sides[late2] == "after_close"
    assert body["counts"]["eligible"] == 2
    assert "@" not in str(body) and "+1" not in str(body)  # short names, no contact data


def test_only_administrators_review_or_apply(people: Comp) -> None:
    a, b = people.users(2)
    _unpaid_while_unconfigured(people, a, b)
    attendee = people.client(a)
    gm = people.volunteer("GAME_MASTER", None)
    body = {"items": [{"connection_id": 1, "user_id": a}], "reason": "Check", "confirm": True}
    for c in (attendee, gm, TestClient(app)):
        assert c.get(UNPAID).status_code in (401, 403)
        assert c.post(RECONCILE, json=body).status_code in (401, 403)
    assert people.points(a, "CONNECTION") == 0


def test_applying_needs_selection_reason_and_confirmation_pays_once_and_is_audited(
    people: Comp,
) -> None:
    a, b = people.users(2)
    _unpaid_while_unconfigured(people, a, b)
    admin = _admin(people)
    item = next(i for i in admin.get(UNPAID).json()["items"] if i["user_id"] == a)
    one = {"connection_id": item["connection_id"], "user_id": a}
    assert (
        admin.post(RECONCILE, json={"items": [one], "reason": "Close set late"}).status_code == 422
    )
    assert (
        admin.post(RECONCILE, json={"items": [one], "reason": "x", "confirm": True}).status_code
        == 422
    )
    assert (
        admin.post(
            RECONCILE, json={"items": [], "reason": "Close set late", "confirm": True}
        ).status_code
        == 422
    )
    assert people.points(a, "CONNECTION") == 0
    done = admin.post(RECONCILE, json={"items": [one], "reason": "Close set late", "confirm": True})
    assert done.status_code == 200, done.text
    assert done.json()["results"] == [{**one, "result": "awarded", "points": 1}]
    assert (
        people.points(a, "CONNECTION") == 1 and people.points(b, "CONNECTION") == 0
    )  # selected side only
    # Dated by the moment the pair connected, not by the review.
    assert people.rig.scalar(
        "SELECT occurred_at FROM points_log WHERE user_id = :u AND source = 'CONNECTION'", u=a
    ) == baku("12:00")
    audit = people.rig.audit_rows("connection.reconcile")
    assert len(audit) == 1 and audit[0]["after_value"]["reason"] == "Close set late"
    assert len(people.rig.audit_rows("connection.reconcile_batch")) == 1
    again = admin.post(
        RECONCILE, json={"items": [one], "reason": "Close set late", "confirm": True}
    )
    assert again.json()["results"] == [{**one, "result": "already_paid"}]
    assert people.points(a, "CONNECTION") == 1
    assert len(people.rig.audit_rows("connection.reconcile")) == 1
    # The person's own view now says paid.
    mine = people.client(a).get("/api/attendees/me/connections").json()["connections"]
    assert mine[0]["paid"] is True and mine[0]["points"] == 1


def test_a_connection_outside_the_event_day_needs_an_organiser_decision(people: Comp) -> None:
    a, b = people.users(2)
    people.rig.sql("UPDATE masterclass_events SET competition_closes_at = NULL")
    people.clock.now = baku("12:00") - timedelta(days=1)  # during setup, the day before
    _connect(people, a, b)
    people.rig.sql("UPDATE masterclass_events SET competition_closes_at = :c", c=baku("17:30"))
    admin = _admin(people)
    item = next(i for i in admin.get(UNPAID).json()["items"] if i["user_id"] == a)
    assert item["category"] == "needs_decision"
    one = {"connection_id": item["connection_id"], "user_id": a}
    r = admin.post(RECONCILE, json={"items": [one], "reason": "Try it", "confirm": True}).json()
    assert r["results"] == [{**one, "result": "needs_decision"}]
    assert people.points(a, "CONNECTION") == 0


def test_the_frozen_kind_decides_never_todays_recommendation(people: Comp) -> None:
    a, b = people.users(2)
    _unpaid_while_unconfigured(people, a, b)  # an ordinary connection: 1
    card(people, a, card_seeking=["mentor"])
    card(people, b, card_offering=["mentoring"])  # recommended NOW, not then
    admin = _admin(people)
    item = next(i for i in admin.get(UNPAID).json()["items"] if i["user_id"] == a)
    assert item["kind"] == "other" and item["points"] == 1
    one = {"connection_id": item["connection_id"], "user_id": a}
    admin.post(RECONCILE, json={"items": [one], "reason": "Close set late", "confirm": True})
    assert people.points(a, "CONNECTION") == 1


def test_a_failure_part_way_leaves_nothing_behind(
    people: Comp, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.people import connections

    a, b = people.users(2)
    _unpaid_while_unconfigured(people, a, b)
    admin = _admin(people)
    items = [
        {"connection_id": i["connection_id"], "user_id": i["user_id"]}
        for i in admin.get(UNPAID).json()["items"]
        if i["user_id"] in (a, b)
    ]
    real = connections._pay
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("the database went away")
        return real(*args, **kwargs)

    monkeypatch.setattr(connections, "_pay", flaky)
    before = _writes(people)
    no_raise = TestClient(app, raise_server_exceptions=False, headers=dict(admin.headers))
    no_raise.cookies.update(admin.cookies)
    r = no_raise.post(RECONCILE, json={"items": items, "reason": "Close set late", "confirm": True})
    assert r.status_code == 500
    assert _writes(people) == before  # all or nothing
    assert people.points(a, "CONNECTION") == people.points(b, "CONNECTION") == 0


def test_applying_keeps_the_best_four_rule_and_existing_reversals(people: Comp) -> None:
    me, *others = people.users(6)
    people.clock.now = baku("11:00")
    for other in others[:4]:
        _connect(people, me, other)  # four counted ordinary connections
    assert people.points(me, "CONNECTION") == 4
    card(people, me, card_seeking=["mentor"])
    card(people, others[4], card_offering=["mentoring"])
    _unpaid_while_unconfigured(people, me, others[4])  # a MATCH that was never paid
    admin = _admin(people)
    item = next(i for i in admin.get(UNPAID).json()["items"] if i["user_id"] == me)
    assert item["kind"] == "match" and item["category"] == "eligible"
    one = {"connection_id": item["connection_id"], "user_id": me}
    r = admin.post(RECONCILE, json={"items": [one], "reason": "Close set late", "confirm": True})
    assert r.json()["results"][0]["result"] == "awarded"
    assert people.points(me, "CONNECTION") == 6  # 3 replaces a counted 1 by a reversal
    replaced = people.rig.scalar(
        "SELECT count(*) FROM points_log WHERE user_id = :u AND source = 'CONNECTION' "
        "AND entry_type = 'REVERSAL'",
        u=me,
    )
    assert replaced == 1
    # A replaced side is not "unpaid": it is never listed or paid again.
    assert me not in {i["user_id"] for i in admin.get(UNPAID).json()["items"]}


def test_an_eligibility_change_after_the_connection_needs_a_decision(people: Comp) -> None:
    """Today's eligibility is no evidence of the eligibility when the pair met."""
    a, b = people.users(2)
    card(people, a, competition_excluded_reason="Volunteer")
    _unpaid_while_unconfigured(people, a, b)
    admin = _admin(people)
    r = admin.patch(f"/api/admin/users/{a}/competition-eligibility", json={"excluded_reason": None})
    assert r.status_code == 200, r.text
    # The change is recorded now; on the event day it would come after 12:00.
    people.rig.sql(
        "UPDATE admin_audit SET created_at = :t WHERE action = 'competition.eligibility'",
        t=baku("13:00"),
    )
    item = next(i for i in admin.get(UNPAID).json()["items"] if i["user_id"] == a)
    assert item["category"] == "needs_decision"
    one = {"connection_id": item["connection_id"], "user_id": a}
    done = admin.post(RECONCILE, json={"items": [one], "reason": "Check it", "confirm": True})
    assert done.json()["results"] == [{**one, "result": "needs_decision"}]
    assert people.points(a, "CONNECTION") == 0


def test_a_payment_after_a_final_snapshot_says_a_new_review_is_needed(
    people: Comp, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.competition import final_routers

    monkeypatch.setattr(final_routers, "_now", people.clock)
    a, b = people.users(2)
    _unpaid_while_unconfigured(people, a, b)
    people.clock.now = baku("17:35")
    admin = _admin(people)
    draft = admin.post("/api/staff/final")
    assert draft.status_code == 201, draft.text
    item = next(i for i in admin.get(UNPAID).json()["items"] if i["user_id"] == a)
    one = {"connection_id": item["connection_id"], "user_id": a}
    done = admin.post(RECONCILE, json={"items": [one], "reason": "Close set late", "confirm": True})
    assert done.json()["results"][0]["result"] == "awarded"
    assert done.json()["final_review_needed"] is True
    # The existing safeguard: the draft taken before the payment cannot be published.
    stale = admin.post(f"/api/staff/final/{draft.json()['id']}/publish")
    assert stale.status_code == 409
    assert stale.json()["error"]["details"]["reason"] == "stale_snapshot"
