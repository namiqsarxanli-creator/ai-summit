"""Cross-process reset notification — M15 / Codex pre-review Blocker 1.

The demo reset CLI (`python -m app.cli reset-demo`) runs as its own OS
process, entirely separate from the serving Uvicorn worker. M14's
`EventBus` (`app/modules/realtime/broker.py`) is in-process asyncio
state — a `publish_broadcast()` call made from the CLI's own process
would touch only *that* process's empty, never-subscribed-to broker,
never the serving worker's real one holding real open streams. Calling
it from the CLI would be a silent no-op with no error, which is worse
than doing nothing: it would look correct while accomplishing nothing.

`LISTEN`/`NOTIFY` is the minimal cross-process transport (see
`app/core/locks.py`'s `RESET_NOTIFY_CHANNEL` docstring): this module
runs a background asyncio task, for the lifetime of the serving
process, that holds one dedicated async connection LISTENing on that
channel. When the reset CLI's dedicated connection issues `NOTIFY`
*after* its restore transaction commits (`app/modules/admin/reset.py`),
this task wakes, and performs exactly the "post-restore in-process
cleanup" ARCHITECTURE.md §19.5 step 5 already specifies — emit
`dataset_reset` to every open stream via the REAL, serving-process
`EventBus`, then clear its bookkeeping. Every open SSE generator
(`app/modules/realtime/routers.py`) already knows what to do with a
`dataset_reset` event: yield it to its client and close (built in M14,
"M15 compatibility only" at the time).

A lost or missed notification (a listener reconnecting after a
transient DB blip, or a restart racing a reset) never threatens
correctness — `dataset_generation` mismatch on the client's very next
HTTP response is the authoritative backstop (§19.3). This channel only
ever affects how quickly a stale stream notices; it is never load-bearing
for what state a client is allowed to act on. Accordingly, `NOTIFY`
payloads are read as a best-effort hint, not trusted verbatim: this
listener always re-derives the delivered generation from the payload
when parseable, but degrades to failing safe (broadcasting the
`dataset_reset` anyway to force reinitialisation) if it cannot.
"""

from __future__ import annotations

import asyncio
import json
import logging

import psycopg

from app.core.locks import RESET_NOTIFY_CHANNEL
from app.modules.realtime.broker import EventBus

logger = logging.getLogger(__name__)

# Backoff between LISTEN reconnect attempts after the connection drops
# (server restart, network blip, etc.) — short enough that the serving
# process is not meaningfully deaf to a reset for long, bounded so a
# persistently unreachable database does not spin the loop hot.
_RECONNECT_DELAY_SECONDS = 1.0


def _psycopg_conninfo(sqlalchemy_database_url: str) -> str:
    """`psycopg.AsyncConnection.connect()` takes a libpq conninfo /
    `postgresql://` URI, not SQLAlchemy's dialect-qualified URL."""
    return sqlalchemy_database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _handle_notify_payload(event_bus: EventBus, raw_payload: str) -> None:
    """Applies exactly ARCHITECTURE.md §19.5 step 5 in the serving
    process: broadcast `dataset_reset` (every open generator closes
    itself on receipt), then clear the broker's own bookkeeping.

    The payload is `{"dataset_generation": N}`, written by the reset
    CLI (`app/modules/admin/reset.py`) after its restore transaction
    commits. If it fails to parse — it never should, but this channel
    is explicitly not trusted for correctness (module docstring) — the
    safe degradation is to broadcast anyway with no generation number,
    still forcing every connected client to reinitialise and refetch,
    rather than silently doing nothing.
    """
    generation: int | None = None
    try:
        parsed = json.loads(raw_payload)
        if isinstance(parsed, dict) and isinstance(parsed.get("dataset_generation"), int):
            generation = parsed["dataset_generation"]
    except (ValueError, TypeError):
        logger.warning("reset NOTIFY payload was not parseable JSON: %r", raw_payload)

    data: dict[str, object] = {} if generation is None else {"dataset_generation": generation}
    event_bus.publish_broadcast("dataset_reset", data)
    event_bus.reset()


async def run_reset_listener(database_url: str, event_bus: EventBus) -> None:
    """Runs until cancelled. Intended to be started as a background
    `asyncio.Task` from `main.py`'s `lifespan`, alongside
    `event_bus.bind_loop()` — both are process-lifetime, serving-worker
    concerns. A dedicated connection (not the SQLAlchemy pool): this is
    a long-lived LISTEN session, not a request-scoped unit of work.
    """
    conninfo = _psycopg_conninfo(database_url)
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
                await conn.execute(f"LISTEN {RESET_NOTIFY_CHANNEL}")
                logger.info("reset listener: LISTENing on %s", RESET_NOTIFY_CHANNEL)
                async for notify in conn.notifies():
                    logger.info("reset listener: received NOTIFY, broadcasting dataset_reset")
                    _handle_notify_payload(event_bus, notify.payload)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a dropped LISTEN connection must not crash the app
            logger.exception("reset listener: connection lost, reconnecting")
            await asyncio.sleep(_RECONNECT_DELAY_SECONDS)
