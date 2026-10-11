"""The OTP email worker: `python -m app.worker`.

A long-running process, separate from the web server, that drains the
`otp_deliveries` outbox (app/modules/auth/otp_outbox.py). Run exactly the
processes you like - several workers are safe: rows are claimed with
`FOR UPDATE SKIP LOCKED` and a lease, so no row is sent by two at once, and
a worker that dies simply stops renewing its rows' leases.

Behaviour
---------
* Polls every `POLL_SECONDS`; claims up to `CONCURRENCY` due rows and sends
  them in parallel threads (each with its own short transactions - no
  database transaction is held open across the provider call).
* Every loop also does maintenance: expire rows whose code has lapsed, give
  up on rows whose workers keep dying.
* Roughly hourly it deletes finished rows past the retention period.
* SIGTERM/SIGINT: finish the batch in hand, then exit 0.
* Writes a heartbeat file so the container health check can tell a wedged
  worker from a live one.
* Logs ids, states and reason codes only - never an address, a code, the
  API key, or anything a provider said.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal, mutating_transaction
from app.modules.auth import otp_outbox
from app.modules.auth.otp_provider import EmailProvider, ResendProvider
from app.modules.auth.otp_throttle import SendThrottle

logger = logging.getLogger("facemark.worker")

POLL_SECONDS = 1.0
IDLE_POLL_SECONDS = 5.0  # when there is nothing to do and nothing configured
CONCURRENCY = 2  # modest: providers rate-limit (Resend's default is 2 requests/second)
PURGE_EVERY_SECONDS = 3600.0
HEARTBEAT_FILE = Path(os.environ.get("OTP_WORKER_HEARTBEAT_FILE", "/tmp/otp-worker.heartbeat"))  # noqa: S108
# What the heartbeat file cannot say: is the loop merely turning, or is it getting
# anywhere? Read by `python -m app.cli ops-check --worker-heartbeat`. Numbers only -
# no address, code or provider text ever goes in it.
STATUS_FILE = Path(os.environ.get("OTP_WORKER_STATUS_FILE", "/tmp/otp-worker.status.json"))  # noqa: S108


def new_worker_id() -> str:
    return f"{os.uname().nodename if hasattr(os, 'uname') else 'worker'}-{uuid.uuid4().hex[:8]}"


def _heartbeat() -> None:
    with contextlib.suppress(OSError):
        HEARTBEAT_FILE.touch()


def write_status(**fields: float | int | str | None) -> None:
    """Atomically replaces the status file (a reader never sees half of it)."""
    with contextlib.suppress(OSError):
        temp = STATUS_FILE.with_name(STATUS_FILE.name + ".tmp")
        temp.write_text(json.dumps(fields), encoding="utf-8")
        os.replace(temp, STATUS_FILE)


def run_once(
    *,
    provider: EmailProvider,
    worker_id: str,
    session_factory: Callable[[], Session] = SessionLocal,
    concurrency: int = CONCURRENCY,
    pool: ThreadPoolExecutor | None = None,
    throttle: SendThrottle | None = None,
) -> int:
    """One poll: maintenance, claim, send. Returns how many rows were
    attempted. Exposed for tests and for `python -m app.cli`-style tooling.

    With a `throttle`, at most as many rows are claimed as it allows right now,
    and none while the provider has asked us to wait. That decision is made
    BEFORE claiming, with no database session open: a throttled worker holds no
    lease and no lock, it simply does not take rows yet."""
    otp_outbox.maintain(session_factory)
    limit = concurrency
    if throttle is not None:
        limit = min(concurrency, throttle.available())
        if limit < 1:
            return 0
    claimed = otp_outbox.claim_next(session_factory, worker_id=worker_id, limit=limit)
    if not claimed:
        return 0
    if throttle is not None:
        throttle.reserve(len(claimed))

    def work(item: otp_outbox.ClaimedDelivery) -> str:
        return otp_outbox.process_delivery(
            session_factory, item, provider=provider, worker_id=worker_id, throttle=throttle
        )

    if pool is None or len(claimed) == 1:
        for item in claimed:
            work(item)
    else:
        list(pool.map(work, claimed))
    return len(claimed)


def run_forever(
    stop: threading.Event,
    *,
    provider: EmailProvider,
    worker_id: str,
    session_factory: Callable[[], Session] = SessionLocal,
    throttle: SendThrottle | None = None,
) -> None:
    last_purge = 0.0
    started_at = time.time()
    last_ok_at: float | None = None
    failures = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="otp-send") as pool:
        while not stop.is_set():
            _heartbeat()
            try:
                attempted = run_once(
                    provider=provider,
                    worker_id=worker_id,
                    session_factory=session_factory,
                    pool=pool,
                    throttle=throttle,
                )
                if time.monotonic() - last_purge > PURGE_EVERY_SECONDS:
                    last_purge = time.monotonic()
                    _purge(session_factory)
                failures = 0
                last_ok_at = time.time()
            except Exception as exc:  # noqa: BLE001 - a bad poll must not kill the worker
                failures += 1
                logger.error("worker poll failed (%s); retrying", type(exc).__name__)
                attempted = 0
                _publish_status(started_at, last_ok_at, failures, throttle)
                stop.wait(POLL_SECONDS * 5)
            else:
                _publish_status(started_at, last_ok_at, failures, throttle)
            if attempted == 0:
                # While paced or paused, wait for the slot instead of polling the
                # database once a second for nothing (no session is open here).
                idle = POLL_SECONDS
                hint = throttle.wait_seconds() if throttle is not None else 0.0
                if hint > 0:
                    idle = min(max(hint, 0.05), 5.0)
                stop.wait(idle)


def _publish_status(
    started_at: float,
    last_ok_at: float | None,
    failures: int,
    throttle: SendThrottle | None,
) -> None:
    state = throttle.state() if throttle is not None else None
    write_status(
        started_at=started_at,
        loop_at=time.time(),
        last_ok_at=last_ok_at,
        consecutive_failures=failures,
        paused_for_seconds=round(state.paused_for_seconds, 1) if state else 0,
        sent_in_window=state.sent_in_window if state else None,
        rate_limit=state.limit if state else None,
    )


def _purge(session_factory: Callable[[], Session]) -> None:
    db = session_factory()
    try:
        with mutating_transaction(db) as tx:
            removed = otp_outbox.purge_finished(tx)
        if removed:
            logger.info("purged %d finished OTP delivery record(s)", removed)
    finally:
        db.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    # `get_settings()` validates the whole configuration, so a production
    # worker refuses to start without the encryption key etc.
    settings = get_settings()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    worker_id = new_worker_id()
    if not settings.outbox_enabled:
        # Development without OTP_OUTBOX_ENCRYPTION_KEY / RESEND_API_KEY: the
        # web process sends (or logs) codes itself, so there is nothing to
        # drain. Stay alive and healthy rather than crash-looping.
        logger.warning(
            "OTP outbox is not enabled (needs RESEND_API_KEY and OTP_OUTBOX_ENCRYPTION_KEY); idling"
        )
        idle_since = time.time()
        while not stop.is_set():
            _heartbeat()
            write_status(started_at=idle_since, loop_at=time.time(), mode="idle")
            stop.wait(IDLE_POLL_SECONDS)
        return 0

    assert settings.resend_api_key is not None
    throttle = SendThrottle(
        settings.otp_delivery_rate_limit, settings.otp_delivery_rate_window_seconds
    )
    logger.info(
        "OTP worker %s started (at most %d sends per %ds)",
        worker_id,
        settings.otp_delivery_rate_limit,
        settings.otp_delivery_rate_window_seconds,
    )
    run_forever(
        stop,
        provider=ResendProvider(settings.resend_api_key),
        worker_id=worker_id,
        throttle=throttle,
    )
    logger.info("OTP worker %s stopped", worker_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
