"""Offline scans (review before any award) and the after-event reports.

Rig database (disposable); server clocks replaced per test; times Asia/Baku.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.modules.competition import offline_routers
from app.modules.mentors import routers as mentor_routers
from app.modules.mentors import service as mentor_service
from app.modules.staff.models import StaffRole
from tests.api_rig import Rig
from tests.test_mentors import Clock, Mentors, baku, book


@pytest.fixture
def om(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Iterator[Mentors]:
    clock = Clock()
    monkeypatch.setattr(mentor_service, "now", clock)
    monkeypatch.setattr(mentor_routers, "_now", clock)
    monkeypatch.setattr(offline_routers, "_now", clock)
    p = Mentors(rig)
    p.clock = clock
    rig.sql("UPDATE masterclass_events SET competition_closes_at = :c", c=baku("17:30"))
    yield p
    for c in p.clients:
        c.close()


def game_master(p: Mentors) -> TestClient:
    sid = int(
        p.rig.scalar(
            "INSERT INTO staff_accounts (email, password_hash, full_name, role, is_active) "
            "VALUES (:e, 'x', 'Game Master', 'VOLUNTEER', true) RETURNING id",
            e=f"gm-{uuid4().hex[:8]}@staff.test",
        )
    )
    p.rig.sql(
        "INSERT INTO staff_scopes (staff_account_id, scope, granted_by_staff_id) "
        "VALUES (:s, 'GAME_MASTER', :a)",
        s=sid,
        a=p.rig.staff_id(StaffRole.ADMIN),
    )
    c = TestClient(app, headers={"Origin": get_settings().app_origin})
    c.cookies.set("session", p.rig.session_for(sid))
    p.clients.append(c)
    return c


def filed_under(c: TestClient) -> dict[str, Any]:
    """What a phone files its queue under: GET /api/staff/me (an active session)."""
    me = c.get("/api/staff/me")
    if me.status_code != 200:  # e.g. a deactivated account: send a well-formed label
        return {"queue_owner": "0" * 64, "dataset_generation": 1}
    return {
        "queue_owner": me.json()["queue_owner"],
        "dataset_generation": me.json()["dataset_generation"],
    }


def submit(
    c: TestClient,
    session_id: int,
    token: str,
    at: str,
    scan_id: str | None = None,
    filed: dict[str, Any] | None = None,
) -> Any:
    return cast(
        httpx.Response,
        c.post(
            "/api/staff/offline-scans",
            json={
                "client_scan_id": scan_id or str(uuid4()),
                "kind": "MENTOR_COMPLETE",
                "target_id": session_id,
                "card_token": token,
                "scanned_at": baku(at).isoformat(),
                **(filed if filed is not None else filed_under(c)),
            },
        ),
    )


def approve(
    c: TestClient, scan_id: int, reason: str, verified: str, **extra: Any
) -> httpx.Response:
    """A reviewer's decision: what they verified, when, and why."""
    return cast(
        httpx.Response,
        c.post(
            f"/api/staff/offline-scans/{scan_id}/approve",
            json={"reason": reason, "verified_at": baku(verified).isoformat(), **extra},
        ),
    )


def setup(om: Mentors, start: str = "15:35") -> tuple[int, int, str, TestClient]:
    s = om.session(start)
    uid = om.users(1)[0]
    book(om.client(uid), s)
    _, host = om.host(s)
    return s, uid, om.card(uid), host


def test_a_scan_that_arrives_at_once_is_simply_applied(om: Mentors) -> None:
    s, uid, token, host = setup(om)
    om.clock.now = baku("15:52")
    r = submit(host, s, token, "15:51")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "applied"
    assert r.json()["outcome"]["award"] == {"outcome": "awarded", "points": 5}


