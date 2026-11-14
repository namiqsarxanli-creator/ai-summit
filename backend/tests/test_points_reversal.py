"""Points reversal by an administrator (`POST /api/admin/points-log/{id}/reverse`,
app/modules/points/reversal.py).

Runs against the disposable reset-test database (a scratch database created by
this run only, see tests/suite_db.py). Awards are planted with SQL where a test
only needs a ledger row, and made through the REAL award endpoint where a test
is about what happens to later awards.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.errors import DomainError
from app.core.security import hash_session_token
from app.db.session import mutating_transaction
from app.modules.admin import audit as audit_view
from app.modules.admin import export_status, exports
from app.modules.admin.export_tickets import tickets
from app.modules.points import reversal as reversal_module
from app.modules.points.reversal import reverse_entry
from app.modules.points.schemas import ReversalRequest
from app.modules.points.service import compute_rule_revision
from app.modules.staff.models import StaffRole
from app.modules.staff.scan_ref import issue_scan_ref
from tests.api_rig import Rig

REASON = "Scanned the wrong attendee at the booth"
AWARD = "/api/staff/points/award"
ELENA_TOKEN = "YGNasg-eVR42rUp_bKbockSk2RLE26A2g9bfCUHvXj8"  # a seeded demo attendee card


def _reverse(
    rig: Rig, entry_id: int, *, reason: str = REASON, key: str | None = None, **extra: Any
) -> Any:
    return rig.client.post(
        f"/api/admin/points-log/{entry_id}/reverse",
        json={"reason": reason, "idempotency_key": key or str(uuid.uuid4()), **extra},
    )


def _error(response: Any) -> tuple[int, str]:
    return int(response.status_code), str(response.json()["error"]["code"])


class Case:
    """One attendee with a once-only TASK award and a FEEDBACK award."""

    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        self.admin_id = rig.staff_id(StaffRole.ADMIN)
        self.booth_id, self.staff_id = rig.booth_with_staff()
        self.user_id = rig.make_user("rev")
        self.task_id = rig.make_task(self.booth_id, points=10)
        self.task_award = rig.plant_task_award(
            user_id=self.user_id,
            booth_id=self.booth_id,
            staff_id=self.staff_id,
            task_id=self.task_id,
            points=10,
        )
        self.feedback_award = rig.plant_feedback_award(
            user_id=self.user_id, booth_id=self.booth_id, points=5
        )
        rig.sign_in(StaffRole.ADMIN)


@pytest.fixture
def case(rig: Rig) -> Case:
    return Case(rig)


# --- the reversal itself -----------------------------------------------------------


def test_an_administrator_reverses_a_task_award_in_full(case: Case) -> None:
    rig = case.rig
    before = rig.ledger_row(case.task_award)
    assert rig.total_points(case.user_id) == 15

    response = _reverse(rig, case.task_award)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "reversal_id",
        "reversed_entry_id",
        "points_reversed",
        "resulting_total",
        "created_at",
        "replayed",
    }
    assert body["reversed_entry_id"] == case.task_award
    assert body["points_reversed"] == 10 and body["resulting_total"] == 5
    assert body["replayed"] is False
    # the original is untouched
    assert rig.ledger_row(case.task_award) == before
    # the reversal is a new, linked, negative row; everything but the reason and the
    # actor comes from the original
    row = rig.ledger_row(body["reversal_id"])
    assert row["entry_type"] == "REVERSAL" and row["points"] == -10
    assert row["reverses_points_log_id"] == case.task_award
    assert (row["user_id"], row["booth_id"], row["task_id"], row["source"]) == (
        before["user_id"], before["booth_id"], before["task_id"], before["source"],
    )  # fmt: skip
    assert row["entered_by_staff_id"] == case.admin_id and row["entered_by_system"] is False
    assert row["dedupe_key"] is None and row["note"] == REASON
    assert rig.total_points(case.user_id) == 5


def test_an_administrator_reverses_a_feedback_award_and_is_recorded_as_the_actor(
    case: Case,
) -> None:
    rig = case.rig
    response = _reverse(rig, case.feedback_award, reason="Duplicate reward given by mistake")
    assert response.status_code == 200, response.text
    row = rig.ledger_row(response.json()["reversal_id"])
    # a FEEDBACK row used to have to be staff-less and "system"; a reversal by an
    # administrator names the administrator instead (migration 0012)
    assert row["source"] == "FEEDBACK" and row["entry_type"] == "REVERSAL" and row["points"] == -5
    assert row["entered_by_staff_id"] == case.admin_id and row["entered_by_system"] is False
    assert rig.total_points(case.user_id) == 10
    # the feedback AWARD itself is still the system's, unchanged
    original = rig.ledger_row(case.feedback_award)
    assert original["entered_by_staff_id"] is None and original["entered_by_system"] is True


def test_the_old_attribution_rules_still_hold(case: Case) -> None:
    """The constraint only gained the administrator's FEEDBACK reversal: nothing
    else that was refused before is allowed now."""
    rig = case.rig
    other_user = rig.make_user("attr")
    bad = [
        # a FEEDBACK AWARD by a staff account
        "INSERT INTO points_log (user_id, booth_id, source, points, entered_by_staff_id, "
        "entered_by_system) VALUES (:u, :b, 'FEEDBACK', 5, :s, false)",
        # a FEEDBACK AWARD that is not the system's
        "INSERT INTO points_log (user_id, booth_id, source, points, entered_by_staff_id, "
        "entered_by_system) VALUES (:u, :b, 'FEEDBACK', 5, NULL, false)",
        # a TASK AWARD by the system
        "INSERT INTO points_log (user_id, booth_id, source, points, entered_by_staff_id, "
        "entered_by_system) VALUES (:u, :b, 'TASK', 5, NULL, true)",
        # a FEEDBACK REVERSAL that names a staff account but still says "system"
        "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
        "reverses_points_log_id, entered_by_staff_id, entered_by_system) "
        "VALUES (:u, :b, 'FEEDBACK', 'REVERSAL', -5, :orig, :s, true)",
        # a FEEDBACK REVERSAL with neither
        "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
        "reverses_points_log_id, entered_by_staff_id, entered_by_system) "
        "VALUES (:u, :b, 'FEEDBACK', 'REVERSAL', -5, :orig, NULL, false)",
    ]
    for statement in bad:
        with pytest.raises(IntegrityError) as raised:
            rig.sql(
                statement, u=other_user, b=case.booth_id, s=case.admin_id, orig=case.feedback_award
            )
        assert "source_attribution" in str(raised.value), statement


def test_the_amount_attendee_and_source_come_from_the_original_not_the_request(case: Case) -> None:
    rig = case.rig
    other_user = rig.make_user("other")
    forbidden_fields: list[dict[str, Any]] = [
        {"points": 3},
        {"points": -10},
        {"user_id": other_user},
        {"booth_id": 1},
        {"source": "FEEDBACK"},
        {"entry_type": "REVERSAL"},
        {"reverses_points_log_id": 1},
        {"entered_by_staff_id": 1},
        {"task_id": 1},
    ]
    for forbidden in forbidden_fields:
        response = _reverse(rig, case.task_award, **forbidden)
        assert _error(response) == (422, "VALIDATION_ERROR"), forbidden
    assert rig.sql("SELECT count(*) FROM points_log WHERE entry_type = 'REVERSAL'")[0][0] == 0
    assert rig.total_points(other_user) == 0


def test_an_entry_can_be_reversed_once_only_by_any_route(case: Case) -> None:
    rig = case.rig
    first = _reverse(rig, case.task_award)
    assert first.status_code == 200
    # another key, same entry: refused, whatever the reason
    again = _reverse(rig, case.task_award, reason="Trying a second time on purpose")
    assert _error(again) == (409, "POINTS_ALREADY_REVERSED")
    assert (
        rig.sql(
            "SELECT count(*) FROM points_log WHERE reverses_points_log_id = :i", i=case.task_award
        )[0][0]
        == 1
    )
    # and the database says the same, without the application
    with pytest.raises(IntegrityError) as raised:
        rig.sql(
            "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, task_id, "
            "reverses_points_log_id, entered_by_staff_id, entered_by_system) "
            "VALUES (:u, :b, 'TASK', 'REVERSAL', -10, :t, :orig, :s, false)",
            u=case.user_id, b=case.booth_id, t=case.task_id, orig=case.task_award, s=case.admin_id,
        )  # fmt: skip
    assert "uq_points_reverses" in str(raised.value)


def test_a_reversal_cannot_be_reversed_and_an_unknown_entry_is_a_404(case: Case) -> None:
    rig = case.rig
    reversal_id = _reverse(rig, case.task_award).json()["reversal_id"]
    assert _error(_reverse(rig, reversal_id)) == (409, "POINTS_NOT_REVERSIBLE")
    assert _error(_reverse(rig, 9_999_999)) == (404, "POINTS_ENTRY_NOT_FOUND")
    for bad_id in ("0", "-4", "abc", "1.5"):
        response = rig.client.post(
            f"/api/admin/points-log/{bad_id}/reverse",
            json={"reason": REASON, "idempotency_key": str(uuid.uuid4())},
        )
        assert response.status_code == 422, bad_id
    assert rig.total_points(case.user_id) == 5  # nothing but the one reversal happened


# --- authority -----------------------------------------------------------------


def test_only_an_administrator_may_reverse(case: Case) -> None:
    rig = case.rig
    key = str(uuid.uuid4())
    body = {"reason": REASON, "idempotency_key": key}
    url = f"/api/admin/points-log/{case.task_award}/reverse"

    rig.sign_out()
    assert rig.client.post(url, json=body).status_code == 401

    rig.sign_in_attendee(case.user_id)
    assert rig.client.post(url, json=body).status_code == 401  # an attendee session is not staff

    rig.sign_in_as(case.staff_id)  # the booth staff member who made the award
    forbidden = rig.client.post(url, json=body)
    assert _error(forbidden) == (403, "STAFF_NOT_AUTHORIZED")

    inactive = rig.staff_id(StaffRole.ADMIN)
    rig.sql("UPDATE staff_accounts SET is_active = false WHERE id = :i", i=inactive)
    rig.sign_in_as(inactive)
    assert rig.client.post(url, json=body).status_code == 403

    assert rig.sql("SELECT count(*) FROM points_log WHERE entry_type = 'REVERSAL'")[0][0] == 0
    assert rig.sql("SELECT count(*) FROM idempotency_records")[0][0] == 0
    assert rig.audit_rows("points.reverse") == []


# --- idempotency and races --------------------------------------------------------


def test_a_repeat_with_the_same_key_replays_and_creates_nothing_new(case: Case) -> None:
    rig = case.rig
    key = str(uuid.uuid4())
    first = _reverse(rig, case.task_award, key=key).json()
    second = _reverse(rig, case.task_award, key=key).json()
    third = _reverse(rig, case.task_award, key=key).json()

    assert first["replayed"] is False and second["replayed"] is True and third["replayed"] is True
    for field in (
        "reversal_id",
        "reversed_entry_id",
        "points_reversed",
        "resulting_total",
        "created_at",
    ):
        assert first[field] == second[field] == third[field]
    assert rig.sql("SELECT count(*) FROM points_log WHERE entry_type = 'REVERSAL'")[0][0] == 1
    assert len(rig.audit_rows("points.reverse")) == 1
    assert rig.total_points(case.user_id) == 5


def test_a_key_cannot_be_reused_for_another_entry_or_another_reason(case: Case) -> None:
    rig = case.rig
    key = str(uuid.uuid4())
    assert _reverse(rig, case.task_award, key=key).status_code == 200
    other_reason = _reverse(rig, case.task_award, key=key, reason="A different reason entirely")
    assert _error(other_reason) == (409, "IDEMPOTENCY_KEY_REUSE")
    other_entry = _reverse(rig, case.feedback_award, key=key)
    assert _error(other_entry) == (409, "IDEMPOTENCY_KEY_REUSE")
    assert rig.sql("SELECT count(*) FROM points_log WHERE entry_type = 'REVERSAL'")[0][0] == 1


def _race(
    case: Case, requests: list[tuple[uuid.UUID, str]], entry_id: int
) -> list[tuple[Any, ...]]:
    """Runs one `reverse_entry` per request at the same moment, each in its own
    session and transaction, and returns what each one did."""
    rig = case.rig
    token = rig.session_for(case.admin_id)
    barrier = threading.Barrier(len(requests))
    results: list[tuple[Any, ...]] = []
    lock = threading.Lock()

    def worker(key: uuid.UUID, reason: str) -> None:
        with Session(rig.engine) as db:
            barrier.wait()
            try:
                with mutating_transaction(db) as tx:
                    outcome = reverse_entry(
                        tx,
                        raw_session_token=token,
                        entry_id=entry_id,
                        request=ReversalRequest(reason=reason, idempotency_key=key),
                        now=datetime.now(UTC),
                    )
                result: tuple[Any, ...] = (
                    "ok", outcome.response.reversal_id, outcome.response.replayed,
                )  # fmt: skip
            except DomainError as err:
                result = ("error", err.code)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=worker, args=r) for r in requests]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results


def test_parallel_retries_of_one_request_create_one_reversal(case: Case) -> None:
    key = uuid.uuid4()
    results = _race(case, [(key, REASON)] * 8, case.task_award)

    assert len(results) == 8
    assert all(r[0] == "ok" for r in results), results
    assert len({r[1] for r in results}) == 1  # one reversal id for all of them
    assert sorted(r[2] for r in results) == [False] + [True] * 7  # one original, seven replays
    rig = case.rig
    assert rig.sql("SELECT count(*) FROM points_log WHERE entry_type = 'REVERSAL'")[0][0] == 1
    assert len(rig.audit_rows("points.reverse")) == 1
    assert rig.total_points(case.user_id) == 5


def test_parallel_different_requests_for_one_entry_create_one_reversal(case: Case) -> None:
    results = _race(
        case, [(uuid.uuid4(), f"Reason number {n} for the race") for n in range(8)], case.task_award
    )

    assert len(results) == 8
    ok = [r for r in results if r[0] == "ok"]
    refused = [r for r in results if r[0] == "error"]
    assert len(ok) == 1 and len(refused) == 7, results
    assert {r[1] for r in refused} == {"POINTS_ALREADY_REVERSED"}
    rig = case.rig
    assert rig.sql("SELECT count(*) FROM points_log WHERE entry_type = 'REVERSAL'")[0][0] == 1
    assert len(rig.audit_rows("points.reverse")) == 1
    assert rig.total_points(case.user_id) == 5


def test_a_failure_after_the_reversal_row_rolls_everything_back(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = case.rig
    key = str(uuid.uuid4())

    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("the audit could not be written")

    monkeypatch.setattr(reversal_module, "_audit_values", boom)
    with pytest.raises(RuntimeError, match="audit could not"):
        _reverse(rig, case.task_award, key=key)

    # neither the reversal, nor the claimed key, nor an audit row survived
    assert rig.sql("SELECT count(*) FROM points_log WHERE entry_type = 'REVERSAL'")[0][0] == 0
    assert rig.sql("SELECT count(*) FROM idempotency_records")[0][0] == 0
    assert rig.audit_rows("points.reverse") == []
    assert rig.total_points(case.user_id) == 15

    monkeypatch.undo()
    retry = _reverse(rig, case.task_award, key=key)  # the same key still works
    assert retry.status_code == 200 and retry.json()["replayed"] is False
    assert rig.total_points(case.user_id) == 5


# --- the reason -----------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "",
        "  ",
        "ab",
        "x" * 301,
        "has a \x00 nul byte",
        "bell \x07 character",
        "a" * 5000,
        "\n\t  \n",
    ],
)
def test_a_missing_overlong_or_unreadable_reason_is_refused(case: Case, reason: str) -> None:
    response = _reverse(case.rig, case.task_award, reason=reason)
    assert _error(response) == (422, "VALIDATION_ERROR"), reason
    if len(reason.strip()) > 8:  # (a 2-letter reason is a substring of any English word)
        assert reason not in response.text  # the validation error never echoes it back
    assert case.rig.sql("SELECT count(*) FROM points_log WHERE entry_type = 'REVERSAL'")[0][0] == 0


def test_the_reason_is_tidied_and_bounded_everywhere(case: Case) -> None:
    rig = case.rig
    spaced = "  Wrong   attendee\n scanned   "
    body = _reverse(rig, case.task_award, reason=spaced).json()
    assert rig.ledger_row(body["reversal_id"])["note"] == "Wrong attendee scanned"
    longest = "é" * 300  # the limit is in characters, not bytes
    assert _reverse(rig, case.feedback_award, reason=longest).status_code == 200
    # the database refuses an over-long note whatever the application does
    with pytest.raises(IntegrityError) as raised:
        rig.sql("UPDATE points_log SET note = repeat('x', 301) WHERE id = :i", i=case.task_award)
    assert "note_length" in str(raised.value)


def test_the_reason_never_reaches_a_log(case: Case, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    secret = "Private remark about Bob Smith 5551234"
    case.rig.client.post(
        f"/api/admin/points-log/{case.task_award}/reverse",
        json={"reason": secret, "idempotency_key": str(uuid.uuid4())},
    )  # success
    case.rig.client.post(
        f"/api/admin/points-log/{case.task_award}/reverse",
        json={"reason": secret, "idempotency_key": str(uuid.uuid4())},
    )  # refused: already reversed
    case.rig.client.post(
        f"/api/admin/points-log/{case.feedback_award}/reverse",
        json={"reason": secret, "idempotency_key": "not-a-uuid"},
    )  # invalid
    assert "Bob Smith" not in caplog.text and "5551234" not in caplog.text


# --- audit ---------------------------------------------------------------------


def test_the_reversal_and_its_audit_row_are_one_unit_and_the_audit_view_shows_it_safely(
    case: Case,
) -> None:
    rig = case.rig
    reversal_id = _reverse(rig, case.task_award).json()["reversal_id"]

    rows = rig.audit_rows("points.reverse")
    assert len(rows) == 1
    row = rows[0]
    assert row["staff_account_id"] == case.admin_id
    assert (row["target_type"], row["target_id"]) == ("points_log", str(case.task_award))
    assert row["before_value"]["reversed"] is False and row["before_value"]["points"] == 10
    assert row["after_value"]["reversed"] is True and row["after_value"]["points"] == 0
    assert (
        row["after_value"]["reversal_id"] == reversal_id and row["after_value"]["reason"] == REASON
    )

    page = rig.client.get("/api/admin/audit", params={"action": "points.reverse"}).json()
    event = page["items"][0]
    assert event["details"] == "known" and event["hidden_field_count"] == 0
    assert event["after"]["reason"] == REASON and event["after"]["reversal_id"] == reversal_id
    assert set(event["changed_fields"]) == {"points", "reversed"}
    assert event["before"]["user_id"] == case.user_id  # the attendee, by id only
    assert "points.reverse" in audit_view.FIELD_ALLOWLISTS


def test_a_refused_reversal_writes_no_audit_row(case: Case) -> None:
    rig = case.rig
    assert _reverse(rig, case.task_award).status_code == 200
    _reverse(rig, case.task_award)  # refused
    _reverse(rig, 9_999_999)  # unknown
    _reverse(rig, case.task_award, reason="")  # invalid
    assert len(rig.audit_rows("points.reverse")) == 1


# --- realtime ------------------------------------------------------------------


def test_the_attendee_and_the_leaderboard_are_signalled_only_after_the_commit(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = case.rig
    seen: list[tuple[int, int]] = []

    def record(user_id: int) -> None:
        # by now the reversal is committed and visible from ANOTHER connection
        seen.append((user_id, rig.total_points(user_id)))

    monkeypatch.setattr("app.modules.admin.routers.publish_award_signals", record)
    key = str(uuid.uuid4())
    assert _reverse(rig, case.task_award, key=key).status_code == 200
    assert seen == [(case.user_id, 5)]

    _reverse(rig, case.task_award, key=key)  # a replay: nothing new committed
    _reverse(rig, case.task_award)  # refused
    _reverse(rig, 9_999_999)  # unknown
    assert seen == [(case.user_id, 5)]


def test_a_failed_reversal_publishes_nothing(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    monkeypatch.setattr("app.modules.admin.routers.publish_award_signals", seen.append)

    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("after the row")

    monkeypatch.setattr(reversal_module, "_audit_values", boom)
    with pytest.raises(RuntimeError):
        _reverse(case.rig, case.task_award)
    assert seen == []


def test_the_personal_score_and_the_leaderboard_reflect_the_reversal(case: Case) -> None:
    rig = case.rig
    _reverse(rig, case.task_award)
    rig.sign_in_attendee(case.user_id)
    score = rig.client.get("/api/attendees/me/score").json()
    assert score["total_points"] == 5
    # the reversal is a real, visible ledger entry of the attendee's history
    assert sorted(a["points"] for a in score["recent_awards"]) == [-10, 5, 10]
    top = rig.client.get("/api/leaderboard").json()
    assert top["current_user"]["total_points"] == 5
    everyone = rig.client.get("/api/leaderboard", params={"view": "all", "limit": 100}).json()
    assert {e["user_id"]: e["total_points"] for e in everyone["entries"]}[case.user_id] == 5


# --- the ledger list and the CSV ---------------------------------------------------


def test_the_ledger_list_links_the_award_and_its_reversal_both_ways(case: Case) -> None:
    rig = case.rig
    reversal_id = _reverse(rig, case.task_award).json()["reversal_id"]
    rows = {
        r["id"]: r
        for r in rig.client.get("/api/admin/points-log", params={"user_id": case.user_id}).json()[
            "items"
        ]
    }
    original, reverse = rows[case.task_award], rows[reversal_id]
    assert original["reversed_by_entry_id"] == reversal_id and original["reverses_entry_id"] is None
    assert (
        reverse["reverses_entry_id"] == case.task_award and reverse["reversed_by_entry_id"] is None
    )
    assert reverse["reason"] == REASON and original["reason"] is None
    assert reverse["points"] == -10 and reverse["entry_type"] == "REVERSAL"
    assert reverse["staff_name"] is not None  # who reversed it
    assert reverse["entered_by_staff_id"] == case.admin_id
    # an award that was never reversed carries no link
    assert rows[case.feedback_award]["reversed_by_entry_id"] is None
    # the REVERSAL filter finds it
    only = rig.client.get("/api/admin/points-log", params={"entry_type": "REVERSAL"}).json()[
        "items"
    ]
    assert [r["id"] for r in only] == [reversal_id]


def test_the_csv_export_has_the_new_entries_and_columns(case: Case, rig: Rig) -> None:
    factory = sessionmaker(bind=rig.engine, autoflush=False)
    from app.main import app

    app.dependency_overrides[exports.export_session_factory] = lambda: factory
    tickets.clear()
    export_status.store.clear()
    exports._active.clear()
    reversal_id = _reverse(
        rig, case.task_award, reason='=HYPERLINK("http://evil.test","click")'
    ).json()["reversal_id"]

    prepared = rig.client.post(
        "/api/admin/exports/points-log", json={"user_id": case.user_id}
    ).json()
    response = rig.client.get(prepared["download_url"])
    assert response.status_code == 200
    data = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"), newline="")))
    header, rows = data[0], data[1:]
    assert header[-3:] == ["reverses_entry_id", "reversed_by_entry_id", "reversal_reason"]
    by_id = {r[header.index("id")]: dict(zip(header, r, strict=True)) for r in rows}
    original, reverse = by_id[str(case.task_award)], by_id[str(reversal_id)]
    assert (
        original["reversed_by_entry_id"] == str(reversal_id) and original["reverses_entry_id"] == ""
    )
    assert reverse["reverses_entry_id"] == str(case.task_award) and reverse["points"] == "-10"
    assert reverse["entry_type"] == "REVERSAL" and reverse["entered_by_staff_id"] == str(
        case.admin_id
    )
    # a reason that starts like a formula is neutralised, as every exported text is
    assert reverse["reversal_reason"].startswith("'=")
    assert original["reversal_reason"] == ""


# --- what a reversal does NOT do: reopen an activity -------------------------------------


def _award(rig: Rig, *, staff_id: int, task_id: int, points: int, repeatable: bool) -> Any:
    token = rig.sign_in_as(staff_id)
    session_id = rig.scalar(
        "SELECT id FROM sessions WHERE token_hash = :h", h=hash_session_token(token)
    )
    scan_ref = issue_scan_ref(
        secret=get_settings().scan_ref_secret,
        staff_id=staff_id,
        session_id=session_id,
        card_token=ELENA_TOKEN,
        now=datetime.now(UTC),
    )
    return rig.client.post(
        AWARD,
        json={
            "card_token": ELENA_TOKEN,
            "task_id": task_id,
            "rule_revision": compute_rule_revision(
                task_id=task_id, points=points, repeatable=repeatable
            ),
            "idempotency_key": str(uuid.uuid4()),
            "scan_ref": scan_ref,
        },
    )


def _admin_reverse_latest_award(rig: Rig, award_id: int) -> None:
    rig.sign_in(StaffRole.ADMIN)
    assert _reverse(rig, award_id).status_code == 200


def test_a_once_only_activity_is_not_reopened_by_a_reversal(rig: Rig) -> None:
    booth_id, staff_id = rig.booth_with_staff()
    task_id = rig.make_task(booth_id, points=10, repeatable=False)
    user_id = int(
        rig.scalar("SELECT id FROM users WHERE email = 'elena.marchetti@attendee.facemark.demo'")
    )

    first = _award(rig, staff_id=staff_id, task_id=task_id, points=10, repeatable=False)
    assert first.status_code == 200, first.text
    _admin_reverse_latest_award(rig, first.json()["points_log_id"])
    assert rig.total_points(user_id) == 0

    again = _award(rig, staff_id=staff_id, task_id=task_id, points=10, repeatable=False)

    # the default, and the rule: the original award still occupies the slot
    assert _error(again) == (409, "DUPLICATE_TASK_REWARD")
    assert rig.total_points(user_id) == 0
    rows = rig.sql(
        "SELECT entry_type, count(*) FROM points_log WHERE user_id = :u AND task_id = :t "
        "GROUP BY entry_type ORDER BY entry_type",
        u=user_id, t=task_id,
    )  # fmt: skip
    assert [tuple(r) for r in rows] == [("AWARD", 1), ("REVERSAL", 1)]


def test_a_repeatable_activity_can_be_earned_again_after_a_reversal(rig: Rig) -> None:
    booth_id, staff_id = rig.booth_with_staff()
    task_id = rig.make_task(booth_id, points=7, repeatable=True)
    user_id = int(
        rig.scalar("SELECT id FROM users WHERE email = 'elena.marchetti@attendee.facemark.demo'")
    )

    first = _award(rig, staff_id=staff_id, task_id=task_id, points=7, repeatable=True)
    assert first.status_code == 200, first.text
    _admin_reverse_latest_award(rig, first.json()["points_log_id"])
    second = _award(rig, staff_id=staff_id, task_id=task_id, points=7, repeatable=True)

    assert second.status_code == 200, second.text  # as it always could be
    assert rig.total_points(user_id) == 7


def test_a_feedback_reward_is_not_restored_by_a_reversal(case: Case) -> None:
    rig = case.rig
    assert _reverse(rig, case.feedback_award).status_code == 200
    with pytest.raises(IntegrityError) as raised:
        rig.plant_feedback_award(user_id=case.user_id, booth_id=case.booth_id)
    assert "uq_points_feedback_user_booth" in str(raised.value)


# --- the database, without the application --------------------------------------------


def test_the_reason_column_and_links_are_constrained_by_the_database(case: Case) -> None:
    rig = case.rig
    reversal_id = _reverse(rig, case.task_award).json()["reversal_id"]
    # the reversal row must point at an entry; points must be negative
    for statement in (
        "UPDATE points_log SET reverses_points_log_id = NULL WHERE id = :i",
        "UPDATE points_log SET points = 10 WHERE id = :i",
    ):
        with pytest.raises(IntegrityError):
            rig.sql(statement, i=reversal_id)
    # an existing check name we rely on is still there
    names = {
        r[0]
        for r in rig.sql(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'points_log'::regclass AND contype = 'c'"
        )
    }
    assert {"ck_points_log_source_attribution", "ck_points_log_note_length"} <= names


def test_reason_text_survives_a_round_trip_as_data(case: Case) -> None:
    rig = case.rig
    markup = "<img src=x onerror=alert(1)> & 'quotes' \"double\""
    body = _reverse(rig, case.task_award, reason=markup).json()
    assert rig.ledger_row(body["reversal_id"])["note"] == markup
    page = rig.client.get("/api/admin/points-log", params={"entry_type": "REVERSAL"}).json()
    assert page["items"][0]["reason"] == markup
    assert re.search(r"\\u003c|<img", rig.client.get("/api/admin/points-log").text) is not None
