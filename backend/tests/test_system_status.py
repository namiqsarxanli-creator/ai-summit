"""The operator's status page and the server-error log behind it."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from app.core.error_log import ErrorLog, ErrorRecorderMiddleware, section_of, template_of
from app.main import app
from app.modules.staff.models import StaffRole
from tests.test_competition import Comp
from tests.test_competition import comp as comp  # noqa: F401 - the fixture


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _call(app_: Any, path: str, method: str = "POST") -> list[Message]:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {"type": "http", "path": path, "method": method, "headers": []}
    asyncio.run(app_(scope, receive, send))
    return sent


def _answer(status: int, body: dict[str, Any]) -> Any:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        raw = json.dumps(body).encode()
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": raw, "more_body": False})

    return inner


def test_server_errors_are_recorded_by_section_with_their_code_and_masked_path() -> None:
    log = ErrorLog(clock=_Clock())
    busy = ErrorRecorderMiddleware(_answer(503, {"error": {"code": "SERVER_BUSY"}}), log)
    _call(busy, "/api/attendees/me/masterclasses/12")
    ok = ErrorRecorderMiddleware(_answer(200, {"ok": True}), log)
    _call(ok, "/api/leaderboard", "GET")
    refused = ErrorRecorderMiddleware(_answer(409, {"error": {"code": "SEAT_TAKEN"}}), log)
    _call(refused, "/api/attendees/me/masterclasses/12")
    foreign = ErrorRecorderMiddleware(_answer(500, {"secret": "x@mail.test"}), log)
    _call(foreign, "/api/cards/" + "t" * 43, "GET")
    snap = log.snapshot()
    # Only 5xx: a refusal (409) and a success are not server errors.
    assert snap["total_since_start"] == 2
    first, second = snap["recent"][1], snap["recent"][0]
    assert first == {
        "at": first["at"],
        "section": "masterclasses",
        "method": "POST",
        "path": "/api/attendees/me/masterclasses/{id}",
        "status": 503,
        "code": "SERVER_BUSY",
    }
    # A body that is not our envelope is never echoed; a token in the path is masked.
    assert second["code"] == "NO_ERROR_CODE" and second["path"] == "/api/cards/{id}"
    assert "mail.test" not in json.dumps(snap) and "t" * 43 not in json.dumps(snap)
    assert snap["sections"]["masterclasses"]["codes"] == {"SERVER_BUSY": 1}


def test_an_escaping_exception_is_recorded_by_class_and_still_raised() -> None:
    log = ErrorLog(clock=_Clock())

    async def broken(scope: Scope, receive: Receive, send: Send) -> None:
        raise ZeroDivisionError("detail that must not be kept")

    try:
        _call(ErrorRecorderMiddleware(broken, log), "/api/playground/slots/7/book")
    except ZeroDivisionError:
        pass
    else:  # pragma: no cover - the middleware must not swallow it
        raise AssertionError("swallowed")
    (event,) = log.snapshot()["recent"]
    assert (event["section"], event["status"], event["code"]) == (
        "playground",
        500,
        "ZeroDivisionError",
    )
    assert "detail" not in json.dumps(log.snapshot())


def test_counts_look_back_15_minutes_an_hour_and_a_day() -> None:
    clock = _Clock()
    log = ErrorLog(clock=clock)
    for ago in (90_000, 7200, 1200, 60):  # oldest first, as errors happen
        clock.now = 1_000_000.0 - ago
        log.record("/api/leaderboard", "GET", 500, "INTERNAL_ERROR")
    clock.now = 1_000_000.0
    s = log.snapshot()["sections"]["leaderboard"]
    assert (s["last_15_min"], s["last_hour"], s["last_day"]) == (1, 2, 3)


def test_paths_map_to_the_sections_the_page_names() -> None:
    assert section_of("/api/attendees/me/connections/scan") == "networking"
    assert section_of("/api/staff/masterclasses/4/door") == "scanning"
    assert section_of("/api/attendees/me/profile") == "profile"
    assert section_of("/api/unknown") == "other"
    assert template_of("/api/x/123/y/0f8c2a1e-aaaa") == "/api/x/{id}/y/{id}"


def test_the_status_page_is_admin_only_private_and_shows_each_part(comp: Comp) -> None:  # noqa: F811
    (uid,) = comp.users(1)
    admin = comp.staff_client(comp.rig.staff_id(StaffRole.ADMIN))
    r = admin.get("/api/admin/system-status")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    names = {c["name"]: c["level"] for c in body["checks"]}
    assert names["database"] == "ok" and "migrations" in names and "media-write" in names
    assert body["status"] in ("ok", "warn", "crit")
    assert set(body["admission"]) >= {"limit", "inside", "waiting", "turned_away"}
    assert set(body["errors"]) >= {"started_at", "sections", "recent", "total_since_start"}
    assert "@" not in r.text
    # The organiser's Game Master, an attendee and an anonymous visitor: refused.
    for c in (comp.volunteer("GAME_MASTER", None), comp.client(uid), TestClient(app)):
        assert c.get("/api/admin/system-status").status_code in (401, 403)