def test_a_late_scan_waits_for_review_and_pays_only_when_approved(om: Mentors) -> None:
    s, uid, token, host = setup(om)
    scan_id = str(uuid4())
    om.clock.now = baku("17:45")  # synchronised after the close
    queued = submit(host, s, token, "15:52", scan_id)
    assert queued.json()["status"] == "pending_review"
    again = submit(host, s, token, "15:52", scan_id)  # the phone replays it
    assert again.json()["id"] == queued.json()["id"]
    assert om.rig.scalar("SELECT count(*) FROM offline_scans") == 1
    assert om.mentor_points(uid) == 0  # the phone's clock alone is not proof

    # The scanner holds no review right at all here.
    assert approve(host, queued.json()["id"], "seen it", "15:52").status_code == 403
    gm = game_master(om)
    pending = gm.get("/api/staff/offline-scans").json()["scans"]
    assert [p["attendee"] for p in pending] == ["Guest N."] and pending[0]["scanned_by"] == "Host"
    approved = approve(gm, queued.json()["id"], "The host's paper list confirms 15:52.", "15:52")
    assert approved.json()["status"] == "applied"
    # The claimed time and the verified one are both kept.
    assert approved.json()["device_scanned_at"] == baku("15:52").isoformat()
    assert approved.json()["verified_at"] == baku("15:52").isoformat()
    assert om.mentor_points(uid) == 5
    assert om.statuses(s) == ["ATTENDED"]
    assert (
        om.rig.scalar("SELECT count(*) FROM admin_audit WHERE action = 'offline_scan.approve'") == 1
    )


def test_an_approved_scan_is_still_judged_by_its_own_window(om: Mentors) -> None:
    s, uid, token, host = setup(om)
    om.clock.now = baku("17:45")
    queued = submit(host, s, token, "15:40")  # mid-session: not the end-of-session window
    gm = game_master(om)
    url = f"/api/staff/offline-scans/{queued.json()['id']}/approve"
    assert (
        gm.post(url, json={"reason": "ok", "verified_at": baku("15:40").isoformat()}).status_code
        == 422
    )  # a reason, not a nod
    assert (
        gm.post(url, json={"reason": "Host list says 15:40."}).status_code == 422
    )  # no verified time
    r = approve(gm, queued.json()["id"], "Host list says 15:40.", "15:40")
    assert r.json()["status"] == "failed" and r.json()["outcome"] == {"reason": "outside_window"}
    assert om.mentor_points(uid) == 0 and om.statuses(s) == ["CONFIRMED"]


def test_rejection_future_times_and_revoked_scopes(om: Mentors) -> None:
    s, uid, token, host = setup(om)
    om.clock.now = baku("17:45")
    assert submit(host, s, token, "18:00").json()["error"]["details"]["reason"] == (
        "device_time_in_future"
    )
    queued = submit(host, s, token, "15:53")
    gm = game_master(om)
    r = gm.post(
        f"/api/staff/offline-scans/{queued.json()['id']}/reject", json={"reason": "No list."}
    )
    assert r.json()["status"] == "rejected" and om.mentor_points(uid) == 0
    om.rig.sql("UPDATE staff_scopes SET revoked_at = now() WHERE scope = 'MENTOR_HOST'")
    assert submit(host, s, token, "15:54").status_code == 403
    assert om.rig.scalar("SELECT count(*) FROM offline_scans") == 1


# --- reports ---------------------------------------------------------------------------


def test_the_attendee_report_is_their_own_verified_day(om: Mentors) -> None:
    s, uid, token, host = setup(om)
    om.clock.now = baku("15:52")
    submit(host, s, token, "15:52")
    report = om.client(uid).get("/api/attendees/me/report")
    assert report.status_code == 200 and report.headers["cache-control"] == "no-store"
    body = report.json()
    assert [(i["kind"], i["status"]) for i in body["attended"]] == [("mentor", "attended")]
    assert body["not_completed"] == []
    assert next(c for c in body["categories"] if c["category"] == "MENTOR")["points"] == 5
    assert body["connections"] == [] and body["photos"] == []
    assert body["final"] is None


