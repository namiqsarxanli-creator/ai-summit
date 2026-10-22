"""M12 acceptance: attendee feedback and automatic reward —
ARCHITECTURE.md §7.11, §7.12, §7.14, §14.5, §15, §19.4, §21.4, §24.1,
§24.6, §24.11, §24.15, §24.16, §24.29, IMPLEMENTATION_PLAN.md M12.

Real PostgreSQL throughout. `scenario()` builds one fresh, real ACTIVE
attendee (via the real M04 registration+activation path) plus a fresh
committed booth, and tears both down in FK-safe order (points_log
before feedback/users, since points_log.user_id references users).
"""

from __future__ import annotations

import inspect
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from httpx import Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.errors import UnexpectedConstraintViolationError
from app.core.locks import FACEMARK_RESET_FENCE
from app.core.security import generate_session_token, hash_session_token
from app.db.session import SessionLocal
from app.modules.feedback.models import Feedback
from app.modules.feedback.schemas import FeedbackRequest
from app.modules.feedback.service import submit_feedback
from app.modules.points.models import PointsLog
from tests.m04_support import (
    PSYCOPG_URL,
    assert_fenced_before_any_read,
    captured_otp,
    execute,
    fetch_one,
    make_client,
    new_email,
    register_payload,
)

FEEDBACK = "/api/attendees/feedback"
REGISTER = "/api/attendees/register"
VERIFY = "/api/auth/otp/verify"

SEEDED_FEEDBACK_POINTS = 5  # app/seed/demo_fixtures.py's event_config default


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
# The `scenario` fixture
# --------------------------------------------------------------------------


@dataclass
class Scenario:
    booth_id: int
    booth_slug: str
    booth_name: str
    user_id: int
    email: str
    session_token: str


def _plant_attendee_session(user_id: int, *, expired: bool = False, revoked: bool = False) -> str:
    raw = generate_session_token()
    execute(
        "INSERT INTO sessions (token_hash, principal_type, user_id, issued_at, expires_at, "
        "revoked_at) SELECT :h, 'ATTENDEE', :u, now() - interval '1 hour', "
        "CASE WHEN :expired THEN now() - interval '1 minute' ELSE now() + interval '12 hours' END, "
        "CASE WHEN :revoked THEN now() ELSE NULL END",
        h=hash_session_token(raw),
        u=user_id,
        expired=expired,
        revoked=revoked,
    )
    return raw


@pytest.fixture
def scenario(otp_log: pytest.LogCaptureFixture) -> Iterator[Callable[..., Scenario]]:
    made: list[Scenario] = []

    def make(**session_kwargs: Any) -> Scenario:
        slug = f"m12-test-booth-{uuid.uuid4().hex[:10]}"
        booth_name = f"M12 Test Booth {slug[-6:]}"
        booth_id = _insert_returning_id(
            "INSERT INTO booths (slug, name) VALUES (:slug, :name) RETURNING id",
            slug=slug,
            name=booth_name,
        )

        email = new_email("m12")
        with make_client() as c:
            assert c.post(REGISTER, json=register_payload(email)).status_code == 200
            code = captured_otp(otp_log.records, email)
            assert c.post(VERIFY, json={"email": email, "code": code}).status_code == 200
        row = fetch_one("SELECT id FROM users WHERE email = :e", e=email.lower())
        assert row is not None
        user_id = int(row[0])

        raw_token = _plant_attendee_session(user_id, **session_kwargs)

        s = Scenario(
            booth_id=booth_id,
            booth_slug=slug,
            booth_name=booth_name,
            user_id=user_id,
            email=email,
            session_token=raw_token,
        )
        made.append(s)
        return s

    yield make

    for s in reversed(made):
        execute("DELETE FROM points_log WHERE user_id = :u", u=s.user_id)
        execute("DELETE FROM feedback WHERE user_id = :u", u=s.user_id)
        execute("DELETE FROM sessions WHERE user_id = :u", u=s.user_id)
        execute("DELETE FROM otp_challenges WHERE email = :e", e=s.email)
        execute("DELETE FROM consent_audit WHERE user_id = :u", u=s.user_id)
        execute("DELETE FROM contact_cards WHERE user_id = :u", u=s.user_id)
        execute("DELETE FROM user_interests WHERE user_id = :u", u=s.user_id)
        execute("DELETE FROM users WHERE id = :u", u=s.user_id)
        execute("DELETE FROM points_log WHERE booth_id = :b", b=s.booth_id)
        execute("DELETE FROM feedback WHERE booth_id = :b", b=s.booth_id)
        execute("DELETE FROM booths WHERE id = :b", b=s.booth_id)


