"""Aggregate pacing of OTP email sends (app/modules/auth/otp_throttle.py).

No real email, no real clock: the throttle takes an injected clock and the
provider is the in-memory `FakeProvider`.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

import app.worker as worker_module
from app.core.config import Settings
from app.db.session import SessionLocal
from app.modules.auth import otp_outbox as ob
from app.modules.auth.otp_provider import SendResult
from app.modules.auth.otp_throttle import (
    DEFAULT_PAUSE_SECONDS,
    MAX_PAUSE_SECONDS,
    SendThrottle,
)
from app.worker import run_forever, run_once
from tests.outbox_support import (
    FakeProvider,
    accepted,
    clean_outbox,
    make_delivery,
    outbox_settings,
    row,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _outbox(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    settings = outbox_settings()
    monkeypatch.setattr("app.modules.auth.otp_outbox.get_settings", lambda: settings)
    clean_outbox()
    yield
    clean_outbox()


def rate_limited(retry_after: float | None) -> SendResult:
    return SendResult(
        False,
        reason_code="provider_rate_limited",
        retryable=True,
        http_status=429,
        retry_after_seconds=retry_after,
    )


# --- the throttle by itself ---------------------------------------------------------


def test_the_window_slides_and_never_allows_more_than_the_limit() -> None:
    clock = Clock()
    throttle = SendThrottle(3, 2.0, clock=clock)
    assert throttle.available() == 3
    throttle.reserve(2)
    assert throttle.available() == 1
    clock.now += 1.0
    throttle.reserve(1)
    assert throttle.available() == 0
    assert throttle.wait_seconds() == pytest.approx(1.0)  # the first two leave in 1 s
    clock.now += 1.0
    assert throttle.available() == 2
    assert throttle.wait_seconds() == 0.0


def test_a_pause_blocks_everything_and_a_shorter_one_cannot_shorten_it() -> None:
    clock = Clock()
    throttle = SendThrottle(5, 1.0, clock=clock)
    assert throttle.pause(30) == pytest.approx(30)
    assert throttle.available() == 0
    assert throttle.wait_seconds() == pytest.approx(30)
    throttle.pause(5)
    assert throttle.wait_seconds() == pytest.approx(30)
    clock.now += 30.5
    assert throttle.available() == 5


def test_a_pause_is_capped_and_a_missing_hint_gets_the_default() -> None:
    throttle = SendThrottle(1, 1.0, clock=Clock())
    assert throttle.pause(10_000) == pytest.approx(MAX_PAUSE_SECONDS)
    other = SendThrottle(1, 1.0, clock=Clock())
    assert other.pause(None) == pytest.approx(DEFAULT_PAUSE_SECONDS)
    assert other.pause(0) == pytest.approx(DEFAULT_PAUSE_SECONDS)


@pytest.mark.parametrize(("limit", "window"), [(0, 1.0), (1, 0.0), (1, -1.0)])
def test_nonsense_limits_are_refused(limit: int, window: float) -> None:
    with pytest.raises(ValueError):
        SendThrottle(limit, window)


def test_the_limit_is_configurable_and_bounded() -> None:
    base: dict[str, Any] = {
        "environment": "development",
        "database_url": "postgresql+psycopg://u:p@db:5432/d",
        "app_origin": "http://localhost:3000",
        "scan_ref_secret": "dev-secret",
    }
    defaults = Settings(**base)
    assert (defaults.otp_delivery_rate_limit, defaults.otp_delivery_rate_window_seconds) == (2, 1)
    tuned = Settings(**base, otp_delivery_rate_limit=5, otp_delivery_rate_window_seconds=10)
    assert (tuned.otp_delivery_rate_limit, tuned.otp_delivery_rate_window_seconds) == (5, 10)
    refused: tuple[dict[str, Any], ...] = (
        {"otp_delivery_rate_limit": 0},
        {"otp_delivery_rate_window_seconds": 0},
    )
    for bad in refused:
        with pytest.raises(ValueError):
            Settings(**{**base, **bad})


# --- the worker keeps to it ----------------------------------------------------------


def test_a_registration_burst_drains_at_the_configured_rate() -> None:
    ids = [make_delivery()[1] for _ in range(7)]
    clock = Clock()
    throttle = SendThrottle(3, 1.0, clock=clock)
    provider = FakeProvider(accepted())

    per_second: list[int] = []
    for _second in range(4):
        before = len(provider.messages)
        for _tick in range(5):  # many polls inside one second: still capped
            run_once(provider=provider, worker_id="w", throttle=throttle)
        per_second.append(len(provider.messages) - before)
        clock.now += 1.0

    assert per_second == [3, 3, 1, 0]
    assert all(row(i)["status"] == "accepted" for i in ids)


def test_a_throttled_worker_claims_nothing_and_holds_no_lease() -> None:
    clock = Clock()
    throttle = SendThrottle(1, 60.0, clock=clock)
    throttle.reserve(1)
    _, delivery_id, _ = make_delivery()
    provider = FakeProvider(accepted())

    assert run_once(provider=provider, worker_id="w", throttle=throttle) == 0

    r = row(delivery_id)
    assert r["status"] == "pending" and r["attempt_count"] == 0 and r["locked_by"] is None
    assert provider.messages == []


def test_a_429_pauses_the_whole_worker_so_the_rest_of_the_queue_keeps_its_attempts() -> None:
    ids = [make_delivery()[1] for _ in range(4)]
    clock = Clock()
    throttle = SendThrottle(10, 1.0, clock=clock)
    provider = FakeProvider(rate_limited(30), accepted())

    run_once(provider=provider, worker_id="w", throttle=throttle, concurrency=1)
    assert len(provider.messages) == 1
    assert throttle.state().paused_for_seconds == pytest.approx(30)

    for _ in range(5):  # nothing else is attempted while the provider asked for quiet
        run_once(provider=provider, worker_id="w", throttle=throttle, concurrency=1)
    assert len(provider.messages) == 1
    untouched = [row(i) for i in ids if row(i)["attempt_count"] == 0]
    assert len(untouched) == 3  # no attempt wasted on a limit we already knew about

    clock.now += 31
    run_once(provider=provider, worker_id="w", throttle=throttle, concurrency=1)
    assert len(provider.messages) == 2


def test_the_row_honours_a_long_retry_after_instead_of_coming_back_after_60_seconds() -> None:
    _, delivery_id, _ = make_delivery()
    throttle = SendThrottle(5, 1.0, clock=Clock())
    run_once(provider=FakeProvider(rate_limited(120)), worker_id="w", throttle=throttle)

    r = row(delivery_id)
    assert r["status"] == "pending" and r["reason_code"] == "provider_rate_limited"
    assert r["next_attempt_at"] >= datetime.now(UTC) + timedelta(seconds=115)


def test_a_retry_after_that_outlives_the_code_ends_the_row_instead_of_retrying() -> None:
    _, delivery_id, _ = make_delivery(ttl=timedelta(minutes=5))
    throttle = SendThrottle(5, 1.0, clock=Clock())
    run_once(provider=FakeProvider(rate_limited(3600)), worker_id="w", throttle=throttle)

    r = row(delivery_id)
    assert r["status"] == "expired" and r["reason_code"] == ob.REASON_OTP_EXPIRED
    assert r["payload_ciphertext"] is None
    # ...but the worker-wide pause is capped, so it looks again soon
    assert throttle.state().paused_for_seconds == pytest.approx(MAX_PAUSE_SECONDS)


def test_without_a_retry_after_the_normal_backoff_applies() -> None:
    _, delivery_id, _ = make_delivery()
    throttle = SendThrottle(5, 1.0, clock=Clock())
    run_once(provider=FakeProvider(rate_limited(None)), worker_id="w", throttle=throttle)

    r = row(delivery_id)
    wait = (r["next_attempt_at"] - datetime.now(UTC)).total_seconds()
    assert r["status"] == "pending" and ob.BACKOFF_SECONDS[0] - 1 <= wait <= ob.BACKOFF_SECONDS[0]
    assert throttle.state().paused_for_seconds == pytest.approx(DEFAULT_PAUSE_SECONDS)


def test_a_superseded_code_costs_no_send_slot_and_no_provider_call() -> None:
    """A user who asks for a new code while the old one still waits in a long
    queue must not use up the provider's quota twice."""
    challenge_id, old_id, address = make_delivery()
    db = SessionLocal()
    try:  # issuing a newer code replaces (consumes) the older challenge
        db.execute(
            text("UPDATE otp_challenges SET consumed_at = now() WHERE id = :c"), {"c": challenge_id}
        )
        db.commit()
    finally:
        db.close()
    make_delivery(email=address)  # the newer code, same address
    clock = Clock()
    throttle = SendThrottle(1, 1.0, clock=clock)
    provider = FakeProvider(accepted())

    run_once(provider=provider, worker_id="w", throttle=throttle, concurrency=1)
    assert row(old_id)["reason_code"] == ob.REASON_CHALLENGE_CONSUMED
    assert provider.messages == []  # skipped without calling the provider
    assert throttle.available() == 1  # and it cost no send slot