def test_a_booth_sees_only_its_own_consenting_visitors(om: Mentors) -> None:
    staff_id, booth = (
        int(x)
        for x in om.rig.sql(
            "SELECT id, booth_id FROM staff_accounts WHERE role = 'BOOTH_STAFF' ORDER BY id LIMIT 1"
        )[0]
    )
    om.rig.sign_in_as(om.rig.staff_id(StaffRole.ADMIN))
    prepared = om.rig.client.post(
        "/api/admin/playground/prepare", json={"event_id": om.event, "booth_id": booth}
    )
    assert prepared.status_code == 200, prepared.text
    slot = int(
        om.rig.scalar("SELECT id FROM playground_slots WHERE number = 2 AND booth_id = :b", b=booth)
    )
    consenting, private, elsewhere = om.users(3)
    om.rig.sql(
        "UPDATE users SET sponsor_consent = true, sponsor_consent_at = now() WHERE id IN (:a, :c)",
        a=consenting,
        c=elsewhere,
    )
    for uid in (consenting, private):
        om.rig.sql(
            "INSERT INTO playground_bookings (slot_id, user_id, command_id, status) "
            "VALUES (:s, :u, :c, 'COMPLETED')",
            s=slot,
            u=uid,
            c=str(uuid4()),
        )
    c = TestClient(app, headers={"Origin": get_settings().app_origin})
    c.cookies.set("session", om.rig.session_for(staff_id))
    om.clients.append(c)

    def mine(body: dict[str, Any]) -> list[str]:
        ours = {"N1", "N2", "N3"}  # the three made here (seeded people may also visit)
        return sorted(lead["last_name"] for lead in body["leads"] if lead["last_name"] in ours)

    r = c.get("/api/staff/booth/report")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stats"]["playground_sessions_completed"] >= 2
    # Consent given AND a visit to THIS booth: not the private one, not the one elsewhere.
    assert mine(body) == ["N1"]
    assert body["stats"]["consenting_leads"] == len(body["leads"])
    # Withdrawing consent removes the person at the next read.
    om.rig.sql(
        "UPDATE users SET sponsor_consent = false, sponsor_consent_at = NULL WHERE id = :u",
        u=consenting,
    )
    assert mine(c.get("/api/staff/booth/report").json()) == []
    assert om.rig.scalar("SELECT count(*) FROM admin_audit WHERE action = 'booth.leads'") == 2
    _, volunteer = om.host(om.session("13:10"))
    assert volunteer.get("/api/staff/booth/report").status_code == 403


def test_a_rotated_card_or_deactivated_operator_cannot_submit(om: Mentors) -> None:
    s, uid, token, host = setup(om)
    om.clock.now = baku("17:45")
    om.rig.sql("UPDATE contact_cards SET revoked_at = now() WHERE user_id = :u", u=uid)
    refused = submit(host, s, token, "15:52")
    assert refused.status_code == 404 and refused.json()["error"]["code"] == "INVALID_QR"
    host_id = int(
        om.rig.scalar("SELECT staff_account_id FROM staff_scopes WHERE scope = 'MENTOR_HOST'")
    )
    om.rig.sql("UPDATE staff_accounts SET is_active = false WHERE id = :h", h=host_id)
    deactivated = submit(host, s, om.card(uid), "15:53")
    assert deactivated.status_code == 403
    assert deactivated.json()["error"]["code"] == "STAFF_NOT_AUTHORIZED"
    assert om.rig.scalar("SELECT count(*) FROM offline_scans") == 0
    assert om.mentor_points(uid) == 0


def test_a_scan_dated_after_the_close_never_pays_even_if_approved(om: Mentors) -> None:
    s, uid, token, host = setup(om, start="17:15")  # window 17:30-17:45
    om.clock.now = baku("17:50")
    queued = submit(host, s, token, "17:31")
    assert queued.json()["status"] == "pending_review"
    gm = game_master(om)
    r = approve(gm, queued.json()["id"], "Phone says 17:31.", "17:31")
    assert r.json()["status"] == "failed" and r.json()["outcome"] == {"reason": "after_close"}
    assert om.mentor_points(uid) == 0 and om.statuses(s) == ["CONFIRMED"]


def test_an_attendee_report_never_shows_another_persons_day(om: Mentors) -> None:
    s, uid, token, host = setup(om)
    other = om.users(1)[0]
    om.clock.now = baku("15:52")
    submit(host, s, token, "15:52")
    theirs = om.client(other).get("/api/attendees/me/report").json()
    assert theirs["attended"] == [] and theirs["total"] == 0
    assert om.client(other).get("/api/staff/booth/report").status_code == 401  # not staff


