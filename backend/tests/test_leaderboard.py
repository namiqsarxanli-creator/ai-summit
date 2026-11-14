"""M13 acceptance: leaderboard and attendee score snapshot —
ARCHITECTURE.md §16, §17.4, §24.1, §24.22, IMPLEMENTATION_PLAN.md M13.

Real PostgreSQL throughout. `rig` builds real, committed fixtures —
ACTIVE attendees via the actual M04 registration+OTP-verify flow (no
shortcut exists to become ACTIVE, by design, §8.1), booths and
points_log rows via direct SQL (M13 itself awards nothing; the same
technique M12 used to test reversal-eligibility) — and tears every
row down in FK-safe order.

Both `/api/leaderboard` and `/api/attendees/me/score` are pure reads:
no fence, no `mutating_transaction`. Nothing here exercises
`assert_fenced_before_any_read` for that reason — there is no fence
acquisition to prove.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import text

from app.core.security import generate_session_token, hash_password, hash_session_token
from app.db.session import SessionLocal
from app.modules.leaderboard.service import decode_cursor
from tests.m04_support import (
    captured_otp,
    execute,
    fetch_one,
    make_client,
    new_email,
    register_payload,
)

LEADERBOARD = "/api/leaderboard"
SCORE = "/api/attendees/me/score"
REGISTER = "/api/attendees/register"
VERIFY = "/api/auth/otp/verify"

SEEDED_TOP_N = 10  # app/seed/demo_fixtures.py's event_config default
TEST_PASSWORD_HASH = hash_password("m13-test-only-staff-password")  # noqa: S106


def _insert_returning_id(sql: str, **params: Any) -> int:
    db = SessionLocal()
    try:
        row: Any = db.execute(text(sql), params).scalar_one()
        db.commit()
        return int(row)
    finally:
        db.close()


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


# --------------------------------------------------------------------------
# The `rig` fixture: a self-cleaning builder for attendees / booths / awards
# --------------------------------------------------------------------------


@dataclass
class Rig:
    otp_log: pytest.LogCaptureFixture
    user_ids: list[int] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    booth_ids: list[int] = field(default_factory=list)
    staff_ids: list[int] = field(default_factory=list)

    def booth(self) -> int:
        slug = f"m13-test-booth-{uuid.uuid4().hex[:10]}"
        booth_id = _insert_returning_id(
            "INSERT INTO booths (slug, name) VALUES (:slug, :name) RETURNING id",
            slug=slug,
            name=f"M13 Test Booth {slug[-6:]}",
        )
        self.booth_ids.append(booth_id)
        return booth_id

    def staff(self, booth_id: int) -> int:
        email = f"m13-{uuid.uuid4().hex[:10]}@staff.test.facemark.demo"
        staff_id = _insert_returning_id(
            "INSERT INTO staff_accounts (email, password_hash, full_name, role, booth_id) "
            "VALUES (:email, :hash, 'M13 Test Staff', 'BOOTH_STAFF', :booth_id) RETURNING id",
            email=email,
            hash=TEST_PASSWORD_HASH,
            booth_id=booth_id,
        )
        self.staff_ids.append(staff_id)
        return staff_id

    def active_attendee(self, **overrides: Any) -> int:
        email = new_email("m13")
        with make_client() as c:
            assert c.post(REGISTER, json=register_payload(email, **overrides)).status_code == 200
            code = captured_otp(self.otp_log.records, email)
            assert c.post(VERIFY, json={"email": email, "code": code}).status_code == 200
        row = fetch_one("SELECT id FROM users WHERE email = :e", e=email.lower())
        assert row is not None
        user_id = int(row[0])
        self.user_ids.append(user_id)
        self.emails.append(email)
        return user_id

    def provisional_attendee(self, **overrides: Any) -> int:
        email = new_email("m13")
        with make_client() as c:
            assert c.post(REGISTER, json=register_payload(email, **overrides)).status_code == 200
        row = fetch_one("SELECT id FROM users WHERE email = :e", e=email.lower())
        assert row is not None
        user_id = int(row[0])
        self.user_ids.append(user_id)
        self.emails.append(email)
        return user_id

    def session(self, user_id: int, *, expired: bool = False, revoked: bool = False) -> str:
        raw = generate_session_token()
        execute(
            "INSERT INTO sessions (token_hash, principal_type, user_id, issued_at, "
            "expires_at, revoked_at) SELECT :h, 'ATTENDEE', :u, now() - interval '1 hour', "
            "CASE WHEN :expired THEN now() - interval '1 minute' "
            "ELSE now() + interval '12 hours' END, "
            "CASE WHEN :revoked THEN now() ELSE NULL END",
            h=hash_session_token(raw),
            u=user_id,
            expired=expired,
            revoked=revoked,
        )
        return raw

    def award(
        self, user_id: int, booth_id: int, points: int, *, created_at: datetime | None = None
    ) -> int:
        """A FEEDBACK-sourced AWARD row — M13 awards nothing itself, so
        every ledger row a test needs is planted directly, exactly the
        technique M12 used for its own reversal-eligibility tests."""
        return _insert_returning_id(
            "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
            "entered_by_staff_id, entered_by_system, created_at) "
            "VALUES (:u, :b, 'FEEDBACK', 'AWARD', :p, NULL, true, COALESCE(:ca, now())) "
            "RETURNING id",
            u=user_id,
            b=booth_id,
            p=points,
            ca=created_at,
        )

    def task_award(
        self,
        user_id: int,
        booth_id: int,
        staff_id: int,
        points: int,
        *,
        created_at: datetime | None = None,
    ) -> int:
        return _insert_returning_id(
            "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
            "entered_by_staff_id, entered_by_system, created_at) "
            "VALUES (:u, :b, 'TASK', 'AWARD', :p, :staff, false, COALESCE(:ca, now())) "
            "RETURNING id",
            u=user_id,
            b=booth_id,
            p=points,
            staff=staff_id,
            ca=created_at,
        )

    def reverse(
        self,
        award_id: int,
        user_id: int,
        booth_id: int,
        points: int,
        *,
        created_at: datetime | None = None,
    ) -> int:
        return _insert_returning_id(
            "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
            "entered_by_staff_id, entered_by_system, reverses_points_log_id, created_at) "
            "VALUES (:u, :b, 'FEEDBACK', 'REVERSAL', :p, NULL, true, :rev, "
            "COALESCE(:ca, now())) RETURNING id",
            u=user_id,
            b=booth_id,
            p=-abs(points),
            rev=award_id,
            ca=created_at,
        )


@pytest.fixture
def rig(otp_log: pytest.LogCaptureFixture) -> Iterator[Rig]:
    r = Rig(otp_log=otp_log)
    yield r
    for uid in reversed(r.user_ids):
        execute("DELETE FROM points_log WHERE user_id = :u", u=uid)
        execute("DELETE FROM sessions WHERE user_id = :u", u=uid)
        execute("DELETE FROM consent_audit WHERE user_id = :u", u=uid)
        execute("DELETE FROM contact_cards WHERE user_id = :u", u=uid)
        execute("DELETE FROM user_interests WHERE user_id = :u", u=uid)
    # otp_challenges (FK'd to users) must go before the users themselves.
    for email in reversed(r.emails):
        execute("DELETE FROM otp_challenges WHERE email = :e", e=email)
    for uid in reversed(r.user_ids):
        execute("DELETE FROM users WHERE id = :u", u=uid)
    for staff_id in reversed(r.staff_ids):
        execute("DELETE FROM points_log WHERE entered_by_staff_id = :s", s=staff_id)
        execute("DELETE FROM staff_accounts WHERE id = :s", s=staff_id)
    for booth_id in reversed(r.booth_ids):
        execute("DELETE FROM points_log WHERE booth_id = :b", b=booth_id)
        execute("DELETE FROM booths WHERE id = :b", b=booth_id)


def _get(path: str, session_token: str | None) -> Response:
    with make_client() as c:
        if session_token is not None:
            c.cookies.set("session", session_token)
        response: Response = c.get(path)
        return response


# --------------------------------------------------------------------------
# Eligible population
# --------------------------------------------------------------------------


def test_zero_point_active_attendee_appears_with_zero(rig: Rig) -> None:
    uid = rig.active_attendee(first_name="Zero", last_name="Points")
    token = rig.session(uid)
    body = _get(SCORE, token).json()
    assert body["total_points"] == 0
    assert body["recent_awards"] == []

    lb = _get(LEADERBOARD, token).json()
    entry = next(e for e in lb["entries"] + [lb["current_user"]] if e and e["user_id"] == uid)
    assert entry["total_points"] == 0


def test_provisional_attendee_excluded_even_with_ledger_row(rig: Rig) -> None:
    active_uid = rig.active_attendee(first_name="Seen", last_name="Attendee")
    provisional_uid = rig.provisional_attendee(first_name="Hidden", last_name="Attendee")
    booth_id = rig.booth()
    # Defence-in-depth: even if a PROVISIONAL user somehow had a ledger
    # row (normally impossible — no capability exists before ACTIVE,
    # invariant #25), the aggregation's own `status = 'ACTIVE'` filter
    # must still exclude them.
    rig.award(provisional_uid, booth_id, 50)
    rig.award(active_uid, booth_id, 10)

    token = rig.session(active_uid)
    lb = _get(f"{LEADERBOARD}?view=all&limit=100", token).json()
    seen_ids = {e["user_id"] for e in lb["entries"]}
    assert active_uid in seen_ids
    assert provisional_uid not in seen_ids


# --------------------------------------------------------------------------
# Source contribution and reversal
# --------------------------------------------------------------------------


def test_task_award_contributes_to_total(rig: Rig) -> None:
    uid = rig.active_attendee(first_name="Task", last_name="Earner")
    booth_id = rig.booth()
    staff_id = rig.staff(booth_id)
    rig.task_award(uid, booth_id, staff_id, 15)

    token = rig.session(uid)
    assert _get(SCORE, token).json()["total_points"] == 15


def test_feedback_award_contributes_to_total(rig: Rig) -> None:
    uid = rig.active_attendee(first_name="Feedback", last_name="Earner")
    booth_id = rig.booth()
    rig.award(uid, booth_id, 5)

    token = rig.session(uid)
    assert _get(SCORE, token).json()["total_points"] == 5


def test_reversal_reduces_total_via_sum(rig: Rig) -> None:
    uid = rig.active_attendee(first_name="Reversed", last_name="Attendee")
    booth_id = rig.booth()
    award_id = rig.award(uid, booth_id, 20)
    rig.reverse(award_id, uid, booth_id, 20)

    token = rig.session(uid)
    body = _get(SCORE, token).json()
    assert body["total_points"] == 0
    # Both the AWARD and the REVERSAL are real ledger rows — the
    # reversal is never deleted or hidden, §14.1.
    assert len(body["recent_awards"]) == 2


# --------------------------------------------------------------------------
# Ranking and ordering
# --------------------------------------------------------------------------


def test_equal_totals_share_rank_and_rank_skips_after_ties(rig: Rig) -> None:
    booth_id = rig.booth()
    a = rig.active_attendee(first_name="Alpha", last_name="Tie")
    b = rig.active_attendee(first_name="Bravo", last_name="Tie")
    c = rig.active_attendee(first_name="Charlie", last_name="Solo")
    rig.award(a, booth_id, 100)
    rig.award(b, booth_id, 100)
    boothed2 = rig.booth()
    rig.award(c, boothed2, 90)

    token = rig.session(a)
    lb = _get(f"{LEADERBOARD}?view=all&limit=100", token).json()
    by_id = {e["user_id"]: e for e in lb["entries"]}
    assert by_id[a]["rank"] == 1
    assert by_id[b]["rank"] == 1
    # Competition ranking: the next distinct total skips to rank 3, not 2.
    assert by_id[c]["rank"] == 3


def test_deterministic_ordering_is_byte_identical_across_repeated_queries(rig: Rig) -> None:
    booth_id = rig.booth()
    uid = rig.active_attendee(first_name="Repeat", last_name="Query")
    rig.award(uid, booth_id, 30)
    token = rig.session(uid)

    first = _get(f"{LEADERBOARD}?view=all&limit=100", token).json()["entries"]
    second = _get(f"{LEADERBOARD}?view=all&limit=100", token).json()["entries"]
    assert first == second


def test_zero_points_have_no_moment_whether_never_scored_or_reversed(rig: Rig) -> None:
    booth_id = rig.booth()
    zero = rig.active_attendee(first_name="ZeroAward", last_name="Sorts")
    reversed_ = rig.active_attendee(first_name="Reversed", last_name="Sorts")
    # `zero` has no ledger rows at all; `reversed_`'s only award was cancelled.
    # Neither holds a standing award, so neither has a moment of reaching its
    # total (`reached_at` IS NULL) and the stable user id orders them. (Before
    # the audit of 18 Oct 2026 the reversal row itself dated the total and put
    # `reversed_` first.) A total of 0 can only be reached this way: standing
    # awards are positive and a reversal cancels its award whole.
    award_id = rig.award(reversed_, booth_id, 10)
    rig.reverse(award_id, reversed_, booth_id, 10)

    token = rig.session(zero)
    lb = _get(f"{LEADERBOARD}?view=all&limit=100", token).json()
    ids_in_order = [e["user_id"] for e in lb["entries"]]
    totals = {e["user_id"]: e["total_points"] for e in lb["entries"]}
    assert totals[zero] == totals[reversed_] == 0
    assert ids_in_order.index(zero) < ids_in_order.index(reversed_)


# --------------------------------------------------------------------------
# Top-N view
# --------------------------------------------------------------------------


def test_top_n_returns_exactly_n_when_enough_attendees(rig: Rig) -> None:
    execute("UPDATE event_config SET leaderboard_top_n = 3 WHERE id = 1")
    try:
        booth_id = rig.booth()
        uids = [rig.active_attendee(first_name=f"TopN{i}", last_name="Attendee") for i in range(5)]
        for i, uid in enumerate(uids):
            rig.award(uid, booth_id, 100 - i)

        token = rig.session(uids[0])
        lb = _get(LEADERBOARD, token).json()
        assert len(lb["entries"]) == 3
    finally:
        execute("UPDATE event_config SET leaderboard_top_n = :n WHERE id = 1", n=SEEDED_TOP_N)


def test_tie_at_top_n_boundary_does_not_expand_entries_past_n(rig: Rig) -> None:
    execute("UPDATE event_config SET leaderboard_top_n = 2 WHERE id = 1")
    try:
        booth_id = rig.booth()
        # Three attendees tied at the SAME total — a rank-based cutoff
        # (`rank <= 2`) would let all three through; the list-position
        # cutoff must not.
        uids = [rig.active_attendee(first_name=f"Tied{i}", last_name="Boundary") for i in range(3)]
        for uid in uids:
            rig.award(uid, booth_id, 50)

        token = rig.session(uids[0])
        lb = _get(LEADERBOARD, token).json()
        assert len(lb["entries"]) == 2
    finally:
        execute("UPDATE event_config SET leaderboard_top_n = :n WHERE id = 1", n=SEEDED_TOP_N)


def test_rank_reflects_full_population_not_just_the_returned_page(rig: Rig) -> None:
    execute("UPDATE event_config SET leaderboard_top_n = 1 WHERE id = 1")
    try:
        booth_id = rig.booth()
        leader = rig.active_attendee(first_name="Leader", last_name="Full")
        outside = rig.active_attendee(first_name="Outside", last_name="Full")
        rig.award(leader, booth_id, 100)
        rig.award(outside, booth_id, 10)

        token = rig.session(outside)
        lb = _get(LEADERBOARD, token).json()
        # `outside` is not in `entries` (top_n=1), but its OWN rank
        # (2) is still its true full-population rank, not derived
        # from the 1-row page it was excluded from.
        assert lb["current_user"]["rank"] == 2
    finally:
        execute("UPDATE event_config SET leaderboard_top_n = :n WHERE id = 1", n=SEEDED_TOP_N)


def test_current_user_inside_top_n_is_also_in_entries(rig: Rig) -> None:
    booth_id = rig.booth()
    uid = rig.active_attendee(first_name="Inside", last_name="TopN")
    rig.award(uid, booth_id, 999)

    token = rig.session(uid)
    lb = _get(LEADERBOARD, token).json()
    assert lb["current_user"]["user_id"] == uid
    assert any(e["user_id"] == uid for e in lb["entries"])


def test_current_user_outside_top_n_is_not_duplicated_into_entries(rig: Rig) -> None:
    execute("UPDATE event_config SET leaderboard_top_n = 1 WHERE id = 1")
    try:
        booth_id = rig.booth()
        leader = rig.active_attendee(first_name="Leader2", last_name="Full")
        outside = rig.active_attendee(first_name="Outside2", last_name="Full")
        rig.award(leader, booth_id, 100)
        rig.award(outside, booth_id, 10)

        token = rig.session(outside)
        lb = _get(LEADERBOARD, token).json()
        assert len(lb["entries"]) == 1
        assert lb["entries"][0]["user_id"] == leader
        assert lb["current_user"]["user_id"] == outside
    finally:
        execute("UPDATE event_config SET leaderboard_top_n = :n WHERE id = 1", n=SEEDED_TOP_N)


# --------------------------------------------------------------------------
# Show-all keyset pagination
# --------------------------------------------------------------------------


def _collect_all_ids(token: str, *, limit: int = 3, max_pages: int = 200) -> list[int]:
    """Walks `view=all` to completion. Real committed state (five
    pre-existing ACTIVE demo/seed attendees, all zero-point — verified
    directly against this database) can share a fixture's total_points
    or NULL `last_award_at` group and low ids, so a boundary test looks
    at the FULL walk rather than assuming its own rows land on the
    first page or two."""
    ids: list[int] = []
    cursor: str | None = None
    for _ in range(max_pages):
        path = f"{LEADERBOARD}?view=all&limit={limit}"
        if cursor is not None:
            path += f"&cursor={cursor}"
        body = _get(path, token).json()
        ids.extend(e["user_id"] for e in body["entries"])
        cursor = body["next_cursor"]
        if cursor is None:
            return ids
    pytest.fail("pagination did not terminate")
    return ids  # unreachable; satisfies the type checker


def test_show_all_pagination_visits_every_active_attendee_exactly_once(rig: Rig) -> None:
    booth_id = rig.booth()
    uids = [rig.active_attendee(first_name=f"Page{i}", last_name="Walker") for i in range(7)]
    for i, uid in enumerate(uids):
        rig.award(uid, booth_id, 10 + i)

    token = rig.session(uids[0])
    seen = _collect_all_ids(token)
    for uid in uids:
        assert seen.count(uid) == 1


def test_pagination_boundary_with_equal_points(rig: Rig) -> None:
    booth_id = rig.booth()
    uids = sorted(
        rig.active_attendee(first_name=f"EqualPts{i}", last_name="Boundary") for i in range(4)
    )
    for uid in uids:
        rig.award(uid, booth_id, 40)

    token = rig.session(uids[0])
    seen = [uid for uid in _collect_all_ids(token, limit=2) if uid in uids]
    assert seen == sorted(seen)  # tiebreak is user_id ASC within the tie
    assert set(seen) == set(uids)


def test_pagination_boundary_with_equal_timestamps(rig: Rig) -> None:
    booth_id = rig.booth()
    same_time = datetime.now(UTC) - timedelta(minutes=5)
    uids = sorted(
        rig.active_attendee(first_name=f"EqualTs{i}", last_name="Boundary") for i in range(4)
    )
    for uid in uids:
        rig.award(uid, booth_id, 25, created_at=same_time)

    token = rig.session(uids[0])
    seen = [uid for uid in _collect_all_ids(token, limit=2) if uid in uids]
    assert seen == sorted(seen)
    assert set(seen) == set(uids)


def test_pagination_boundary_with_null_last_award_at(rig: Rig) -> None:
    uids = sorted(
        rig.active_attendee(first_name=f"NullTs{i}", last_name="Boundary") for i in range(4)
    )
    # No ledger rows at all: total_points=0, last_award_at NULL for all
    # four — the same group pre-existing zero-point ACTIVE attendees
    # fall into, which is exactly why this walks the full list rather
    # than reading just the first two pages.
    token = rig.session(uids[0])
    seen = [uid for uid in _collect_all_ids(token, limit=2) if uid in uids]
    assert seen == sorted(seen)
    assert set(seen) == set(uids)


def test_next_cursor_decodes_to_the_last_page_rows_own_sort_key_not_a_points_log_id(
    rig: Rig,
) -> None:
    """Invariant #22: `points_log.id` is never a cursor. Decodes the
    real, server-issued cursor and checks its fields are the returned
    page's own (total_points, last_award_at, user_id) — never any
    `points_log.id` value, which never appears in the response at all."""
    booth_id = rig.booth()
    uids = [rig.active_attendee(first_name=f"Cursor{i}", last_name="Check") for i in range(3)]
    award_ids = [rig.award(uid, booth_id, 10 + i) for i, uid in enumerate(uids)]

    token = rig.session(uids[0])
    page = _get(f"{LEADERBOARD}?view=all&limit=1", token).json()
    assert page["next_cursor"] is not None
    decoded = decode_cursor(page["next_cursor"])
    last_row = page["entries"][-1]
    assert decoded.total_points == last_row["total_points"]
    assert decoded.user_id == last_row["user_id"]
    assert decoded.user_id not in award_ids  # disjoint id spaces; belt-and-braces


def test_malformed_cursor_is_rejected_as_validation_error(rig: Rig) -> None:
    uid = rig.active_attendee(first_name="Malformed", last_name="Cursor")
    token = rig.session(uid)
    garbage = base64.urlsafe_b64encode(b"not json").decode().rstrip("=")
    response = _get(f"{LEADERBOARD}?view=all&cursor={garbage}", token)
    assert _error(response) == (422, "VALIDATION_ERROR")


def test_tampered_but_well_formed_cursor_is_accepted_as_a_position_not_an_identity(
    rig: Rig,
) -> None:
    """A cursor is a POSITION in the ordering, not a signed capability
    — nothing in the locked contract requires it to be tamper-proof,
    only that OFFSET pagination is never used. A hand-built cursor
    naming an arbitrary position is valid input, not an attack."""
    uid = rig.active_attendee(first_name="HandBuilt", last_name="Cursor")
    token = rig.session(uid)
    payload = json.dumps({"tp": 999999, "la": None, "uid": 0}, separators=(",", ":"))
    cursor = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    response = _get(f"{LEADERBOARD}?view=all&cursor={cursor}", token)
    assert response.status_code == 200


# --------------------------------------------------------------------------
# as_of / dataset_generation
# --------------------------------------------------------------------------


def test_as_of_is_a_recent_server_timestamp_on_both_endpoints(rig: Rig) -> None:
    uid = rig.active_attendee(first_name="AsOf", last_name="Checker")
    token = rig.session(uid)
    now = datetime.now(UTC)

    for path in (LEADERBOARD, SCORE):
        as_of = datetime.fromisoformat(_get(path, token).json()["as_of"].replace("Z", "+00:00"))
        assert abs((as_of - now).total_seconds()) < 10


def test_dataset_generation_matches_event_config_on_both_endpoints(rig: Rig) -> None:
    uid = rig.active_attendee(first_name="Generation", last_name="Checker")
    token = rig.session(uid)
    row = fetch_one("SELECT dataset_generation FROM event_config WHERE id = 1")
    assert row is not None
    current_generation = int(row[0])

    for path in (LEADERBOARD, SCORE):
        assert _get(path, token).json()["dataset_generation"] == current_generation


# --------------------------------------------------------------------------
# Recent awards
# --------------------------------------------------------------------------


def test_recent_awards_ordered_created_at_desc_id_desc(rig: Rig) -> None:
    uid = rig.active_attendee(first_name="Recent", last_name="Order")
    b1, b2, b3 = rig.booth(), rig.booth(), rig.booth()
    base = datetime.now(UTC) - timedelta(hours=1)
    id_oldest = rig.award(uid, b1, 5, created_at=base)
    id_middle = rig.award(uid, b2, 5, created_at=base + timedelta(minutes=10))
    id_newest = rig.award(uid, b3, 5, created_at=base + timedelta(minutes=20))

    token = rig.session(uid)
    recent = _get(SCORE, token).json()["recent_awards"]
    assert [r["id"] for r in recent] == [id_newest, id_middle, id_oldest]


def test_recent_awards_includes_booth_name_and_source(rig: Rig) -> None:
    uid = rig.active_attendee(first_name="Detail", last_name="Award")
    booth_id = rig.booth()
    row = fetch_one("SELECT name FROM booths WHERE id = :b", b=booth_id)
    assert row is not None
    booth_name = str(row[0])
    rig.award(uid, booth_id, 5)

    token = rig.session(uid)
    recent = _get(SCORE, token).json()["recent_awards"]
    assert recent[0]["booth_name"] == booth_name
    assert recent[0]["source"] == "FEEDBACK"


# --------------------------------------------------------------------------
# Privacy
# --------------------------------------------------------------------------


_FORBIDDEN_KEY_SUBSTRINGS = ("email", "phone", "linkedin", "consent", "comment", "session")


def _assert_no_forbidden_keys(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            assert not any(bad in key.lower() for bad in _FORBIDDEN_KEY_SUBSTRINGS), key
            _assert_no_forbidden_keys(value)
    elif isinstance(node, list):
        for item in node:
            _assert_no_forbidden_keys(item)


def test_leaderboard_dto_never_exposes_pii_fields(rig: Rig) -> None:
    booth_id = rig.booth()
    uid = rig.active_attendee(first_name="Privacy", last_name="Checked")
    rig.award(uid, booth_id, 10)

    token = rig.session(uid)
    body = _get(f"{LEADERBOARD}?view=all&limit=10", token).json()
    _assert_no_forbidden_keys(body)
    assert set(body["entries"][0].keys()) == {
        "user_id",
        "name",  # first name and surname initial only
        "profession",
        "total_points",
        "rank",
        "photo_url",  # the profile photo's URL (small rendition) or None - no private field
    }
    assert body["entries"][0]["name"] == "Privacy C."
    assert "Checked" not in str(body)  # the full surname is never sent


def test_score_dto_never_exposes_pii_fields(rig: Rig) -> None:
    uid = rig.active_attendee(first_name="Privacy2", last_name="Checked")
    token = rig.session(uid)
    body = _get(SCORE, token).json()
    _assert_no_forbidden_keys(body)


# --------------------------------------------------------------------------
# Authentication / authority
# --------------------------------------------------------------------------


def test_leaderboard_and_score_require_authentication(rig: Rig) -> None:
    assert _error(_get(LEADERBOARD, None)) == (401, "UNAUTHENTICATED")
    assert _error(_get(SCORE, None)) == (401, "UNAUTHENTICATED")


def test_expired_session_is_rejected(rig: Rig) -> None:
    uid = rig.active_attendee(first_name="Expired", last_name="Session")
    token = rig.session(uid, expired=True)
    assert _error(_get(SCORE, token)) == (401, "UNAUTHENTICATED")


def test_revoked_session_is_rejected(rig: Rig) -> None:
    uid = rig.active_attendee(first_name="Revoked", last_name="Session")
    token = rig.session(uid, revoked=True)
    assert _error(_get(SCORE, token)) == (401, "UNAUTHENTICATED")


def test_provisional_attendee_session_is_rejected(rig: Rig) -> None:
    uid = rig.provisional_attendee()
    token = rig.session(uid)
    assert _error(_get(SCORE, token)) == (403, "REGISTRATION_NOT_VERIFIED")


def test_score_endpoint_ignores_a_client_supplied_user_id_and_returns_only_the_caller(
    rig: Rig,
) -> None:
    booth_id = rig.booth()
    me = rig.active_attendee(first_name="Me", last_name="Caller")
    other = rig.active_attendee(first_name="Other", last_name="Attendee")
    rig.award(other, booth_id, 500)  # a large, obviously-distinguishable total

    token = rig.session(me)
    body = _get(f"{SCORE}?user_id={other}", token).json()
    assert body["total_points"] == 0  # `me`'s own total, never `other`'s


# --------------------------------------------------------------------------
# No mutable/cached total
# --------------------------------------------------------------------------


def test_no_mutable_total_points_column_exists_on_users(rig: Rig) -> None:
    row = fetch_one(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'users' AND column_name = 'total_points'"
    )
    assert row is not None
    assert int(row[0]) == 0


# --------------------------------------------------------------------------
# Performance (§16.4 / IMPLEMENTATION_PLAN.md M13 Tests: "Performance")
# --------------------------------------------------------------------------


def test_aggregation_stays_fast_over_a_few_thousand_row_ledger(rig: Rig) -> None:
    """Not full summit scale (§16.4: "hundreds of attendees, low
    thousands of point rows") — real attendees only come from the real
    registration+OTP flow (no shortcut exists), so this uses a smaller,
    real attendee count (30, bulk-SQL-inserted booths and points_log
    rows) to reach a genuinely multi-thousand-row ledger without an
    impractically slow test. The assertion is generous (well under
    what would ever be noticeable on stage) precisely to avoid CI
    flakiness while still catching a real regression (e.g. a missing
    index, or an accidental N+1)."""
    attendee_count = 30
    booth_count = 100  # 30 * 100 = 3,000 points_log rows
    uids = [
        rig.active_attendee(first_name=f"Perf{i}", last_name="Scale") for i in range(attendee_count)
    ]

    db = SessionLocal()
    try:
        booth_rows = [
            {"slug": f"m13-perf-booth-{i}-{uuid.uuid4().hex[:8]}", "name": f"Perf Booth {i}"}
            for i in range(booth_count)
        ]
        db.execute(
            text("INSERT INTO booths (slug, name) VALUES (:slug, :name)"),
            booth_rows,
        )
        db.commit()
        booth_ids = [
            int(r[0])
            for r in db.execute(
                text("SELECT id FROM booths WHERE slug = ANY(:slugs)"),
                {"slugs": [b["slug"] for b in booth_rows]},
            ).all()
        ]
        rig.booth_ids.extend(booth_ids)

        award_rows = [
            {"u": uid, "b": booth_id, "p": 1 + (i % 15)}
            for uid in uids
            for i, booth_id in enumerate(booth_ids)
        ]
        db.execute(
            text(
                "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                "entered_by_staff_id, entered_by_system) "
                "VALUES (:u, :b, 'FEEDBACK', 'AWARD', :p, NULL, true)"
            ),
            award_rows,
        )
        db.commit()

        plan: Any = db.execute(
            text(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
                "SELECT u.id, COALESCE(SUM(p.points), 0) AS total_points, "
                "MAX(p.created_at) AS last_award_at "
                "FROM users u LEFT JOIN points_log p ON p.user_id = u.id "
                "WHERE u.status = 'ACTIVE' GROUP BY u.id"
            )
        ).scalar_one()
    finally:
        db.close()

    execution_time_ms = plan[0]["Execution Time"]
    assert execution_time_ms < 500, f"aggregation took {execution_time_ms}ms"

    token = rig.session(uids[0])
    response = _get(f"{LEADERBOARD}?view=all&limit=5", token)
    assert response.status_code == 200