# --- waiting happens outside every transaction ---------------------------------------


class _StopAfter:
    """A stand-in for threading.Event: records every wait, and stops the loop
    after `limit` of them. `on_wait` runs while the worker is "waiting"."""

    def __init__(self, limit: int, on_wait: Any) -> None:
        self.waits: list[float] = []
        self._limit = limit
        self._on_wait = on_wait
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout or 0.0)
        self._on_wait()
        if len(self.waits) >= self._limit:
            self._set = True
        return self._set


class _OneTurn(threading.Event):
    """An Event whose first wait ends the loop: exactly one turn, no real waiting."""

    def wait(self, timeout: float | None = None) -> bool:
        self.set()
        return True


def _idle_in_transaction() -> int:
    db = SessionLocal()
    try:
        return int(
            db.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"
                )
            ).scalar_one()
        )
    finally:
        db.close()


def test_a_throttled_worker_waits_with_no_transaction_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(worker_module, "HEARTBEAT_FILE", tmp_path / "hb")
    monkeypatch.setattr(worker_module, "STATUS_FILE", tmp_path / "status.json")
    ids = [make_delivery()[1] for _ in range(3)]
    throttle = SendThrottle(1, 3600.0)  # one send, then a long wait
    seen: list[int] = []
    stop = _StopAfter(2, lambda: seen.append(_idle_in_transaction()))

    run_forever(
        stop,  # type: ignore[arg-type]
        provider=FakeProvider(accepted()),
        worker_id="w",
        throttle=throttle,
    )

    assert seen and set(seen) == {0}  # nothing idle-in-transaction while it waited
    assert all(wait > 0 for wait in stop.waits)
    assert sum(row(i)["status"] == "accepted" for i in ids) == 1
    pending = [row(i) for i in ids if row(i)["status"] == "pending"]
    assert len(pending) == 2 and all(p["locked_by"] is None for p in pending)


def test_the_worker_publishes_a_status_file_with_numbers_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    status = tmp_path / "status.json"
    monkeypatch.setattr(worker_module, "HEARTBEAT_FILE", tmp_path / "hb")
    monkeypatch.setattr(worker_module, "STATUS_FILE", status)
    _, _, address = make_delivery(code="918273")
    stop = _OneTurn()

    run_forever(
        stop,
        provider=FakeProvider(accepted()),
        worker_id="w",
        throttle=SendThrottle(2, 1.0),
    )

    text_ = status.read_text(encoding="utf-8")
    assert '"loop_at"' in text_ and '"consecutive_failures": 0' in text_
    assert address not in text_ and "918273" not in text_