# --- review authority (independent audit 2026-10-17, findings 2-4) -----------------------


def _late(om: Mentors) -> tuple[int, int, str, TestClient, int]:
    s, uid, token, host = setup(om)
    om.clock.now = baku("17:45")
    queued = submit(host, s, token, "15:52")
    assert queued.json()["status"] == "pending_review"
    return s, uid, token, host, int(queued.json()["id"])


def _audited(om: Mentors, scan_id: int) -> list[Any]:
    return [
        r[0]
        for r in om.rig.sql(
            "SELECT after_value FROM admin_audit WHERE action = 'offline_scan.approve' "
            "AND target_id = :t",
            t=str(scan_id),
        )
    ]


def test_a_card_revoked_before_the_review_never_pays(om: Mentors) -> None:
    s, uid, token, host, scan = _late(om)
    om.rig.sql("UPDATE contact_cards SET revoked_at = now() WHERE public_token = :t", t=token)
    r = approve(game_master(om), scan, "The host's list shows 15:52.", "15:52")
    assert r.status_code == 200
    assert r.json()["status"] == "failed" and r.json()["outcome"] == {"reason": "card_revoked"}
    assert om.mentor_points(uid) == 0 and om.statuses(s) == ["CONFIRMED"]
    [after] = _audited(om, scan)
    assert after["outcome"] == "card_revoked" and after["reason"] == "The host's list shows 15:52."
    # The QR secret itself was never stored with the scan.
    assert token not in str(om.rig.sql("SELECT * FROM offline_scans"))


def test_the_scanner_never_reviews_their_own_scan_even_as_game_master(om: Mentors) -> None:
    s, uid, token, host, scan = _late(om)
    scanner = int(om.rig.scalar("SELECT staff_account_id FROM offline_scans WHERE id = :i", i=scan))
    om.rig.sql(
        "INSERT INTO staff_scopes (staff_account_id, scope, granted_by_staff_id) "
        "VALUES (:s, 'GAME_MASTER', :a)",
        s=scanner,
        a=om.rig.staff_id(StaffRole.ADMIN),
    )
    for verb in ("approve", "reject"):
        r = host.post(
            f"/api/staff/offline-scans/{scan}/{verb}",
            json={"reason": "my own scan", "verified_at": baku("15:52").isoformat()}
            if verb == "approve"
            else {"reason": "my own scan"},
        )
        assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "self_review"
    assert om.rig.scalar("SELECT status FROM offline_scans WHERE id = :i", i=scan) == (
        "PENDING_REVIEW"
    )
    assert om.mentor_points(uid) == 0
    queue = host.get("/api/staff/offline-scans").json()["scans"]
    assert queue[0]["own_scan"] is True
    # Another Game Master can.
    assert approve(game_master(om), scan, "Paper list: 15:52.", "15:52").json()["status"] == (
        "applied"
    )


def test_a_revoked_scope_or_account_is_a_recorded_refusal_without_side_effects(
    om: Mentors,
) -> None:
    s, uid, token, host, scan = _late(om)
    om.rig.sql("UPDATE staff_scopes SET revoked_at = now() WHERE scope = 'MENTOR_HOST'")
    r = approve(game_master(om), scan, "Paper list: 15:52.", "15:52")
    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    assert r.json()["outcome"] == {"reason": "scanner_not_authorized"}
    assert om.mentor_points(uid) == 0 and om.statuses(s) == ["CONFIRMED"]
    assert _audited(om, scan)[0]["outcome"] == "scanner_not_authorized"

    s2, uid2, token2, host2, scan2 = _late(om)
    host_id = int(
        om.rig.scalar("SELECT staff_account_id FROM offline_scans WHERE id = :i", i=scan2)
    )
    om.rig.sql("UPDATE staff_accounts SET is_active = false WHERE id = :h", h=host_id)
    r2 = approve(game_master(om), scan2, "Paper list: 15:52.", "15:52")
    assert r2.json()["outcome"] == {"reason": "scanner_inactive"}
    assert om.mentor_points(uid2) == 0


