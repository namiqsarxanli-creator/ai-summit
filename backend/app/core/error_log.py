"""Server errors as the operator sees them: which part of the site failed, when, why.

The operator (AIWorks, the system's builder - never the event organiser) reads
it in the admin console (`GET /api/admin/system-status`). Kept in this process's
memory, like the rate limiter and admission control (one worker): nothing is
written to the database, so recording works while the database is the thing
that is down. A restart starts it empty - and the uptime shown next to it says
so.

What is recorded, for every API response with status 500 or more (and every
exception that escapes the application): the time, the section of the site the
path belongs to, the method, the path with every identifier or token replaced by
`{id}`, the status, and the error code from the JSON envelope (`SERVER_BUSY`,
`MAINTENANCE`, `INTERNAL_ERROR`, ...) or the exception's CLASS name. Never a
query string, a request or response body beyond that code, a person or a
message: nothing personal can be read back from it.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Path prefix -> section key (the admin page names them). First match wins.
SECTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("/api/attendees/register", "registration"),
    ("/api/auth", "sign_in"),
    ("/api/staff/auth", "staff_sign_in"),
    ("/api/masterclasses", "masterclasses"),
    ("/api/attendees/me/masterclasses", "masterclasses"),
    ("/api/staff/masterclasses", "scanning"),
    ("/api/playground", "playground"),
    ("/api/staff/playground", "playground"),
    ("/api/attendees/me/people", "networking"),
    ("/api/attendees/me/meeting", "networking"),
    ("/api/attendees/me/connections", "networking"),
    ("/api/attendees/me/blocks", "networking"),
    ("/api/attendees/me/recommendations", "networking"),
    ("/api/attendees/me/pair-photos", "photos"),
    ("/api/pair-photos", "photos"),
    ("/api/media", "photos"),
    ("/api/attendees/me/photo", "photos"),
    ("/api/mentor-sessions", "mentors"),
    ("/api/mentor-bookings", "mentors"),
    ("/api/staff/mentor-sessions", "mentors"),
    ("/api/attendees/me/panels", "panels"),
    ("/api/panels", "panels"),
    ("/api/staff/qmeter", "panels"),
    ("/api/leaderboard", "leaderboard"),
    ("/api/attendees/me/score", "leaderboard"),
    ("/api/screen", "screens"),
    ("/api/final", "final"),
    ("/api/staff/final", "final"),
    ("/api/staff/scan", "scanning"),
    ("/api/staff/offline-scans", "scanning"),
    ("/api/booths", "booths"),
    ("/api/attendees/feedback", "booths"),
    ("/api/staff/booth", "booths"),
    ("/api/staff", "staff"),
    ("/api/admin", "admin"),
    ("/api/realtime", "live_updates"),
    ("/api/cards", "cards"),
    ("/api/attendees/me/card", "cards"),
    ("/api/attendees/me", "profile"),
    ("/api/interests", "profile"),
    ("/api/health", "health"),
)
MAX_EVENTS: Final = 200  # the latest errors listed one by one
WINDOW_SECONDS: Final = 24 * 3600  # counts look back at most a day
MAX_STAMPS: Final = 50_000  # a bound on memory even in a storm
_BODY_LIMIT: Final = 4096
# A path segment that names a row or carries a token: digits, or long/opaque.
_ID_SEGMENT = re.compile(r"^(\d+|[A-Za-z0-9_\-]{20,}|[0-9a-fA-F-]{8,})$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,40}$")


def section_of(path: str) -> str:
    for prefix, key in SECTIONS:
        if path.startswith(prefix):
            return key
    return "other"


def template_of(path: str) -> str:
    """The path with identifiers and tokens masked: `/api/x/12/y` -> `/api/x/{id}/y`."""
    return "/".join("{id}" if _ID_SEGMENT.match(s) else s for s in path.split("/"))[:200]


@dataclass(frozen=True)
class ErrorEvent:
    at: float
    section: str
    method: str
    path: str
    status: int
    code: str


class ErrorLog:
    def __init__(self, clock: Any = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self.started_at = clock()
        self._events: deque[ErrorEvent] = deque(maxlen=MAX_EVENTS)
        self._stamps: deque[tuple[float, str, str]] = deque(maxlen=MAX_STAMPS)
        self._total = 0

    def record(self, path: str, method: str, status: int, code: str) -> None:
        now = self._clock()
        event = ErrorEvent(now, section_of(path), method, template_of(path), status, code)
        with self._lock:
            self._events.append(event)
            self._stamps.append((now, event.section, code))
            self._total += 1

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            while self._stamps and self._stamps[0][0] < now - WINDOW_SECONDS:
                self._stamps.popleft()
            stamps = list(self._stamps)
            events = list(self._events)
            total = self._total
        sections: dict[str, dict[str, Any]] = {}
        for at, section, code in stamps:
            s = sections.setdefault(
                section,
                {"last_15_min": 0, "last_hour": 0, "last_day": 0, "last_at": 0.0, "codes": {}},
            )
            s["last_day"] += 1
            s["last_hour"] += at >= now - 3600
            s["last_15_min"] += at >= now - 900
            s["last_at"] = max(s["last_at"], at)
            s["codes"][code] = s["codes"].get(code, 0) + 1
        return {
            "started_at": self.started_at,
            "now": now,
            "total_since_start": total,
            "sections": sections,
            "recent": [
                {
                    "at": e.at,
                    "section": e.section,
                    "method": e.method,
                    "path": e.path,
                    "status": e.status,
                    "code": e.code,
                }
                for e in reversed(events)
            ],
        }

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._stamps.clear()
            self._total = 0
            self.started_at = self._clock()


ERRORS = ErrorLog()


def _code_from(body: bytes) -> str:
    try:
        code = json.loads(body.decode("utf-8"))["error"]["code"]
    except Exception:  # noqa: BLE001 - not our envelope: say so, never echo it
        return "NO_ERROR_CODE"
    return code if isinstance(code, str) and _CODE.match(code) else "NO_ERROR_CODE"


class ErrorRecorderMiddleware:
    """Outermost: it also sees admission control's 503 and every exception that
    escapes the application (Starlette answers those 500 outside this layer)."""

    def __init__(self, app: ASGIApp, log: ErrorLog | None = None) -> None:
        self.app = app
        self.log = log or ERRORS

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return
        status = 0
        body = b""
        recorded = False

        async def watch(message: Message) -> None:
            nonlocal status, body, recorded
            if message["type"] == "http.response.start":
                status = int(message["status"])
            elif message["type"] == "http.response.body" and status >= 500 and not recorded:
                body += message.get("body", b"")[: _BODY_LIMIT - len(body)]
                if not message.get("more_body", False) or len(body) >= _BODY_LIMIT:
                    recorded = True
                    self.log.record(path, scope.get("method", ""), status, _code_from(body))
            await send(message)

        try:
            await self.app(scope, receive, watch)
        except Exception as exc:
            if not recorded:
                self.log.record(path, scope.get("method", ""), 500, type(exc).__name__[:40])
            raise