def _feedback(
    s: Scenario,
    *,
    session_token: str | None = None,
    rating: int = 5,
    comment: str | None = None,
    booth_slug: str | None = None,
) -> Response:
    with make_client() as c:
        c.cookies.set("session", session_token if session_token is not None else s.session_token)
        body: dict[str, Any] = {
            "booth_slug": booth_slug if booth_slug is not None else s.booth_slug,
            "rating": rating,
        }
        if comment is not None:
            body["comment"] = comment
        response: Response = c.post(FEEDBACK, json=body)
        return response


def _award_row(user_id: int, booth_id: int) -> Any:
    return fetch_one(
        "SELECT points, entered_by_staff_id, entered_by_system, dedupe_key, source, entry_type "
        "FROM points_log WHERE user_id = :u AND booth_id = :b AND source = 'FEEDBACK' "
        "AND entry_type = 'AWARD'",
        u=user_id,
        b=booth_id,
    )


# --------------------------------------------------------------------------
# Golden path: first submission awards
# --------------------------------------------------------------------------


def test_first_feedback_submission_is_awarded(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    response = _feedback(s, rating=5, comment="Great booth!")
    assert response.status_code == 200
    body = response.json()
    assert body["awarded_points"] == SEEDED_FEEDBACK_POINTS
    assert body["already_rewarded"] is False
    assert body["total_points"] == SEEDED_FEEDBACK_POINTS
    assert isinstance(body["feedback_id"], int)

    feedback_row = fetch_one(
        "SELECT rating, comment FROM feedback WHERE id = :id", id=body["feedback_id"]
    )
    assert feedback_row is not None
    assert feedback_row[0] == 5
    assert feedback_row[1] == "Great booth!"


def test_configured_feedback_points_value_is_used(scenario: Callable[..., Scenario]) -> None:
    execute("UPDATE event_config SET feedback_points = 7 WHERE id = 1")
    try:
        s = scenario()
        response = _feedback(s)
        assert response.json()["awarded_points"] == 7
    finally:
        execute(
            "UPDATE event_config SET feedback_points = :p WHERE id = 1", p=SEEDED_FEEDBACK_POINTS
        )


def test_reward_row_satisfies_the_feedback_check_triple_with_no_dedupe_key(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario()
    _feedback(s)
    row = _award_row(s.user_id, s.booth_id)
    assert row is not None
    points, entered_by_staff_id, entered_by_system, dedupe_key, source, entry_type = row
    assert points == SEEDED_FEEDBACK_POINTS
    assert entered_by_staff_id is None
    assert entered_by_system is True
    assert dedupe_key is None
    assert source == "FEEDBACK"
    assert entry_type == "AWARD"


# --------------------------------------------------------------------------
# Duplicate feedback: persists, no second reward
# --------------------------------------------------------------------------


def test_second_feedback_same_booth_persists_with_zero_additional_reward(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario()
    first = _feedback(s, rating=5, comment="First impression")
    assert first.status_code == 200
    assert first.json()["already_rewarded"] is False

    second = _feedback(s, rating=3, comment="Changed my mind")
    assert second.status_code == 200  # never an error — §15
    body = second.json()
    assert body["awarded_points"] == 0
    assert body["already_rewarded"] is True
    assert body["total_points"] == SEEDED_FEEDBACK_POINTS  # unchanged — no second reward
    assert body["feedback_id"] != first.json()["feedback_id"]

    rows = fetch_one(
        "SELECT count(*) FROM feedback WHERE user_id = :u AND booth_id = :b",
        u=s.user_id,
        b=s.booth_id,
    )
    assert rows[0] == 2  # both feedback rows stored — §7.11, never rejected
    award_rows = fetch_one(
        "SELECT count(*) FROM points_log WHERE user_id = :u AND booth_id = :b "
        "AND source = 'FEEDBACK' AND entry_type = 'AWARD'",
        u=s.user_id,
        b=s.booth_id,
    )
    assert award_rows[0] == 1  # exactly one reward, ever


def test_second_feedback_comment_is_never_lost(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    _feedback(s, rating=5, comment="first")
    second = _feedback(s, rating=2, comment="a more considered take")
    row = fetch_one("SELECT comment FROM feedback WHERE id = :id", id=second.json()["feedback_id"])
    assert row is not None
    assert row[0] == "a more considered take"


def test_different_booth_gets_an_independent_reward(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    other_slug = f"m12-test-booth-{uuid.uuid4().hex[:10]}"
    other_booth_id = _insert_returning_id(
        "INSERT INTO booths (slug, name) VALUES (:slug, :name) RETURNING id",
        slug=other_slug,
        name="M12 Second Booth",
    )
    try:
        first = _feedback(s)
        assert first.json()["awarded_points"] == SEEDED_FEEDBACK_POINTS

        second = _feedback(s, booth_slug=other_slug)
        assert second.json()["awarded_points"] == SEEDED_FEEDBACK_POINTS
        assert second.json()["already_rewarded"] is False
        assert second.json()["total_points"] == 2 * SEEDED_FEEDBACK_POINTS
    finally:
        execute("DELETE FROM points_log WHERE booth_id = :b", b=other_booth_id)
        execute("DELETE FROM feedback WHERE booth_id = :b", b=other_booth_id)
        execute("DELETE FROM booths WHERE id = :b", b=other_booth_id)


# --------------------------------------------------------------------------
# Reversal semantics — locked
# --------------------------------------------------------------------------


def test_reward_reversal_does_not_restore_eligibility(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    first = _feedback(s, rating=5)
    assert first.json()["awarded_points"] == SEEDED_FEEDBACK_POINTS
    assert first.json()["total_points"] == SEEDED_FEEDBACK_POINTS

    orig_id = fetch_one(
        "SELECT id FROM points_log WHERE user_id = :u AND booth_id = :b "
        "AND source = 'FEEDBACK' AND entry_type = 'AWARD'",
        u=s.user_id,
        b=s.booth_id,
    )
    assert orig_id is not None
    execute(
        "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
        "reverses_points_log_id, entered_by_system) VALUES "
        "(:u, :b, 'FEEDBACK', 'REVERSAL', :neg, :orig, true)",
        u=s.user_id,
        b=s.booth_id,
        neg=-SEEDED_FEEDBACK_POINTS,
        orig=orig_id[0],
    )

    second = _feedback(s, rating=1, comment="after the reversal")
    assert second.status_code == 200
    body = second.json()
    assert body["awarded_points"] == 0
    assert body["already_rewarded"] is True
    # total reflects BOTH the original +5 award and the -5 reversal.
    assert body["total_points"] == 0

    assert (
        fetch_one(
            "SELECT count(*) FROM feedback WHERE user_id = :u AND booth_id = :b",
            u=s.user_id,
            b=s.booth_id,
        )[0]
        == 2
    )
    assert (
        fetch_one(
            "SELECT count(*) FROM points_log WHERE user_id = :u AND booth_id = :b "
            "AND source = 'FEEDBACK' AND entry_type = 'AWARD'",
            u=s.user_id,
            b=s.booth_id,
        )[0]
        == 1
    )


# --------------------------------------------------------------------------
# Authority: attendee session, ACTIVE gating, booth resolution
# --------------------------------------------------------------------------


def test_provisional_attendee_cannot_submit_feedback(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    execute("UPDATE users SET status = 'PROVISIONAL' WHERE id = :u", u=s.user_id)
    assert _error(_feedback(s)) == (403, "REGISTRATION_NOT_VERIFIED")
    assert fetch_one("SELECT count(*) FROM feedback WHERE user_id = :u", u=s.user_id)[0] == 0


def test_expired_session_is_rejected(scenario: Callable[..., Scenario]) -> None:
    s = scenario(expired=True)
    assert _error(_feedback(s)) == (401, "UNAUTHENTICATED")


def test_revoked_session_is_rejected(scenario: Callable[..., Scenario]) -> None:
    s = scenario(revoked=True)
    assert _error(_feedback(s)) == (401, "UNAUTHENTICATED")


def test_unauthenticated_caller_is_rejected(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    with make_client() as c:
        response = c.post(FEEDBACK, json={"booth_slug": s.booth_slug, "rating": 5})
    assert _error(response) == (401, "UNAUTHENTICATED")


def test_staff_session_cannot_submit_feedback(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    from app.core.security import hash_password

    staff_id = _insert_returning_id(
        "INSERT INTO staff_accounts (email, password_hash, full_name, role) "
        "VALUES (:e, :h, 'M12 Staff', 'ADMIN') RETURNING id",
        e=f"m12-staff-{uuid.uuid4().hex[:10]}@staff.test.facemark.demo",
        h=hash_password("irrelevant"),
    )
    raw = generate_session_token()
    execute(
        "INSERT INTO sessions (token_hash, principal_type, staff_account_id, issued_at, "
        "expires_at) VALUES (:h, 'STAFF', :sid, now(), now() + interval '12 hours')",
        h=hash_session_token(raw),
        sid=staff_id,
    )
    try:
        assert _error(_feedback(s, session_token=raw)) == (401, "UNAUTHENTICATED")
    finally:
        execute("DELETE FROM sessions WHERE staff_account_id = :s", s=staff_id)
        execute("DELETE FROM staff_accounts WHERE id = :s", s=staff_id)


@pytest.mark.parametrize("rating", [0, 6, -1, 100])
def test_invalid_rating_is_rejected(scenario: Callable[..., Scenario], rating: int) -> None:
    s = scenario()
    assert _error(_feedback(s, rating=rating)) == (422, "VALIDATION_ERROR")
    assert fetch_one("SELECT count(*) FROM feedback WHERE user_id = :u", u=s.user_id)[0] == 0


def test_unknown_booth_slug_is_rejected(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    assert _error(_feedback(s, booth_slug="does-not-exist")) == (404, "BOOTH_NOT_FOUND")


def test_inactive_booth_is_rejected(scenario: Callable[..., Scenario]) -> None:
    s = scenario()
    execute("UPDATE booths SET is_active = false WHERE id = :b", b=s.booth_id)
    assert _error(_feedback(s)) == (404, "BOOTH_NOT_FOUND")


# --------------------------------------------------------------------------
# Field authority — no client-supplied user_id/booth_id/points/etc.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "user_id",
        "booth_id",
        "points",
        "source",
        "staff_id",
        "entered_by_system",
        "dataset_generation",
    ],
)
def test_request_body_cannot_express_server_derived_fields(
    scenario: Callable[..., Scenario], field: str
) -> None:
    s = scenario()
    body: dict[str, Any] = {"booth_slug": s.booth_slug, "rating": 5, field: 999999}
    with make_client() as c:
        c.cookies.set("session", s.session_token)
        response = c.post(FEEDBACK, json=body)
    assert _error(response) == (422, "VALIDATION_ERROR")
    assert fetch_one("SELECT count(*) FROM feedback WHERE user_id = :u", u=s.user_id)[0] == 0


def test_reward_always_carries_the_submitting_attendee_and_resolved_booth(
    scenario: Callable[..., Scenario],
) -> None:
    victim = scenario()
    s = scenario()
    _feedback(s)
    row = _award_row(s.user_id, s.booth_id)
    assert row is not None
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=victim.user_id)[0] == 0


# --------------------------------------------------------------------------
# Reset fence
# --------------------------------------------------------------------------


def test_feedback_is_fenced_before_any_reset_sensitive_read(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario()

    def call() -> Response:
        return _feedback(s)

    response = assert_fenced_before_any_read(call)
    assert response.status_code == 200


# --------------------------------------------------------------------------
# Concurrency (real PostgreSQL)
# --------------------------------------------------------------------------


def test_two_simultaneous_first_submissions_both_persist_exactly_one_reward(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario()
    results: list[Response] = []
    lock = threading.Lock()

    def fire() -> None:
        r = _feedback(s, rating=4)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(results) == 2
    for r in results:
        assert r.status_code == 200
    already_rewarded_flags = sorted(r.json()["already_rewarded"] for r in results)
    assert already_rewarded_flags == [False, True]
    awarded_points = sorted(r.json()["awarded_points"] for r in results)
    assert awarded_points == [0, SEEDED_FEEDBACK_POINTS]

    assert (
        fetch_one(
            "SELECT count(*) FROM feedback WHERE user_id = :u AND booth_id = :b",
            u=s.user_id,
            b=s.booth_id,
        )[0]
        == 2
    )
    assert (
        fetch_one(
            "SELECT count(*) FROM points_log WHERE user_id = :u AND booth_id = :b "
            "AND source = 'FEEDBACK' AND entry_type = 'AWARD'",
            u=s.user_id,
            b=s.booth_id,
        )[0]
        == 1
    )


def test_first_concurrent_reward_rolling_back_lets_the_waiter_become_sole_awarder(
    scenario: Callable[..., Scenario],
) -> None:
    """Exercises the exact PostgreSQL mechanism the reward SAVEPOINT
    depends on directly: if the FIRST transaction to claim the partial
    unique index ultimately rolls back entirely (not merely loses the
    race), the transaction that was blocked waiting on it must become
    the sole, normal owner — never a permanently-blocked or lost award."""
    s = scenario()
    conn_a = psycopg.connect(PSYCOPG_URL)
    conn_a.autocommit = False
    conn_b = psycopg.connect(PSYCOPG_URL)
    conn_b.autocommit = False
    try:
        with conn_a.cursor() as cur_a:
            cur_a.execute(
                "INSERT INTO points_log (user_id, booth_id, source, points, entered_by_system) "
                "VALUES (%s, %s, 'FEEDBACK', %s, true)",
                (s.user_id, s.booth_id, SEEDED_FEEDBACK_POINTS),
            )

        b_done = threading.Event()

        def try_b() -> None:
            with conn_b.cursor() as cur_b:
                cur_b.execute(
                    "INSERT INTO points_log (user_id, booth_id, source, points, "
                    "entered_by_system) VALUES (%s, %s, 'FEEDBACK', %s, true)",
                    (s.user_id, s.booth_id, SEEDED_FEEDBACK_POINTS),
                )
            conn_b.commit()
            b_done.set()

        b_thread = threading.Thread(target=try_b)
        b_thread.start()
        time.sleep(0.3)
        assert not b_done.is_set(), "B did not block on A's uncommitted claim"

        conn_a.rollback()  # T1 aborts entirely — not just loses a race
        b_thread.join(timeout=5)
        assert b_done.is_set(), "B never became the sole owner once A released"
    finally:
        conn_a.close()
        conn_b.close()

    assert (
        fetch_one(
            "SELECT count(*) FROM points_log WHERE user_id = :u AND booth_id = :b",
            u=s.user_id,
            b=s.booth_id,
        )[0]
        == 1
    )


# --------------------------------------------------------------------------
# Rollback / failure injection — real code path, not source inspection alone
# --------------------------------------------------------------------------


def _inject_flush_failure(model_cls: type, constraint_name: str) -> Callable[..., None]:
    """Fails the NEXT flush that has a pending instance of `model_cls`
    in `db.new` — not a raw call count, which would also catch
    SQLAlchemy's own internal flush inside `begin_nested()`'s snapshot
    (taken before `points_log` is even added, so it never matches the
    PointsLog case, and after `feedback` is already persisted, so it
    never matches the Feedback case either)."""

    def make(db: Any) -> None:
        real_flush = db.flush

        def fake_flush(*args: Any, **kwargs: Any) -> Any:
            if any(isinstance(obj, model_cls) for obj in db.new):
                orig = MagicMock()
                orig.diag = MagicMock(constraint_name=constraint_name)
                raise IntegrityError("INSERT ...", {}, orig)
            return real_flush(*args, **kwargs)

        db.flush = fake_flush

    return make


def test_unrelated_integrity_error_during_reward_insert_is_not_swallowed(
    scenario: Callable[..., Scenario],
) -> None:
    """The reward SAVEPOINT recovers ONLY `uq_points_feedback_user_booth`
    (§15) — every other constraint name must fail the OUTER transaction.
    Triggering a genuinely different violation through the real HTTP
    flow is structurally impossible here (every field the service
    constructs already satisfies every other constraint by
    construction) — so this exercises the real code path directly, by
    making the reward insert's own flush() raise a fabricated,
    unrelated constraint name, and asserts it propagates and rolls back
    the WHOLE transaction, including the feedback row already inserted."""
    s = scenario()
    db = SessionLocal()
    try:
        db.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": FACEMARK_RESET_FENCE})
        _inject_flush_failure(PointsLog, "some_unrelated_future_constraint")(db)
        payload = FeedbackRequest(booth_slug=s.booth_slug, rating=5)
        with pytest.raises(UnexpectedConstraintViolationError):
            submit_feedback(db, raw_session_token=s.session_token, payload=payload)
    finally:
        db.rollback()
        db.close()
    assert fetch_one("SELECT count(*) FROM feedback WHERE user_id = :u", u=s.user_id)[0] == 0
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 0


def test_failure_during_feedback_insert_rolls_back_the_outer_transaction(
    scenario: Callable[..., Scenario],
) -> None:
    """A failure at the FIRST flush (the feedback insert itself) must
    also roll back cleanly — nothing partially committed, no reward
    ever attempted."""
    s = scenario()
    db = SessionLocal()
    try:
        db.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": FACEMARK_RESET_FENCE})
        _inject_flush_failure(Feedback, "some_unrelated_future_constraint")(db)
        payload = FeedbackRequest(booth_slug=s.booth_slug, rating=5)
        with pytest.raises(UnexpectedConstraintViolationError):
            submit_feedback(db, raw_session_token=s.session_token, payload=payload)
    finally:
        db.rollback()
        db.close()
    assert fetch_one("SELECT count(*) FROM feedback WHERE user_id = :u", u=s.user_id)[0] == 0
    assert fetch_one("SELECT count(*) FROM points_log WHERE user_id = :u", u=s.user_id)[0] == 0


def test_exact_dedupe_conflict_is_recovered_by_savepoint_not_by_broad_except() -> None:
    """Source-level confirmation alongside the behavioural tests above:
    the reward `except IntegrityError` block re-raises unless the
    translated exception is specifically `DuplicateFeedbackRewardError`
    — it is never a bare `except IntegrityError: pass` or similar
    catch-all."""
    source = inspect.getsource(submit_feedback)
    assert "except IntegrityError as err:" in source
    assert "if not isinstance(translated, DuplicateFeedbackRewardError):" in source
    assert "raise translated from err" in source
    assert "except IntegrityError:\n        pass" not in source


# --------------------------------------------------------------------------
# Total and ledger invariants
# --------------------------------------------------------------------------


def test_total_points_is_derived_from_the_ledger_not_cached(
    scenario: Callable[..., Scenario],
) -> None:
    s = scenario()
    # A task award from a different source, inserted directly, proves
    # the total includes EVERY ledger entry, not just feedback rows.
    execute(
        "INSERT INTO points_log (user_id, booth_id, source, points, entered_by_staff_id, "
        "entered_by_system) SELECT :u, :b, 'TASK', 15, id, false FROM staff_accounts "
        "WHERE email = 'aiworks@staff.facemark.demo'",
        u=s.user_id,
        b=s.booth_id,
    )
    response = _feedback(s)
    assert response.json()["total_points"] == 15 + SEEDED_FEEDBACK_POINTS


def test_no_mutable_total_points_column_exists() -> None:
    row = fetch_one(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'users' AND column_name = 'total_points'"
    )
    assert row is not None
    assert row[0] == 0