def test_a_repeated_decision_and_a_verified_time_after_receipt(om: Mentors) -> None:
    s, uid, token, host, scan = _late(om)
    gm = game_master(om)
    late = approve(gm, scan, "Paper list.", "17:50")  # after the server received it (17:45)
    assert late.status_code == 409
    assert late.json()["error"]["details"]["reason"] == "verified_after_received"
    first = approve(gm, scan, "Paper list: 15:52.", "15:52")
    second = approve(gm, scan, "Paper list: 15:52.", "15:52")
    assert first.json() == second.json() and om.mentor_points(uid) == 5
    assert len(_audited(om, scan)) == 1


def test_a_scan_from_before_card_evidence_needs_the_card_rescanned(om: Mentors) -> None:
    s, uid, token, host, scan = _late(om)
    om.rig.sql("UPDATE offline_scans SET card_id = NULL WHERE id = :i", i=scan)  # pre-0027 row
    gm = game_master(om)
    missing = approve(gm, scan, "Paper list: 15:52.", "15:52")
    assert missing.status_code == 409
    assert missing.json()["error"]["details"]["reason"] == "card_evidence_required"
    other = om.users(1)[0]
    wrong = approve(gm, scan, "Paper list: 15:52.", "15:52", card_token=om.card(other))
    assert wrong.json()["error"]["details"]["reason"] == "card_mismatch"
    assert om.rig.scalar("SELECT status FROM offline_scans WHERE id = :i", i=scan) == (
        "PENDING_REVIEW"
    )
    ok = approve(gm, scan, "Re-scanned the card; list 15:52.", "15:52", card_token=token)
    assert ok.json()["status"] == "applied" and om.mentor_points(uid) == 5


def test_a_door_scan_review_rechecks_the_card_too(om: Mentors) -> None:
    mc = om.masterclass(om.b2)
    uid = om.users(1)[0]
    c = om.client(uid)
    from tests.test_masterclasses import choose

    assert choose(c, om.b2, mc).status_code == 200
    token = om.card(uid)
    door = int(
        om.rig.scalar(
            "INSERT INTO staff_accounts (email, password_hash, full_name, role, is_active) "
            "VALUES (:e, 'x', 'Door', 'VOLUNTEER', true) RETURNING id",
            e=f"door-{uuid4().hex[:8]}@staff.test",
        )
    )
    om.rig.sql(
        "INSERT INTO staff_scopes (staff_account_id, scope, target_id, granted_by_staff_id) "
        "VALUES (:s, 'MASTERCLASS_DOOR', :t, :a)",
        s=door,
        t=mc,
        a=om.rig.staff_id(StaffRole.ADMIN),
    )
    dc = TestClient(app, headers={"Origin": get_settings().app_origin})
    dc.cookies.set("session", om.rig.session_for(door))
    om.clients.append(dc)
    om.clock.now = baku("17:45")
    queued = dc.post(
        "/api/staff/offline-scans",
        json={
            "client_scan_id": str(uuid4()),
            "kind": "MASTERCLASS_DOOR",
            "target_id": mc,
            "phase": "ENTRY",
            "card_token": token,
            "scanned_at": baku("16:03").isoformat(),
            **filed_under(dc),
        },
    )
    assert queued.json()["status"] == "pending_review", queued.text
    om.rig.sql("UPDATE contact_cards SET revoked_at = now() WHERE public_token = :t", t=token)
    r = approve(game_master(om), int(queued.json()["id"]), "Door list: 16:03.", "16:03")
    assert r.json()["outcome"] == {"reason": "card_revoked"}
    assert om.rig.scalar("SELECT count(*) FROM masterclass_attendance") == 0


