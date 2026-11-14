"""M14 acceptance: realtime invalidation and reconciliation —
ARCHITECTURE.md §17, §19.3, §19.6, invariants #10, #12, #17, #18, #22,
#27, #28, IMPLEMENTATION_PLAN.md M14.

Real HTTP streaming throughout, against a REAL running server — a
`uvicorn.Server` started in a background thread of this test process,
bound to an ephemeral localhost port, hit with a real `httpx.Client`.
A mocked `EventSource` proves nothing about the actual wire protocol,
timing, or the sync-thread -> async-queue bridge (`call_soon_threadsafe`)
the broker depends on; only a real server exercises all of that.

Two DIFFERENT httpx.Client instances are used whenever a test needs a
concurrent stream-read and a concurrent mutation — reusing a single
client for both was found, empirically, to serialize on Starlette's
`TestClient` portal (deadlock); a real server has no such limitation,
but this file keeps the separate-client pattern throughout for clarity
and because it also matches two different real devices.

`rig` builds real, committed fixtures exactly like `test_leaderboard.py`
does — ACTIVE attendees via the real M04 registration+OTP flow, booths
and points_log rows via direct SQL where M11/M12's own endpoints would
be slower or less precise than needed — and tears every row down in
FK-safe order.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy import text

from app.core.config import get_settings
from app.core.security import generate_session_token, hash_password, hash_session_token
from app.db.session import SessionLocal
from app.main import app
from app.modules.realtime import service as realtime_service
from app.modules.realtime.broker import event_bus
from app.modules.staff.scan_ref import issue_scan_ref
from tests.m04_support import execute, fetch_one, new_email, register_payload

OTP_LOGGER = "app.modules.auth.otp_delivery"
TEST_PASSWORD_HASH = hash_password("m14-test-only-staff-password")  # noqa: S106
SCAN_REF_SECRET = get_settings().scan_ref_secret
SEEDED_FEEDBACK_POINTS = 5  # app/seed/demo_fixtures.py's event_config default


def _insert_returning_id(sql: str, **params: Any) -> int:
    db = SessionLocal()
    try:
        row: Any = db.execute(text(sql), params).scalar_one()
        db.commit()
        return int(row)
    finally:
        db.close()


# --------------------------------------------------------------------------
# A real, running server
# --------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


@pytest.fixture(scope="module")
def live_server() -> Iterator[str]:
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "live test server failed to start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _client(
    live_server: str, cookies: dict[str, str] | None = None, *, read_timeout: float = 20.0
) -> httpx.Client:
    return httpx.Client(
        base_url=live_server,
        headers={"Origin": "http://localhost:3000"},
        cookies=cookies or {},
        timeout=httpx.Timeout(read_timeout, connect=5.0),
    )


def _iter_sse_events(resp: httpx.Response) -> Iterator[tuple[str, dict[str, Any]]]:
    """Assembles raw SSE lines into `(event, data)` pairs. A blocking
    generator — the caller's `httpx.Client(timeout=...)` is what bounds
    how long a `next()` call may wait before raising `httpx.ReadTimeout`
    (used deliberately by tests that assert "nothing more arrives")."""
    event_name: str | None = None
    data: dict[str, Any] | None = None
    for line in resp.iter_lines():
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data = json.loads(line[len("data:") :].strip())
        elif line == "" and event_name is not None:
            yield event_name, (data or {})
            event_name, data = None, None


# --------------------------------------------------------------------------
# The `rig` fixture — real attendees/booths/staff/points_log, self-cleaning
# --------------------------------------------------------------------------


@dataclass
class Rig:
    live_server: str
    otp_log: pytest.LogCaptureFixture
    user_ids: list[int] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    booth_ids: list[int] = field(default_factory=list)
    staff_ids: list[int] = field(default_factory=list)

    def booth(self) -> int:
        slug = f"m14-test-booth-{uuid.uuid4().hex[:10]}"
        booth_id = _insert_returning_id(
            "INSERT INTO booths (slug, name) VALUES (:slug, :name) RETURNING id",
            slug=slug,
            name=f"M14 Test Booth {slug[-6:]}",
        )
        # Every booth gets one 5-point activity: awards name an activity.
        _insert_returning_id(
            "INSERT INTO tasks (booth_id, title, points) VALUES (:b, 'M14 activity', 5) "
            "RETURNING id",
            b=booth_id,
        )
        self.booth_ids.append(booth_id)
        return booth_id

    def staff(self, booth_id: int) -> tuple[int, int, str]:
        """Returns (staff_id, session_id, raw session token)."""
        email = f"m14-{uuid.uuid4().hex[:10]}@staff.test.facemark.demo"
        staff_id = _insert_returning_id(
            "INSERT INTO staff_accounts (email, password_hash, full_name, role, booth_id) "
            "VALUES (:email, :hash, 'M14 Test Staff', 'BOOTH_STAFF', :booth_id) RETURNING id",
            email=email,
            hash=TEST_PASSWORD_HASH,
            booth_id=booth_id,
        )
        self.staff_ids.append(staff_id)
        raw = generate_session_token()
        session_id = _insert_returning_id(
            "INSERT INTO sessions (token_hash, principal_type, staff_account_id, issued_at, "
            "expires_at) VALUES (:h, 'STAFF', :sid, now(), now() + interval '12 hours') "
            "RETURNING id",
            h=hash_session_token(raw),
            sid=staff_id,
        )
        return staff_id, session_id, raw

    def card_token(self, user_id: int) -> str:
        row = fetch_one(
            "SELECT public_token FROM contact_cards WHERE user_id = :u AND revoked_at IS NULL",
            u=user_id,
        )
        assert row is not None
        return str(row[0])

    def active_attendee(self, **overrides: Any) -> tuple[int, str]:
        """Returns (user_id, email) — a real ACTIVE attendee via the
        actual M04 registration+OTP-verify flow, exactly like
        `test_leaderboard.py`'s own `rig`."""
        email = new_email("m14")
        with _client(self.live_server) as c:
            assert (
                c.post(
                    "/api/attendees/register", json=register_payload(email, **overrides)
                ).status_code
                == 200
            )
            code = _captured_otp(self.otp_log.records, email)
            assert (
                c.post("/api/auth/otp/verify", json={"email": email, "code": code}).status_code
                == 200
            )
        row = fetch_one("SELECT id FROM users WHERE email = :e", e=email.lower())
        assert row is not None
        user_id = int(row[0])
        self.user_ids.append(user_id)
        self.emails.append(email)
        return user_id, email

    def provisional_attendee(self, **overrides: Any) -> tuple[int, str]:
        email = new_email("m14")
        with _client(self.live_server) as c:
            assert (
                c.post(
                    "/api/attendees/register", json=register_payload(email, **overrides)
                ).status_code
                == 200
            )
        row = fetch_one("SELECT id FROM users WHERE email = :e", e=email.lower())
        assert row is not None
        user_id = int(row[0])
        self.user_ids.append(user_id)
        self.emails.append(email)
        return user_id, email

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


