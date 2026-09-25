"""API and card-token privacy protections — ARCHITECTURE.md §21.11, §9.5.

"QR tokens need specific care because they appear in a URL path
(/c/{token}), which is the part of a request that proxies, access logs
and error trackers record by default." Two application-side measures
live here (the reverse proxy's own log masking is infrastructure,
verified in M18):

- `CardTokenLogFilter` — the application's access log masks the token
  segment as `/c/***` (and the API path the card page calls, as
  `/api/cards/***`).
- `CardPrivacyHeadersMiddleware` — responses that carry a card send
  `Referrer-Policy: no-referrer` and `X-Robots-Tag: noindex`, so the
  token neither leaks to LinkedIn when the contact action is tapped nor
  reaches a search index. `Cache-Control: no-store` keeps a shared cache
  from holding a card after it stops resolving.
"""

from __future__ import annotations

import logging
import re

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CARD_PRIVACY_HEADERS: dict[str, str] = {
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex",
    "Cache-Control": "no-store",
}

# Paths whose request or response carries a card token: the public
# card read (token in the path) and the attendee's own card (token in
# the body, inside the QR URL).
CARD_PATH_PREFIXES: tuple[str, ...] = ("/api/cards/", "/api/attendees/me/card")

_TOKEN_SEGMENT = re.compile(r"(/c/|/api/cards/)[^/?#\s\"]+")


def mask_card_tokens(text: str) -> str:
    """`/c/<token>` → `/c/***`, `/api/cards/<token>` → `/api/cards/***`."""
    return _TOKEN_SEGMENT.sub(r"\1***", text)


class CardTokenLogFilter(logging.Filter):
    """Rewrites a record's message and string arguments before any
    handler formats them. uvicorn's access log passes the request path
    as an argument, so masking only `msg` would miss it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = mask_card_tokens(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                mask_card_tokens(a) if isinstance(a, str) else a for a in record.args
            )
        return True


def install_card_token_log_filter(target: logging.Logger | logging.Handler) -> None:
    """Idempotent — `--reload` re-imports the app."""
    if not any(isinstance(f, CardTokenLogFilter) for f in target.filters):
        target.addFilter(CardTokenLogFilter())


class CardPrivacyHeadersMiddleware:
    """No API response is reusable; card responses get extra token protection.

    The outer realtime middleware replaces no-store with the locked SSE
    no-cache/no-transform policy for the stream response.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope["path"])
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                if path.startswith(CARD_PATH_PREFIXES):
                    for name, value in CARD_PRIVACY_HEADERS.items():
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