def test_a_queue_filed_under_another_account_or_dataset_is_refused(om: Mentors) -> None:
    s, uid, token, host = setup(om)
    om.clock.now = baku("15:52")
    _, other_host = om.host(s)  # a second host of the same session
    theirs = filed_under(other_host)
    r = submit(host, s, token, "15:52", filed=theirs)  # the queue of the other account
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "queue_owner_mismatch"
    mine = filed_under(host)
    before_reset = {**mine, "dataset_generation": mine["dataset_generation"] - 1 or 99}
    r2 = submit(host, s, token, "15:52", filed=before_reset)
    assert r2.status_code == 409 and r2.json()["error"]["details"]["reason"] == "dataset_changed"
    assert om.rig.scalar("SELECT count(*) FROM offline_scans") == 0
    assert om.mentor_points(uid) == 0
    assert submit(host, s, token, "15:52").json()["status"] == "applied"  # its own queue


def test_a_page_of_the_first_queue_version_is_told_to_reload_not_refused(om: Mentors) -> None:
    """Rollout: a staff tab opened before this release still posts its queue without
    an owner label. It must get a TEMPORARY answer (that page keeps the scan and
    retries; a reload brings the current page, which quarantines it), never a
    final refusal that page would delete the scan for."""
    s, uid, token, host = setup(om)
    om.clock.now = baku("15:52")
    old_payload = {
        "client_scan_id": str(uuid4()),
        "kind": "MENTOR_COMPLETE",
        "target_id": s,
        "card_token": token,
        "scanned_at": baku("15:52").isoformat(),
    }
    r = host.post("/api/staff/offline-scans", json=old_payload)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "CLIENT_UPDATE_REQUIRED"
    assert r.headers["retry-after"] == "60"
    assert om.rig.scalar("SELECT count(*) FROM offline_scans") == 0
    assert om.mentor_points(uid) == 0


# --- the tie-break (PDF conformance audit 2026-10-18, P1) --------------------------------


def test_equal_points_are_ordered_by_when_the_activity_happened_not_when_it_was_reviewed(
    om: Mentors,
) -> None:
    """PDF: on equal points whoever reached them earlier is ahead. A's activity
    happened first but its offline scan was approved after the close; B's was
    recorded at once. The live board, its pages and the final snapshot must all
    put A first."""
    from sqlalchemy.orm import Session

    from app.modules.competition.final import build_payload
    from app.modules.leaderboard.service import invalidate_ranking_snapshot

    early, late = om.session("13:35"), om.session("15:35")
    a, b = om.users(2)
    book(om.client(a), early)
    book(om.client(b), late)
    _, host_a = om.host(early)
    _, host_b = om.host(late)

    om.clock.now = baku("15:52")  # B: scanned on time and applied at once
    assert submit(host_b, late, om.card(b), "15:51").json()["status"] == "applied"

    om.clock.now = baku("17:45")  # A: the phone synchronised only after the close
    queued = submit(host_a, early, om.card(a), "13:51")
    assert queued.json()["status"] == "pending_review"
    approved = approve(game_master(om), queued.json()["id"], "Paper list: 13:51.", "13:51")
    assert approved.json()["status"] == "applied"
    assert om.mentor_points(a) == om.mentor_points(b) == 5

    om.rig.sql("UPDATE event_config SET competition_mode = true")
    invalidate_ranking_snapshot()
    try:
        viewer = om.client(a)
        top = [e["user_id"] for e in viewer.get("/api/leaderboard").json()["entries"]]
        assert [u for u in top if u in (a, b)] == [a, b]
        paged: list[int] = []
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {"view": "all", "limit": 1}
            if cursor:
                params["cursor"] = cursor
            page = viewer.get("/api/leaderboard", params=params).json()
            paged += [e["user_id"] for e in page["entries"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert [u for u in paged if u in (a, b)] == [a, b]
        with Session(om.rig.engine) as db:
            ranking = build_payload(db)["ranking"]
        final = [(e["user_id"], e["reached_at"]) for e in ranking if e["user_id"] in (a, b)]
        # A by the verified scan; B, applied at once, by the server's own clock.
        assert final == [(a, baku("13:51").isoformat()), (b, baku("15:52").isoformat())]
    finally:
        om.rig.sql("UPDATE event_config SET competition_mode = false")
        invalidate_ranking_snapshot()
