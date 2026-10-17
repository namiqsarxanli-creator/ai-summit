"""Beyond Now Founder (+10 for all four zones) and the versioned final result.

Rig database (disposable, freshly seeded); server clocks replaced per test.
Awards go through the real ledger (`competition.ledger.award`), the one writer
every scan uses. Times are Asia/Baku (UTC+4) on 13 Nov 2026.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.main import app
from app.modules.competition import final_routers, ledger
from app.modules.competition import routers as comp_routers
from app.modules.points.models import PointsSource
from app.modules.staff.models import StaffRole
from tests.api_rig import Rig
from tests.test_masterclasses import Program


def baku(hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 11, 13, h - 4, m, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = baku("12:00")

    def __call__(self) -> datetime:
        return self.now


class Final(Program):
    clock: Clock

    def pay(self, uid: int, source: PointsSource, unit: str, points: int, at: str = "12:00") -> str:
        with Session(self.rig.engine) as db:
            result = ledger.award(
                db, user_id=uid, source=source, unit=unit, points=points, occurred_at=baku(at)
            )
            db.commit()
            return result.outcome

    def all_zones(self, uid: int) -> None:
        self.pay(uid, PointsSource.PANEL, "panel:1", 8)
        self.pay(uid, PointsSource.MASTERCLASS, "block:1", 12)
        self.pay(uid, PointsSource.PLAYGROUND, "booth:1", 3)
        self.pay(uid, PointsSource.MENTOR, "once", 5)

    def founder_points(self, uid: int) -> int:
        return int(
            self.rig.scalar(
                "SELECT coalesce(sum(points), 0) FROM points_log "
                "WHERE user_id = :u AND source = 'FOUNDER'",
                u=uid,
            )
        )

    def entry(self, uid: int, source: str) -> int:
        return int(
            self.rig.scalar(
                "SELECT id FROM points_log WHERE user_id = :u AND source = :s "
                "AND entry_type = 'AWARD' ORDER BY id LIMIT 1",
                u=uid,
                s=source,
            )
        )

    def game_master(self) -> TestClient:
        sid = int(
            self.rig.scalar(
                "INSERT INTO staff_accounts (email, password_hash, full_name, role, is_active) "
                "VALUES (:e, 'x', 'Game Master', 'VOLUNTEER', true) RETURNING id",
                e=f"gm-{uuid4().hex[:8]}@staff.test",
            )
        )
        self.rig.sql(
            "INSERT INTO staff_scopes (staff_account_id, scope, granted_by_staff_id) "
            "VALUES (:s, 'GAME_MASTER', :a)",
            s=sid,
            a=self.rig.staff_id(StaffRole.ADMIN),
        )
        return self.staff_client(sid)

    def staff_client(self, sid: int) -> TestClient:
        c = TestClient(app, headers={"Origin": get_settings().app_origin})
        c.cookies.set("session", self.rig.session_for(sid))
        self.clients.append(c)
        return c


def rule(monkeypatch: pytest.MonkeyPatch, *, confirmed: bool) -> None:
    monkeypatch.setenv("FOUNDER_RULE_CONFIRMED", "true" if confirmed else "false")
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    yield
    get_settings.cache_clear()  # monkeypatch has restored the environment by now


@pytest.fixture
def fin(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Iterator[Final]:
    clock = Clock()
    monkeypatch.setattr(final_routers, "_now", clock)
    monkeypatch.setattr(comp_routers, "_now", clock)
    # These tests exercise the PROVISIONAL Founder rule as designed; the gate that
    # keeps it off until the organiser confirms it is tested on its own below.
    rule(monkeypatch, confirmed=True)
    p = Final(rig)
    p.clock = clock
    rig.sql("UPDATE masterclass_events SET competition_closes_at = :c", c=baku("17:30"))
    rig.sql("UPDATE event_config SET competition_mode = true")
    yield p
    for c in p.clients:
        c.close()


def reverse(rig: Rig, entry_id: int) -> int:
    rig.sign_in_as(rig.staff_id(StaffRole.ADMIN))
    r = rig.client.post(
        f"/api/admin/points-log/{entry_id}/reverse",
        json={"reason": "Scanned by mistake.", "idempotency_key": str(uuid4())},
    )
    return int(r.status_code)


# --- Founder ------------------------------------------------------------------------------


def test_founder_pays_ten_once_when_the_fourth_zone_is_reached(fin: Final) -> None:
    uid = fin.users(1)[0]
    fin.pay(uid, PointsSource.PANEL, "panel:1", 8)
    fin.pay(uid, PointsSource.QMETER, "panel:1", 2)  # a second Main Stage activity
    fin.pay(uid, PointsSource.MASTERCLASS, "block:1", 12)
    fin.pay(uid, PointsSource.PLAYGROUND, "booth:1", 3)
    fin.pay(uid, PointsSource.PROFILE, "profile", 5)  # not a fifth zone
    assert fin.founder_points(uid) == 0
    fin.pay(uid, PointsSource.CONNECTION, "pair:1:2", 1)
    assert fin.founder_points(uid) == 10
    fin.pay(uid, PointsSource.MENTOR, "once", 5)  # more Connection Hub: no second bonus
    assert fin.founder_points(uid) == 10
    mine = fin.client(uid).get("/api/attendees/me/competition").json()
    assert mine["zones"] == {
        "main_stage": True,
        "masterclass": True,
        "playground": True,
        "connection_hub": True,
    }
    assert mine["founder"] is True and mine["lucky_draw"] is True
    assert next(c for c in mine["categories"] if c["category"] == "FOUNDER")["points"] == 10


def test_a_correction_removes_founder_and_new_evidence_restores_it(fin: Final) -> None:
    uid = fin.users(1)[0]
    fin.all_zones(uid)
    assert fin.founder_points(uid) == 10
    # The only masterclass was scanned by mistake: reversing it reverses Founder too.
    assert reverse(fin.rig, fin.entry(uid, "MASTERCLASS")) == 200
    assert fin.founder_points(uid) == 0
    assert (
        fin.rig.scalar(
            "SELECT count(*) FROM points_log WHERE user_id = :u AND source = 'FOUNDER' "
            "AND entry_type = 'REVERSAL'",
            u=uid,
        )
        == 1
    )
    mine = fin.client(uid).get("/api/attendees/me/competition").json()
    assert mine["zones"]["masterclass"] is False and mine["lucky_draw"] is False
    # Block 2 is verified later: Founder is earned again, as a new ledger entry.
    fin.pay(uid, PointsSource.MASTERCLASS, "block:2", 12)
    assert fin.founder_points(uid) == 10
    assert fin.rig.scalar("SELECT count(*) FROM points_log WHERE source = 'FOUNDER'") == 3


def test_the_founder_bonus_itself_cannot_be_reversed_by_hand(fin: Final) -> None:
    uid = fin.users(1)[0]
    fin.all_zones(uid)
    assert reverse(fin.rig, fin.entry(uid, "FOUNDER")) == 409
    assert fin.founder_points(uid) == 10


def test_a_zone_completed_after_the_close_earns_no_founder(fin: Final) -> None:
    uid = fin.users(1)[0]
    fin.pay(uid, PointsSource.PANEL, "panel:1", 8)
    fin.pay(uid, PointsSource.MASTERCLASS, "block:1", 12)
    fin.pay(uid, PointsSource.PLAYGROUND, "booth:1", 3)
    # The mentor scan itself is after the close: no award, so no fourth zone.
    assert fin.pay(uid, PointsSource.MENTOR, "once", 5, at="17:30") == "closed"
    assert fin.founder_points(uid) == 0


def test_an_excluded_person_is_not_on_the_lucky_draw_list(fin: Final) -> None:
    uid = fin.users(1)[0]
    fin.all_zones(uid)
    fin.rig.sql("UPDATE users SET competition_excluded_reason = 'Volunteer' WHERE id = :u", u=uid)
    mine = fin.client(uid).get("/api/attendees/me/competition").json()
    assert mine["founder"] is True and mine["lucky_draw"] is False


# --- the final result ---------------------------------------------------------------------


def test_no_snapshot_before_the_close_and_only_for_a_game_master(fin: Final) -> None:
    gm = fin.game_master()
    r = gm.post("/api/staff/final")
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "competition_open"
    assert TestClient(app).get("/api/final").json()["status"] == "open"
    fin.clock.now = baku("17:31")
    booth_staff = fin.staff_client(fin.rig.staff_id(StaffRole.BOOTH_STAFF))
    assert booth_staff.post("/api/staff/final").status_code == 403
    assert booth_staff.get("/api/staff/final").status_code == 403
    assert TestClient(app).get("/api/final").json()["status"] == "pending_review"


def test_review_publish_and_a_new_version_never_rewrites_the_published_one(fin: Final) -> None:
    a, b, c = fin.users(3)
    fin.rig.sql("UPDATE users SET first_name = 'Aysel', last_name = 'Məmmədova' WHERE id = :u", u=a)
    fin.all_zones(a)  # 28 + Founder 10 = 38
    fin.pay(b, PointsSource.PANEL, "panel:1", 8)
    fin.clock.now = baku("17:35")
    gm = fin.game_master()
    draft = gm.post("/api/staff/final")
    assert draft.status_code == 201, draft.text
    v1 = draft.json()
    assert v1["version"] == 1 and v1["status"] == "draft"
    review = gm.get(f"/api/staff/final/{v1['id']}").json()
    assert [e["name"] for e in review["ranking"][:2]] == ["Aysel M.", "Guest N."]
    assert review["ranking"][0]["points"] == 38
    assert [f["user_id"] for f in review["founder_list"]] == [a] and review["founders"] == 1
    assert TestClient(app).get("/api/final").json()["status"] == "pending_review"

    published = gm.post(f"/api/staff/final/{v1['id']}/publish")
    assert published.json()["status"] == "published"
    assert gm.post(f"/api/staff/final/{v1['id']}/publish").json()["status"] == "published"
    screen = TestClient(app).get("/api/final")
    body = screen.json()
    assert body["status"] == "published" and body["version"] == 1
    assert body["top"][0] == {"name": "Aysel M.", "photo_url": None, "place": 1, "points": 38}
    assert body["founders"] == [{"name": "Aysel M.", "photo_url": None}]
    assert "@" not in screen.text and "Məmmədova" not in screen.text
    assert fin.client(a).get("/api/attendees/me/competition").json()["final"] == {
        "version": 1,
        "place": 1,
        "points": 38,
        "founder": True,
    }

    # A verified pre-close correction arrives after publication.
    fin.pay(c, PointsSource.PANEL, "panel:2", 8, at="15:20")
    assert gm.get(f"/api/staff/final/{v1['id']}").json()["ledger_changed_since"] is True
    assert TestClient(app).get("/api/final").json()["top"][1]["name"] == "Guest N."
    v2 = gm.post("/api/staff/final").json()
    assert v2["version"] == 2
    gm.post(f"/api/staff/final/{v2['id']}/publish")
    statuses = dict(fin.rig.sql("SELECT version, status FROM final_results ORDER BY version"))
    assert statuses == {1: "SUPERSEDED", 2: "PUBLISHED"}
    assert TestClient(app).get("/api/final").json()["version"] == 2
    # The superseded snapshot keeps exactly what was shown.
    assert len(gm.get(f"/api/staff/final/{v1['id']}").json()["ranking"]) == len(review["ranking"])

    actions = [
        str(r[0])
        for r in fin.rig.sql(
            "SELECT action FROM admin_audit WHERE target_type = 'final_result' ORDER BY id"
        )
    ]
    assert actions == ["final.create", "final.publish", "final.create", "final.publish"]


def test_the_database_keeps_a_published_result_permanent(fin: Final) -> None:
    fin.clock.now = baku("17:35")
    gm = fin.game_master()
    row = gm.post("/api/staff/final").json()
    gm.post(f"/api/staff/final/{row['id']}/publish")
    for statement in (
        "UPDATE final_results SET payload = '{}'::jsonb WHERE id = :i",
        "UPDATE final_results SET status = 'DRAFT', published_at = NULL, "
        "published_by_staff_id = NULL WHERE id = :i",
        "DELETE FROM final_results WHERE id = :i",
    ):
        with pytest.raises(IntegrityError) as caught:
            fin.rig.sql(statement, i=row["id"])
        assert caught.value.orig.diag.constraint_name == "ck_final_results_immutable"  # type: ignore[union-attr]


def test_a_draft_can_be_discarded_and_not_published_afterwards(fin: Final) -> None:
    fin.clock.now = baku("17:35")
    gm = fin.game_master()
    row = gm.post("/api/staff/final").json()
    assert gm.post(f"/api/staff/final/{row['id']}/discard").json()["status"] == "discarded"
    r = gm.post(f"/api/staff/final/{row['id']}/publish")
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "not_a_draft"


def test_on_equal_points_whoever_reached_the_total_first_ranks_ahead(fin: Final) -> None:
    early, late = fin.users(2)
    fin.pay(late, PointsSource.PANEL, "panel:9", 8)
    fin.rig.sql("UPDATE points_log SET created_at = :t WHERE user_id = :u", t=baku("14:10"), u=late)
    fin.pay(early, PointsSource.PANEL, "panel:9", 8)
    fin.rig.sql(
        "UPDATE points_log SET created_at = :t WHERE user_id = :u", t=baku("11:00"), u=early
    )
    fin.clock.now = baku("17:35")
    gm = fin.game_master()
    row = gm.post("/api/staff/final").json()
    ranking = gm.get(f"/api/staff/final/{row['id']}").json()["ranking"]
    top = [(e["user_id"], e["place"]) for e in ranking[:2]]
    assert top == [(early, 1), (late, 2)]  # own places, never a shared rank


def test_the_lucky_draw_list_is_exported_from_the_snapshot_with_audit(fin: Final) -> None:
    a = fin.users(1)[0]
    fin.rig.sql("UPDATE users SET first_name = '=cmd', last_name = 'X' WHERE id = :u", u=a)
    fin.all_zones(a)
    fin.clock.now = baku("17:35")
    gm = fin.game_master()
    row = gm.post("/api/staff/final").json()
    r = gm.get(f"/api/staff/final/{row['id']}/founders.csv")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    lines = r.text.strip().splitlines()
    assert lines[0] == "user_id,name,founder_at"
    assert lines[1].startswith(f"{a},'=cmd X.,")  # spreadsheet formula neutralised
    assert fin.rig.scalar("SELECT count(*) FROM admin_audit WHERE action = 'final.export'") == 1


# --- the gate: the Founder rule is not confirmed by the organiser ------------------------


def test_an_unconfirmed_founder_rule_awards_nothing_and_names_no_one(
    fin: Final, monkeypatch: pytest.MonkeyPatch
) -> None:
    rule(monkeypatch, confirmed=False)
    uid = fin.users(1)[0]
    fin.all_zones(uid)
    assert fin.founder_points(uid) == 0  # no automatic bonus while unconfirmed
    mine = fin.client(uid).get("/api/attendees/me/competition").json()
    assert all(mine["zones"].values())  # the evidence itself is still shown
    assert mine["founder_rule_confirmed"] is False
    assert mine["founder"] is False and mine["lucky_draw"] is False
    fin.clock.now = baku("17:35")
    gm = fin.game_master()
    row = gm.post("/api/staff/final").json()
    assert row["founders"] == 0 and row["founder_rule_confirmed"] is False
    gm.post(f"/api/staff/final/{row['id']}/publish")
    public = TestClient(app).get("/api/final").json()
    assert public["founders"] == [] and public["founder_rule_confirmed"] is False
    assert public["top"][0]["points"] == 28  # the competition itself is unaffected
    csv = gm.get(f"/api/staff/final/{row['id']}/founders.csv")
    assert csv.status_code == 409
    assert csv.json()["error"]["details"]["reason"] == "founder_rule_unconfirmed"


def test_a_founder_bonus_awarded_before_the_gate_is_not_shown_while_gated(
    fin: Final, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = fin.users(1)[0]
    fin.all_zones(uid)  # confirmed (fixture): the bonus exists in the ledger
    assert fin.founder_points(uid) == 10
    rule(monkeypatch, confirmed=False)
    mine = fin.client(uid).get("/api/attendees/me/competition").json()
    assert mine["founder"] is False and mine["lucky_draw"] is False
    fin.clock.now = baku("17:35")
    gm = fin.game_master()
    row = gm.post("/api/staff/final").json()
    assert row["founders"] == 0  # a snapshot taken while gated names no Founder


# --- the server enforces readiness (independent audit 2026-10-17, findings 5-6) -----------


def _pending_offline(fin: Final, uid: int) -> int:
    staff = fin.rig.staff_id(StaffRole.ADMIN)
    return int(
        fin.rig.scalar(
            "INSERT INTO offline_scans (staff_account_id, client_scan_id, kind, target_id, "
            "user_id, device_scanned_at, status) VALUES (:s, :c, 'MENTOR_COMPLETE', 1, :u, "
            ":t, 'PENDING_REVIEW') RETURNING id",
            s=staff,
            c=str(uuid4()),
            u=uid,
            t=baku("15:52"),
        )
    )


def test_a_draft_the_ledger_has_moved_past_cannot_be_published(fin: Final) -> None:
    uid = fin.users(1)[0]
    fin.clock.now = baku("17:40")
    gm = fin.game_master()
    draft = gm.post("/api/staff/final").json()
    # A verified pre-close activity is recorded after the draft was taken.
    assert fin.pay(uid, PointsSource.PANEL, "panel:7", 8, "12:00") == "awarded"
    assert gm.get(f"/api/staff/final/{draft['id']}").json()["ledger_changed_since"] is True
    refused = gm.post(f"/api/staff/final/{draft['id']}/publish")
    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["reason"] == "stale_snapshot"
    assert fin.rig.scalar("SELECT count(*) FROM final_results WHERE status = 'PUBLISHED'") == 0
    # The way forward: a new version, which includes it.
    fresh = gm.post("/api/staff/final").json()
    assert gm.post(f"/api/staff/final/{fresh['id']}/publish").json()["status"] == "published"
    top = TestClient(app).get("/api/final").json()["top"]
    assert any(p["points"] == 8 for p in top)
    # Publishing again is a no-op; the refused draft is still a draft, unchanged.
    assert gm.post(f"/api/staff/final/{fresh['id']}/publish").json()["status"] == "published"
    assert fin.rig.scalar("SELECT status FROM final_results WHERE id = :i", i=draft["id"]) == (
        "DRAFT"
    )


def test_unresolved_offline_reviews_block_publication(fin: Final) -> None:
    uid = fin.users(1)[0]
    fin.clock.now = baku("17:40")
    scan = _pending_offline(fin, uid)
    gm = fin.game_master()
    draft = gm.post("/api/staff/final").json()
    assert gm.get(f"/api/staff/final/{draft['id']}").json()["pending_offline_scans"] == 1
    refused = gm.post(f"/api/staff/final/{draft['id']}/publish")
    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["reason"] == "offline_reviews_pending"
    fin.rig.sql(
        "UPDATE offline_scans SET status = 'REJECTED', review_reason = 'not seen' WHERE id = :i",
        i=scan,
    )
    assert gm.post(f"/api/staff/final/{draft['id']}/publish").json()["status"] == "published"


def _wait_until_blocked(fin: Final, pattern: str) -> None:
    """Deterministic interleaving: return once a session waits on a lock running
    `pattern` (bounded: a few seconds at most)."""
    import time

    for _ in range(100):
        waiting = fin.rig.scalar(
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
            "AND query LIKE :p",
            p=pattern,
        )
        if waiting:
            return
        time.sleep(0.02)
    raise AssertionError("the publication never waited for the concurrent writer")


@pytest.mark.parametrize("writer", ["award", "offline_submission"])
def test_a_write_in_flight_is_seen_by_the_publication_not_raced(fin: Final, writer: str) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import text

    uid = fin.users(1)[0]
    fin.clock.now = baku("17:40")
    gm = fin.game_master()
    draft = gm.post("/api/staff/final").json()
    with fin.rig.engine.connect() as other:
        tx = other.begin()
        if writer == "award":
            other.execute(
                text(
                    "INSERT INTO points_log (user_id, source, entry_type, points, "
                    "entered_by_system, dedupe_key) VALUES (:u, 'PANEL', 'AWARD', 8, true, :k)"
                ),
                {"u": uid, "k": f"COMP:PANEL:{uid}:race"},
            )
        else:
            other.execute(
                text(
                    "INSERT INTO offline_scans (staff_account_id, client_scan_id, kind, "
                    "target_id, user_id, device_scanned_at, status) VALUES (:s, :c, "
                    "'MENTOR_COMPLETE', 1, :u, now(), 'PENDING_REVIEW')"
                ),
                {"s": fin.rig.staff_id(StaffRole.ADMIN), "c": str(uuid4()), "u": uid},
            )
        with ThreadPoolExecutor(max_workers=1) as pool:
            publishing = pool.submit(gm.post, f"/api/staff/final/{draft['id']}/publish")
            _wait_until_blocked(fin, "LOCK TABLE users, offline_scans, points_log%")
            tx.commit()  # the concurrent write lands while the publication waits
            result = publishing.result(timeout=30)
    expected = "stale_snapshot" if writer == "award" else "offline_reviews_pending"
    assert result.status_code == 409, result.text
    assert result.json()["error"]["details"]["reason"] == expected
    assert fin.rig.scalar("SELECT count(*) FROM final_results WHERE status = 'PUBLISHED'") == 0


# --- Founder judges the verified occurrence, not the recording time ---------------------


def test_activity_verified_late_but_done_before_the_close_completes_founder(fin: Final) -> None:
    from datetime import timedelta

    # The close has just passed; the four activities happened before it, and are
    # recorded (created_at) only now - like reviewed offline scans.
    now = datetime.now(UTC)
    fin.rig.sql(
        "UPDATE masterclass_events SET competition_closes_at = :c", c=now - timedelta(minutes=5)
    )
    uid = fin.users(1)[0]
    before = now - timedelta(hours=1)
    with Session(fin.rig.engine) as db:
        for source, unit, points in (
            (PointsSource.PANEL, "panel:1", 8),
            (PointsSource.MASTERCLASS, "block:1", 12),
            (PointsSource.PLAYGROUND, "booth:1", 3),
            (PointsSource.MENTOR, "once", 5),
        ):
            assert (
                ledger.award(
                    db, user_id=uid, source=source, unit=unit, points=points, occurred_at=before
                ).outcome
                == "awarded"
            )
        db.commit()
    assert fin.founder_points(uid) == 10
    occurred = fin.rig.scalar(
        "SELECT occurred_at FROM points_log WHERE user_id = :u AND source = 'FOUNDER'", u=uid
    )
    assert occurred == before  # dated by the fourth zone's activity


def test_historical_rows_without_an_occurrence_are_read_by_their_recording_time(
    fin: Final,
) -> None:
    from datetime import timedelta

    from app.modules.competition.founder import zones

    uid = fin.users(1)[0]
    fin.all_zones(uid)
    fin.rig.sql("UPDATE points_log SET occurred_at = NULL WHERE user_id = :u", u=uid)  # pre-0027
    with Session(fin.rig.engine) as db:
        reached = zones(db, uid)
    created = fin.rig.scalar(
        "SELECT min(created_at) FROM points_log WHERE user_id = :u AND source = 'PANEL'", u=uid
    )
    assert reached["main_stage"] == created
    assert all(
        abs((t - created).total_seconds()) < timedelta(minutes=5).total_seconds()
        for t in reached.values()
        if t
    )


def test_after_confirmation_the_recomputation_reaches_people_with_no_new_award(
    fin: Final, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.competition.founder import recompute_all

    rule(monkeypatch, confirmed=False)
    uid = fin.users(1)[0]
    fin.all_zones(uid)  # completed while the rule was unconfirmed: no bonus
    assert fin.founder_points(uid) == 0
    rule(monkeypatch, confirmed=True)
    with Session(fin.rig.engine) as db:
        dry = recompute_all(db, apply=False)
        db.rollback()
    assert dry["would_award"] == 1 and dry["awarded"] == 0
    assert fin.founder_points(uid) == 0  # a dry run writes nothing
    with Session(fin.rig.engine) as db:
        done = recompute_all(db, apply=True)
        db.commit()
    assert done["awarded"] == 1 and fin.founder_points(uid) == 10
    with Session(fin.rig.engine) as db:
        assert recompute_all(db, apply=True)["unchanged"] >= 1  # idempotent
        db.commit()
    assert fin.founder_points(uid) == 10


def test_the_recomputation_command_refuses_while_the_rule_is_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import main

    rule(monkeypatch, confirmed=False)
    with pytest.raises(SystemExit, match="FOUNDER_RULE_CONFIRMED is false"):
        main(["founder-recompute", "--confirm"])
