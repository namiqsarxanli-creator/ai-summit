"""Load-test findings (production copy, 1 vCPU / 1 GB): admission control and
the shared leaderboard ranking.

* Without admission control, more simultaneous requests than pooled database
  connections deadlocked the single worker until the pool's 30 s timeout.
* Ranking the whole population per request made the worker CPU-bound when
  every phone refetched the leaderboard after an award.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.core.admission import AdmissionMiddleware
from app.core.config import get_settings
from app.modules.leaderboard import service as leaderboard_service
from app.modules.realtime.broker import event_bus

# --------------------------------------------------------------------------
# Admission control
# --------------------------------------------------------------------------


def _app(gate: asyncio.Event, limit: int, wait: float) -> AdmissionMiddleware:
    async def slow(_request: Request) -> PlainTextResponse:
        await gate.wait()
        return PlainTextResponse("done")

    inner = Starlette(
        routes=[
            Route("/api/slow", slow),
            Route("/api/realtime/stream", slow),
            Route("/page", slow),
        ]
    )
    return AdmissionMiddleware(inner, limit=limit, wait_seconds=wait)


def test_requests_over_the_admission_limit_wait_and_then_get_503() -> None:
    async def scenario() -> tuple[int, int, Any, str | None]:
        gate = asyncio.Event()
        transport = httpx.ASGITransport(app=_app(gate, limit=1, wait=0.2))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            first = asyncio.create_task(client.get("/api/slow"))
            await asyncio.sleep(0.05)
            refused = await client.get("/api/slow")
            gate.set()
            done = await first
        return (
            done.status_code,
            refused.status_code,
            refused.json(),
            refused.headers.get("retry-after"),
        )

    first_status, refused_status, body, retry_after = asyncio.run(scenario())
    assert first_status == 200
    assert refused_status == 503
    assert body["error"]["code"] == "SERVER_BUSY"
    assert retry_after == "2"


def test_a_waiting_request_is_admitted_when_a_slot_frees() -> None:
    async def scenario() -> list[int]:
        gate = asyncio.Event()
        transport = httpx.ASGITransport(app=_app(gate, limit=1, wait=2.0))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            first = asyncio.create_task(client.get("/api/slow"))
            await asyncio.sleep(0.05)
            second = asyncio.create_task(client.get("/api/slow"))
            await asyncio.sleep(0.05)
            gate.set()
            return [(await first).status_code, (await second).status_code]

    assert asyncio.run(scenario()) == [200, 200]


def test_the_live_stream_and_pages_are_not_counted() -> None:
    async def scenario() -> list[int]:
        gate = asyncio.Event()
        transport = httpx.ASGITransport(app=_app(gate, limit=1, wait=0.1))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            holder = asyncio.create_task(client.get("/api/slow"))
            await asyncio.sleep(0.05)
            stream = asyncio.create_task(client.get("/api/realtime/stream"))
            page = asyncio.create_task(client.get("/page"))
            await asyncio.sleep(0.2)  # longer than the admission wait
            gate.set()
            return [
                (await holder).status_code,
                (await stream).status_code,
                (await page).status_code,
            ]

    assert asyncio.run(scenario()) == [200, 200, 200]


# --------------------------------------------------------------------------
# Shared leaderboard ranking
# --------------------------------------------------------------------------


@pytest.fixture
def ranking_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(get_settings(), "leaderboard_cache_seconds", 5.0)
    leaderboard_service.invalidate_ranking_snapshot()
    yield
    leaderboard_service.invalidate_ranking_snapshot()


def test_the_ranking_is_built_once_and_shared(ranking_cache: None, db: Session) -> None:
    first = leaderboard_service.ranking_snapshot(db)
    second = leaderboard_service.ranking_snapshot(db)
    assert first is not None
    assert second is first


def test_an_award_signal_discards_the_shared_ranking(ranking_cache: None, db: Session) -> None:
    first = leaderboard_service.ranking_snapshot(db)
    event_bus.invalidate_leaderboard()
    second = leaderboard_service.ranking_snapshot(db)
    assert first is not None and second is not None
    assert second is not first
    assert second.version == event_bus.leaderboard_version


def test_the_shared_ranking_expires(
    ranking_cache: None, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = leaderboard_service.ranking_snapshot(db)
    assert first is not None
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + 6.0)
    assert leaderboard_service.ranking_snapshot(db) is not first


def test_caching_off_means_every_request_ranks(db: Session) -> None:
    assert get_settings().leaderboard_cache_seconds == 0  # the suite's default
    assert leaderboard_service.ranking_snapshot(db) is None


def test_a_burst_of_changes_is_pushed_once_now_and_once_at_the_end_of_the_window(
    ranking_cache: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """500-phone load test: a push per award re-ranked and re-sent the board to
    every stream and starved the requests. The first change goes out at once, the
    rest of the window as ONE push with the newest version; every change still
    discards the ranking cache at once, so reads stay exact."""
    from app.modules.realtime import service as realtime_service

    monkeypatch.setattr(get_settings(), "leaderboard_push_interval_seconds", 0.4)
    realtime_service.reset_leaderboard_push()
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        event_bus, "publish_broadcast", lambda e, d: sent.append(dict(d["leaderboard"]))
    )
    before = event_bus.leaderboard_version
    try:
        for _ in range(5):
            realtime_service.publish_leaderboard_changed()
        assert event_bus.leaderboard_version == before + 5  # every change invalidates
        assert [p["version"] for p in sent] == [before + 1]  # the first one at once
        time.sleep(0.8)
        assert [p["version"] for p in sent] == [before + 1, before + 5]  # one trailing push
        # After a quiet window the next change goes out at once again; one more
        # inside the new window waits for its end.
        realtime_service.publish_leaderboard_changed()
        realtime_service.publish_leaderboard_changed()
        assert [p["version"] for p in sent] == [before + 1, before + 5, before + 6]
        time.sleep(0.8)
        assert [p["version"] for p in sent] == [before + 1, before + 5, before + 6, before + 7]
    finally:
        realtime_service.reset_leaderboard_push()


def test_an_award_pushes_the_ranked_top_n_with_its_version(
    ranking_cache: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.realtime import service as realtime_service

    sent: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(event_bus, "publish_broadcast", lambda e, d: sent.append((e, d)))
    before = event_bus.leaderboard_version
    realtime_service.publish_leaderboard_changed()
    [(event, data)] = sent
    assert event == "leaderboard_changed"
    pushed: Any = data["leaderboard"]
    assert pushed["version"] == before + 1 == event_bus.leaderboard_version
    assert isinstance(pushed["entries"], list)
    assert isinstance(pushed["dataset_generation"], int)