def _captured_otp(records: list[logging.LogRecord], email: str, purpose: str = "ACTIVATION") -> str:
    for record in reversed(records):
        args = record.args
        if (
            record.name == OTP_LOGGER
            and isinstance(args, tuple)
            and len(args) == 3
            and args[0] == email.lower()
            and args[1] == purpose
        ):
            return str(args[2])
    raise AssertionError(f"no {purpose} OTP was delivered for {email}")


@pytest.fixture
def rig(live_server: str, otp_log: pytest.LogCaptureFixture) -> Iterator[Rig]:
    r = Rig(live_server=live_server, otp_log=otp_log)
    yield r
    # idempotency_records FKs to points_log — must go first.
    for staff_id in reversed(r.staff_ids):
        execute("DELETE FROM idempotency_records WHERE staff_account_id = :s", s=staff_id)
    for uid in reversed(r.user_ids):
        execute("DELETE FROM points_log WHERE user_id = :u", u=uid)
        execute("DELETE FROM feedback WHERE user_id = :u", u=uid)
        execute("DELETE FROM sessions WHERE user_id = :u", u=uid)
        execute("DELETE FROM consent_audit WHERE user_id = :u", u=uid)
        execute("DELETE FROM contact_cards WHERE user_id = :u", u=uid)
        execute("DELETE FROM user_interests WHERE user_id = :u", u=uid)
    for email in reversed(r.emails):
        execute("DELETE FROM otp_challenges WHERE email = :e", e=email)
    for uid in reversed(r.user_ids):
        execute("DELETE FROM users WHERE id = :u", u=uid)
    for staff_id in reversed(r.staff_ids):
        execute("DELETE FROM points_log WHERE entered_by_staff_id = :s", s=staff_id)
        execute("DELETE FROM sessions WHERE staff_account_id = :s", s=staff_id)
        execute("DELETE FROM staff_accounts WHERE id = :s", s=staff_id)
    for booth_id in reversed(r.booth_ids):
        execute("DELETE FROM points_log WHERE booth_id = :b", b=booth_id)
        execute("DELETE FROM feedback WHERE booth_id = :b", b=booth_id)
        execute("DELETE FROM tasks WHERE booth_id = :b", b=booth_id)
        execute("DELETE FROM booths WHERE id = :b", b=booth_id)


def _error(response: httpx.Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def _next_non_heartbeat(
    events: Iterator[tuple[str, dict[str, Any]]],
) -> tuple[str, dict[str, Any]]:
    """Skips `heartbeat` frames. The 15 s default interval is long
    enough that no test should normally see one mid-assertion, but
    under real scheduling/CPU contention a test occasionally CAN run
    long enough for one incidental heartbeat tick to interleave — that
    is a timing coincidence the test has no business caring about, not
    a real assertion failure."""
    while True:
        event, data = next(events)
        if event != "heartbeat":
            return event, data


def _collect_non_heartbeat(
    events: Iterator[tuple[str, dict[str, Any]]], count: int
) -> list[tuple[str, dict[str, Any]]]:
    return [_next_non_heartbeat(events) for _ in range(count)]


def _task_award(
    live_server: str,
    *,
    staff_token: str,
    staff_id: int,
    session_id: int,
    card_token: str,
    idempotency_key: uuid.UUID | None = None,
) -> httpx.Response:
    """A real `POST /api/staff/points/award` — the same request shape
    `test_points_award.py` uses, reproduced here rather than imported
    (this codebase's test files are each self-contained)."""
    scan_ref = issue_scan_ref(
        secret=SCAN_REF_SECRET,
        staff_id=staff_id,
        session_id=session_id,
        card_token=card_token,
        now=datetime.now(UTC),
    )
    task_id = fetch_one(
        "SELECT t.id FROM tasks t JOIN staff_accounts s ON s.booth_id = t.booth_id "
        "WHERE s.id = :s ORDER BY t.id LIMIT 1",
        s=staff_id,
    )[0]
    from tests.m04_support import rule_revision_in_force

    body = {
        "card_token": card_token,
        "task_id": task_id,
        "rule_revision": rule_revision_in_force(task_id),
        "idempotency_key": str(idempotency_key or uuid.uuid4()),
        "scan_ref": scan_ref,
    }
    with _client(live_server, {"session": staff_token}) as c:
        response: httpx.Response = c.post("/api/staff/points/award", json=body)
        return response


# --------------------------------------------------------------------------
# Auth / connectivity
# --------------------------------------------------------------------------


def test_unauthenticated_stream_returns_a_clean_401_not_a_broken_stream(rig: Rig) -> None:
    with _client(rig.live_server) as c:
        response = c.get("/api/realtime/stream")
    assert _error(response) == (401, "UNAUTHENTICATED")
    assert response.headers["content-type"].startswith("application/json")


def test_expired_session_is_rejected_at_connect(rig: Rig) -> None:
    uid, _ = rig.active_attendee()
    token = rig.session(uid, expired=True)
    with _client(rig.live_server, {"session": token}) as c:
        response = c.get("/api/realtime/stream")
    assert _error(response) == (401, "UNAUTHENTICATED")


def test_revoked_session_is_rejected_at_connect(rig: Rig) -> None:
    uid, _ = rig.active_attendee()
    token = rig.session(uid, revoked=True)
    with _client(rig.live_server, {"session": token}) as c:
        response = c.get("/api/realtime/stream")
    assert _error(response) == (401, "UNAUTHENTICATED")


def test_provisional_attendee_is_rejected_at_connect(rig: Rig) -> None:
    uid, _ = rig.provisional_attendee()
    token = rig.session(uid)
    with _client(rig.live_server, {"session": token}) as c:
        response = c.get("/api/realtime/stream")
    assert _error(response) == (403, "REGISTRATION_NOT_VERIFIED")


def test_staff_session_cannot_authenticate_the_attendee_stream(rig: Rig) -> None:
    booth_id = rig.booth()
    _, _, staff_token = rig.staff(booth_id)
    with _client(rig.live_server, {"session": staff_token}) as c:
        response = c.get("/api/realtime/stream")
    assert _error(response) == (401, "UNAUTHENTICATED")


def test_no_query_parameter_exists_to_choose_another_users_channel(rig: Rig) -> None:
    """§9.3/invariant #4's spirit, extended to realtime identity: `?user_id=`
    is simply an unrecognised, ignored query string — there is no code
    path anywhere in `realtime/routers.py` that reads a client-supplied
    identity. Proved structurally (a forged id never subscribes the
    caller to anyone else's channel) rather than merely by absence of
    a parameter."""
    a_uid, _ = rig.active_attendee()
    other_uid, _ = rig.active_attendee()
    token = rig.session(a_uid)
    with (
        _client(rig.live_server, {"session": token}) as c,
        c.stream("GET", f"/api/realtime/stream?user_id={other_uid}") as resp,
    ):
        event, _data = next(_iter_sse_events(resp))
        assert event == "ready"
        assert event_bus.subscriber_count(a_uid) == 1
        assert event_bus.subscriber_count(other_uid) == 0


# --------------------------------------------------------------------------
# `ready` event / headers
# --------------------------------------------------------------------------


def test_ready_event_carries_dataset_generation_and_heartbeat_seconds(rig: Rig) -> None:
    uid, _ = rig.active_attendee()
    token = rig.session(uid)
    row = fetch_one("SELECT dataset_generation FROM event_config WHERE id = 1")
    assert row is not None
    current_generation = int(row[0])
    with (
        _client(rig.live_server, {"session": token}) as c,
        c.stream("GET", "/api/realtime/stream") as resp,
    ):
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        event, data = next(_iter_sse_events(resp))
    assert event == "ready"
    assert data == {"dataset_generation": current_generation, "heartbeat_seconds": 15}


def test_stream_response_carries_proxy_buffering_and_generation_headers(rig: Rig) -> None:
    uid, _ = rig.active_attendee()
    token = rig.session(uid)
    with (
        _client(rig.live_server, {"session": token}) as c,
        c.stream("GET", "/api/realtime/stream") as resp,
    ):
        assert resp.headers["cache-control"] == "no-cache, no-transform"
        assert resp.headers["x-accel-buffering"] == "no"
        assert "x-dataset-generation" in resp.headers
        next(_iter_sse_events(resp))  # drain `ready` so the `with` exits cleanly


def test_health_and_every_other_response_also_carries_the_generation_header(rig: Rig) -> None:
    """§19.3: "Every API response carries X-Dataset-Generation" — not
    scoped to realtime-specific endpoints."""
    with _client(rig.live_server) as c:
        health = c.get("/api/health")
        booths = c.get("/api/booths")
    assert "x-dataset-generation" in health.headers
    assert "x-dataset-generation" in booths.headers
    assert health.headers["x-dataset-generation"] == booths.headers["x-dataset-generation"]


# --------------------------------------------------------------------------
# Subscribe-before-snapshot
# --------------------------------------------------------------------------


def test_subscriber_is_registered_before_ready_is_sent(rig: Rig) -> None:
    uid, _ = rig.active_attendee()
    token = rig.session(uid)
    with (
        _client(rig.live_server, {"session": token}) as c,
        c.stream("GET", "/api/realtime/stream") as resp,
    ):
        events = _iter_sse_events(resp)
        # By the time `ready` is observed, the server had ALREADY
        # registered the subscriber queue — that registration is the
        # very first thing `stream()` does, strictly before its first
        # yield (`realtime/routers.py`).
        event, _data = next(events)
        assert event == "ready"
        assert event_bus.subscriber_count(uid) == 1


def test_an_award_landing_between_ready_and_the_snapshot_fetch_is_still_caught(rig: Rig) -> None:
    """The headline correctness property (§17.4): subscribe first, wait
    for `ready`, THEN fetch the snapshot. An award committing in that
    exact gap must be reflected in the snapshot (because it already
    committed by the time the snapshot is fetched) AND signalled on the
    stream (because the subscriber was already registered when it
    committed) — no window where it is missed by both."""
    booth_id = rig.booth()
    uid, _ = rig.active_attendee()
    token = rig.session(uid)

    with (
        _client(rig.live_server, {"session": token}) as c,
        c.stream("GET", "/api/realtime/stream") as resp,
    ):
        events = _iter_sse_events(resp)
        event, _data = next(events)
        assert event == "ready"

        # The award lands HERE — strictly after subscribe, strictly
        # before the snapshot fetch below. `rig.award` is a raw SQL
        # insert (M14 awards nothing itself) so the publish that would
        # normally follow a real M11/M12 commit is issued explicitly.
        rig.award(uid, booth_id, 15)
        realtime_service.publish_award_signals(uid)

        with _client(rig.live_server, {"session": token}) as snapshot_client:
            score = snapshot_client.get("/api/attendees/me/score")
        assert score.json()["total_points"] == 15

        event2, data2 = _next_non_heartbeat(events)
        assert (event2, data2) == ("state_changed", {"scope": "personal"})


def test_snapshot_before_subscribe_can_miss_the_hint_for_that_award(rig: Rig) -> None:
    """The negative control (M14 prompt: "snapshot-first incorrect
    control exposes race"): fetching the snapshot and THEN opening the
    stream means a subscriber that did not exist yet when the award
    published never receives that specific hint — this is exactly the
    gap §17.4's locked ordering exists to close, demonstrated here by
    doing it the wrong way round."""
    booth_id = rig.booth()
    uid, _ = rig.active_attendee()
    token = rig.session(uid)

    with _client(rig.live_server, {"session": token}) as snapshot_client:
        before = snapshot_client.get("/api/attendees/me/score")
    assert before.json()["total_points"] == 0

    # The award lands AFTER the (now stale) snapshot was fetched, and
    # BEFORE any subscriber exists to receive its publish. `rig.award`
    # is a raw SQL insert (M14 awards nothing itself) so the publish a
    # real M11/M12 commit would trigger is issued explicitly here.
    rig.award(uid, booth_id, 20)
    realtime_service.publish_award_signals(uid)

    with (
        _client(rig.live_server, {"session": token}, read_timeout=2.0) as c,
        c.stream("GET", "/api/realtime/stream") as resp,
    ):
        events = _iter_sse_events(resp)
        event, _data = next(events)
        assert event == "ready"
        # Nothing else arrives for this specific award — the hint was
        # published before this connection subscribed, so it was never
        # queued here. (The 20 s reconciliation floor is what recovers
        # this in the real client; this test only proves the gap
        # exists, which is exactly why that floor is mandatory.)
        with pytest.raises(httpx.ReadTimeout):
            next(events)


# --------------------------------------------------------------------------
# Post-commit publication (M11 task awards, M12 feedback rewards)
# --------------------------------------------------------------------------


def test_a_committed_task_award_publishes_personal_and_broadcast_signals(rig: Rig) -> None:
    booth_id = rig.booth()
    staff_id, session_id, staff_token = rig.staff(booth_id)
    uid, _ = rig.active_attendee()
    card_token = rig.card_token(uid)
    attendee_token = rig.session(uid)

    with (
        _client(rig.live_server, {"session": attendee_token}) as c,
        c.stream("GET", "/api/realtime/stream") as resp,
    ):
        events = _iter_sse_events(resp)
        assert next(events)[0] == "ready"

        award_response = _task_award(
            rig.live_server,
            staff_token=staff_token,
            staff_id=staff_id,
            session_id=session_id,
            card_token=card_token,
        )
        assert award_response.status_code == 200

        seen = _collect_non_heartbeat(events, 2)
    assert ("state_changed", {"scope": "personal"}) in seen
    assert any(e == "leaderboard_changed" and set(d) == {"leaderboard"} for e, d in seen)


def test_idempotent_task_award_replay_does_not_publish(rig: Rig) -> None:
    booth_id = rig.booth()
    staff_id, session_id, staff_token = rig.staff(booth_id)
    uid, _ = rig.active_attendee()
    card_token = rig.card_token(uid)
    attendee_token = rig.session(uid)
    key = uuid.uuid4()

    first = _task_award(
        rig.live_server,
        staff_token=staff_token,
        staff_id=staff_id,
        session_id=session_id,
        card_token=card_token,
        idempotency_key=key,
    )
    assert first.status_code == 200
    assert first.json()["replayed"] is False

    with (
        _client(rig.live_server, {"session": attendee_token}, read_timeout=2.0) as c,
        c.stream("GET", "/api/realtime/stream") as resp,
    ):
        events = _iter_sse_events(resp)
        assert next(events)[0] == "ready"

        replay = _task_award(
            rig.live_server,
            staff_token=staff_token,
            staff_id=staff_id,
            session_id=session_id,
            card_token=card_token,
            idempotency_key=key,
        )
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True

        # Nothing committed anew — nothing to publish.
        with pytest.raises(httpx.ReadTimeout):
            next(events)


def test_a_committed_first_feedback_reward_publishes_personal_and_broadcast_signals(
    rig: Rig,
) -> None:
    booth_id = rig.booth()
    row = fetch_one("SELECT slug FROM booths WHERE id = :b", b=booth_id)
    assert row is not None
    booth_slug = str(row[0])
    uid, _ = rig.active_attendee()
    token = rig.session(uid)

    with (
        _client(rig.live_server, {"session": token}) as c,
        c.stream("GET", "/api/realtime/stream") as resp,
    ):
        events = _iter_sse_events(resp)
        assert next(events)[0] == "ready"

        with _client(rig.live_server, {"session": token}) as fc:
            fb = fc.post("/api/attendees/feedback", json={"booth_slug": booth_slug, "rating": 5})
        assert fb.status_code == 200
        assert fb.json()["already_rewarded"] is False

        seen = _collect_non_heartbeat(events, 2)
    assert ("state_changed", {"scope": "personal"}) in seen
    assert any(e == "leaderboard_changed" and set(d) == {"leaderboard"} for e, d in seen)


def test_already_rewarded_feedback_does_not_publish(rig: Rig) -> None:
    booth_id = rig.booth()
    row = fetch_one("SELECT slug FROM booths WHERE id = :b", b=booth_id)
    assert row is not None
    booth_slug = str(row[0])
    uid, _ = rig.active_attendee()
    token = rig.session(uid)

    with _client(rig.live_server, {"session": token}) as fc:
        first = fc.post("/api/attendees/feedback", json={"booth_slug": booth_slug, "rating": 5})
    assert first.json()["already_rewarded"] is False

    with (
        _client(rig.live_server, {"session": token}, read_timeout=2.0) as c,
        c.stream("GET", "/api/realtime/stream") as resp,
    ):
        events = _iter_sse_events(resp)
        assert next(events)[0] == "ready"

        with _client(rig.live_server, {"session": token}) as fc2:
            second = fc2.post(
                "/api/attendees/feedback", json={"booth_slug": booth_slug, "rating": 2}
            )
        assert second.status_code == 200
        assert second.json()["already_rewarded"] is True

        with pytest.raises(httpx.ReadTimeout):
            next(events)


def test_publication_failure_does_not_roll_back_the_committed_mutation(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§17.7.2: "a failure to publish is logged and never fails the
    award." Forces `EventBus.publish_personal` to raise, and proves
    the feedback endpoint still returns its normal 200 with the reward
    correctly committed — publication is genuinely best-effort, not
    load-bearing for the mutation's own correctness."""
    booth_id = rig.booth()
    row = fetch_one("SELECT slug FROM booths WHERE id = :b", b=booth_id)
    assert row is not None
    booth_slug = str(row[0])
    uid, _ = rig.active_attendee()
    token = rig.session(uid)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated publish failure")

    monkeypatch.setattr(event_bus, "publish_personal", boom)

    with _client(rig.live_server, {"session": token}) as fc:
        response = fc.post("/api/attendees/feedback", json={"booth_slug": booth_slug, "rating": 4})

    assert response.status_code == 200
    body = response.json()
    assert body["already_rewarded"] is False
    assert body["awarded_points"] == SEEDED_FEEDBACK_POINTS

    row2 = fetch_one(
        "SELECT count(*) FROM points_log WHERE user_id = :u AND source = 'FEEDBACK'", u=uid
    )
    assert row2 is not None and int(row2[0]) == 1


# --------------------------------------------------------------------------
# Delivery robustness: dropped, duplicate, delayed hints are all harmless
# --------------------------------------------------------------------------


def test_a_slow_subscribers_queue_is_bounded_and_drops_the_oldest_hint() -> None:
    """M14 prompt: "a slow subscriber must never block an award
    transaction or grow memory without bound... dropped/coalesced
    invalidation hints are acceptable because snapshots restore
    correctness." A pure `EventBus` unit test — no HTTP, no live
    server — publishing far more hints than the bound without ever
    draining the queue."""
    import asyncio

    from app.modules.realtime.broker import QUEUE_MAXSIZE, EventBus

    async def run() -> None:
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        queue = bus.subscribe(999_999_1)
        total = QUEUE_MAXSIZE * 3
        for i in range(total):
            bus.publish_personal(999_999_1, "state_changed", {"i": i})
            await asyncio.sleep(0)  # let the scheduled call_soon_threadsafe callback run
        assert queue.qsize() <= QUEUE_MAXSIZE
        last = None
        while not queue.empty():
            last = queue.get_nowait()
        assert last is not None
        assert last.data["i"] == total - 1  # newest survives; oldest were dropped

    asyncio.run(run())


def test_publish_with_no_subscribers_and_no_bound_loop_is_a_harmless_no_op() -> None:
    """§17.7.2: best-effort. Before `bind_loop` (or after `unbind_loop`
    on shutdown), a publish must not raise — it is simply unable to
    reach anyone, which is always an acceptable outcome for a hint."""
    from app.modules.realtime.broker import EventBus

    bus = EventBus()
    bus.publish_personal(1, "state_changed", {"scope": "personal"})
    bus.publish_broadcast("leaderboard_changed", {})


def test_duplicate_and_rapid_hints_are_each_delivered_and_individually_harmless(rig: Rig) -> None:
    """Two (or more) hints in immediate succession are just independent
    "refetch" signals (§17.7.3: no event carries authoritative state,
    so nothing about receiving the same event type twice, or several
    in a burst, is meaningful beyond "refetch again")."""
    uid, _ = rig.active_attendee()
    token = rig.session(uid)
    with (
        _client(rig.live_server, {"session": token}) as c,
        c.stream("GET", "/api/realtime/stream") as resp,
    ):
        events = _iter_sse_events(resp)
        assert next(events)[0] == "ready"

        realtime_service.publish_award_signals(uid)
        realtime_service.publish_award_signals(uid)

        received = _collect_non_heartbeat(events, 4)
    assert received.count(("state_changed", {"scope": "personal"})) == 2
    assert sum(1 for e, d in received if e == "leaderboard_changed" and "leaderboard" in d) == 2


# --------------------------------------------------------------------------
# Heartbeat / connection lifecycle (§17.9)
# --------------------------------------------------------------------------


def test_heartbeat_arrives_on_schedule(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(realtime_service, "HEARTBEAT_SECONDS", 0.3)
    uid, _ = rig.active_attendee()
    token = rig.session(uid)
    with (
        _client(rig.live_server, {"session": token}, read_timeout=5.0) as c,
        c.stream("GET", "/api/realtime/stream") as resp,
    ):
        events = _iter_sse_events(resp)
        assert next(events)[0] == "ready"
        event, data = next(events)
    assert event == "heartbeat"
    assert "t" in data


def test_heartbeat_tick_revalidates_the_session_and_closes_on_revocation(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(realtime_service, "HEARTBEAT_SECONDS", 0.3)
    uid, _ = rig.active_attendee()
    token = rig.session(uid)
    with (
        _client(rig.live_server, {"session": token}, read_timeout=5.0) as c,
        c.stream("GET", "/api/realtime/stream") as resp,
    ):
        events = _iter_sse_events(resp)
        assert next(events)[0] == "ready"
        execute(
            "UPDATE sessions SET revoked_at = now() WHERE token_hash = :h",
            h=hash_session_token(token),
        )
        event, data = next(events)
        assert (event, data) == ("session_invalid", {"reason": "revoked"})
        with pytest.raises(StopIteration):
            next(events)  # the server closed the connection — nothing further


def test_heartbeat_tick_closes_the_stream_for_an_expired_session(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(realtime_service, "HEARTBEAT_SECONDS", 0.3)
    uid, _ = rig.active_attendee()
    token = rig.session(uid)
    with (
        _client(rig.live_server, {"session": token}, read_timeout=5.0) as c,
        c.stream("GET", "/api/realtime/stream") as resp,
    ):
        events = _iter_sse_events(resp)
        assert next(events)[0] == "ready"
        execute(
            "UPDATE sessions SET expires_at = now() - interval '1 second' WHERE token_hash = :h",
            h=hash_session_token(token),
        )
        event, data = next(events)
        assert (event, data) == ("session_invalid", {"reason": "revoked"})


def test_heartbeat_tick_closes_the_stream_for_a_deactivated_attendee(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deactivated/purged (§17.9: "same path"). No real endpoint
    reverts an ACTIVE attendee to PROVISIONAL — this exercises the
    enforcement directly, the same defence-in-depth technique M12/M13
    used for their own status checks."""
    monkeypatch.setattr(realtime_service, "HEARTBEAT_SECONDS", 0.3)
    uid, _ = rig.active_attendee()
    token = rig.session(uid)
    with (
        _client(rig.live_server, {"session": token}, read_timeout=5.0) as c,
        c.stream("GET", "/api/realtime/stream") as resp,
    ):
        events = _iter_sse_events(resp)
        assert next(events)[0] == "ready"
        execute("UPDATE users SET status = 'PROVISIONAL' WHERE id = :u", u=uid)
        event, data = next(events)
        assert (event, data) == ("session_invalid", {"reason": "revoked"})
    execute("UPDATE users SET status = 'ACTIVE' WHERE id = :u", u=uid)


def test_max_stream_age_closes_the_connection_with_no_further_events(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§17.9: "Streams are closed by the server after 30 minutes."
    Shrinks the max age well below the (unshrunk) heartbeat interval,
    so the close is unambiguously the age limit, not a heartbeat tick."""
    monkeypatch.setattr(realtime_service, "MAX_STREAM_AGE_SECONDS", 0.3)
    uid, _ = rig.active_attendee()
    token = rig.session(uid)
    with (
        _client(rig.live_server, {"session": token}, read_timeout=5.0) as c,
        c.stream("GET", "/api/realtime/stream") as resp,
    ):
        events = _iter_sse_events(resp)
        assert next(events)[0] == "ready"
        with pytest.raises(StopIteration):
            next(events)


# --------------------------------------------------------------------------
# Cleanup / backpressure / multiple tabs
# --------------------------------------------------------------------------


def test_subscriber_is_removed_from_the_broker_after_disconnect(rig: Rig) -> None:
    uid, _ = rig.active_attendee()
    token = rig.session(uid)
    with (
        _client(rig.live_server, {"session": token}) as c,
        c.stream("GET", "/api/realtime/stream") as resp,
    ):
        events = _iter_sse_events(resp)
        assert next(events)[0] == "ready"
        assert event_bus.subscriber_count(uid) == 1
    # The client has closed its end — poll briefly for the server's
    # own `finally: event_bus.unsubscribe(...)` to run.
    deadline = time.monotonic() + 3
    while event_bus.subscriber_count(uid) != 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert event_bus.subscriber_count(uid) == 0


def test_multiple_tabs_for_the_same_attendee_each_receive_the_signal(rig: Rig) -> None:
    booth_id = rig.booth()
    uid, _ = rig.active_attendee()
    token = rig.session(uid)
    with (
        _client(rig.live_server, {"session": token}) as c1,
        c1.stream("GET", "/api/realtime/stream") as resp1,
        _client(rig.live_server, {"session": token}) as c2,
        c2.stream("GET", "/api/realtime/stream") as resp2,
    ):
        events1 = _iter_sse_events(resp1)
        events2 = _iter_sse_events(resp2)
        assert next(events1)[0] == "ready"
        assert next(events2)[0] == "ready"
        assert event_bus.subscriber_count(uid) == 2

        rig.award(uid, booth_id, 5)
        realtime_service.publish_award_signals(uid)

        assert _next_non_heartbeat(events1) == ("state_changed", {"scope": "personal"})
        assert _next_non_heartbeat(events2) == ("state_changed", {"scope": "personal"})


# --------------------------------------------------------------------------
# Dataset generation
# --------------------------------------------------------------------------


def test_ready_generation_reflects_a_change_made_before_connecting(rig: Rig) -> None:
    """M14 does not implement reset (M15's job) — but the value itself
    is already live, not cached in this process (`current_dataset_generation`
    reads fresh every call), so a change made directly (standing in for
    a future M15 reset) is visible on the very next connection."""
    uid, _ = rig.active_attendee()
    token = rig.session(uid)
    execute("UPDATE event_config SET dataset_generation = dataset_generation + 1 WHERE id = 1")
    try:
        row = fetch_one("SELECT dataset_generation FROM event_config WHERE id = 1")
        assert row is not None
        bumped = int(row[0])
        with (
            _client(rig.live_server, {"session": token}) as c,
            c.stream("GET", "/api/realtime/stream") as resp,
        ):
            _event, data = next(_iter_sse_events(resp))
        assert data["dataset_generation"] == bumped
    finally:
        execute("UPDATE event_config SET dataset_generation = dataset_generation - 1 WHERE id = 1")


# --------------------------------------------------------------------------
# Privacy: no event payload carries points, totals, ranks, names, or PII -
# except `leaderboard_changed`, which pushes the public top-N view (load test:
# every phone refetching it after every award overloaded the single worker).
# That view is exactly what `GET /api/leaderboard` returns to any signed-in
# attendee, and the stream itself is attendee-only.
# --------------------------------------------------------------------------


_ALLOWED_EVENT_DATA_KEYS: dict[str, set[str]] = {
    "ready": {"dataset_generation", "heartbeat_seconds"},
    "state_changed": {"scope"},
    "leaderboard_changed": {"leaderboard"},
    "heartbeat": {"t"},
    "session_invalid": {"reason"},
    "dataset_reset": {"dataset_generation"},
}

_FORBIDDEN_SUBSTRINGS = (
    "point",
    "total",
    "rank",
    "name",
    "email",
    "phone",
    "linkedin",
    "feedback",
    "consent",
    "staff",
    "session",
    "token",
)


def test_every_event_payload_is_minimal_and_carries_no_attendee_data(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(realtime_service, "HEARTBEAT_SECONDS", 0.3)
    booth_id = rig.booth()
    uid, _ = rig.active_attendee()
    token = rig.session(uid)
    with (
        _client(rig.live_server, {"session": token}, read_timeout=5.0) as c,
        c.stream("GET", "/api/realtime/stream") as resp,
    ):
        events = _iter_sse_events(resp)
        assert next(events)[0] == "ready"
        rig.award(uid, booth_id, 5)
        realtime_service.publish_award_signals(uid)
        # state_changed, leaderboard_changed, heartbeat
        collected = [next(events) for _ in range(3)]

    for event, data in collected:
        allowed = _ALLOWED_EVENT_DATA_KEYS.get(event)
        assert allowed is not None, f"unexpected event type: {event}"
        assert set(data.keys()) <= allowed, (event, data)
        if event == "leaderboard_changed":
            _assert_public_leaderboard_push(data["leaderboard"])
            continue
        for key in data:
            assert not any(bad in key.lower() for bad in _FORBIDDEN_SUBSTRINGS), (event, data)


_PUBLIC_ENTRY_KEYS = {
    "user_id",
    "name",
    "profession",
    "total_points",
    "rank",
    "photo_url",
}


def _assert_public_leaderboard_push(pushed: Any) -> None:
    """Only the public top-N view: no email, phone, LinkedIn, consent or token."""
    assert set(pushed) == {"entries", "dataset_generation", "version"}
    for entry in pushed["entries"]:
        assert set(entry) <= _PUBLIC_ENTRY_KEYS, entry


def test_no_sse_id_field_is_ever_sent(rig: Rig) -> None:
    """§17.5: "Events deliberately have no `id:` field, so `EventSource`
    never sends `Last-Event-ID`." Checked against the RAW wire bytes,
    not just the parsed helper (which only looks for `event:`/`data:`
    lines and would silently ignore a stray `id:` line)."""
    uid, _ = rig.active_attendee()
    token = rig.session(uid)
    with (
        _client(rig.live_server, {"session": token}) as c,
        c.stream("GET", "/api/realtime/stream") as resp,
    ):
        raw_lines: list[str] = []
        for line in resp.iter_lines():
            raw_lines.append(line)
            if line == "":
                break  # end of the first (ready) frame
    assert not any(line.startswith("id:") for line in raw_lines)


def test_no_points_log_id_is_used_as_a_cursor_anywhere_in_realtime(rig: Rig) -> None:
    """Invariant #22, structurally: `points_log.id` never appears in
    `realtime/`'s source at all — not as a query parameter name, not
    in a comparison, not in an SSE `id:` field. Source-level, alongside
    the wire-level proof above, the same two-pronged technique M12
    used for its own "never a bare except" invariant."""
    import inspect

    from app.modules.realtime import broker, routers, service

    for module in (broker, routers, service):
        source = inspect.getsource(module)
        assert "points_log" not in source.lower()
        assert "last_event_id" not in source.lower()
        assert "last-event-id" not in source.lower()


# --------------------------------------------------------------------------
# Realtime unavailable -> authoritative HTTP still works
# --------------------------------------------------------------------------


def test_score_and_leaderboard_endpoints_work_even_with_the_broker_loop_unbound(
    rig: Rig,
) -> None:
    """§17.7/§17.8: the realtime channel is purely additive latency
    optimisation. With the broker's loop unbound (standing in for "the
    stream subsystem is unavailable" — e.g. between a restart and the
    next request), the authoritative HTTP snapshots must be completely
    unaffected."""
    uid, _ = rig.active_attendee()
    token = rig.session(uid)
    saved_loop = event_bus._loop  # noqa: SLF001 - test-only introspection
    event_bus.unbind_loop()
    try:
        with _client(rig.live_server, {"session": token}) as c:
            score = c.get("/api/attendees/me/score")
            leaderboard = c.get("/api/leaderboard")
        assert score.status_code == 200
        assert leaderboard.status_code == 200
    finally:
        event_bus._loop = saved_loop  # noqa: SLF001 - restore for later tests in this module
