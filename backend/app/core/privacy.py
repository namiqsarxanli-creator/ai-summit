"""API and card-token privacy protections — ARCHITECTURE.md §21.11, §9.5.

"QR tokens need specific care because they appear in a URL path
(/c/{token}), which is the part of a request that proxies, access logs
and error trackers record by default." Two application-side measures
live here (the reverse proxy's own log masking is infrastructure,
verified in M18):

- `CardTokenLogFilter` — the application's logs mask the token segment as
  `/c/***` (and the API path the card page calls, as `/api/cards/***`).
  The same filter masks the one-time CSV **export ticket**, which travels
  in a query string (`?ticket=...`, see admin/export_tickets.py): whatever
  the parameter order or percent-encoding, its value becomes `***`. A
  ticket is a short-lived single-use capability, but it is still a
  credential to personal data, so it never reaches a log line.
- `CardPrivacyHeadersMiddleware` — responses that carry a card send
  `Referrer-Policy: no-referrer` and `X-Robots-Tag: noindex`, so the
  token neither leaks to LinkedIn when the contact action is tapped nor
  reaches a search index. `Cache-Control: no-store` keeps a shared cache
  from holding a card after it stops resolving.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import deque
from urllib.parse import unquote_plus

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

# The one family of /api responses that IS reusable: profile photos. Their URL is
# a random, per-photo name that changes with every new photo, so the response
# sets its own `Cache-Control: public, max-age=31536000, immutable` and this
# middleware leaves it alone. Nothing else under /api/ is exempt.
PUBLIC_MEDIA_PREFIX = "/api/media/avatars/"

_TOKEN_SEGMENT = re.compile(r"(/c/|/api/cards/)[^/?#\s\"]+")


def mask_card_tokens(text: str) -> str:
    """`/c/<token>` → `/c/***`, `/api/cards/<token>` → `/api/cards/***`."""
    return _TOKEN_SEGMENT.sub(r"\1***", text)


# --- export tickets ----------------------------------------------------------

TICKET_PARAM = "ticket"
# One query-string segment: what follows a `?` or `&` up to the next separator,
# fragment, quote, bracket or whitespace (so it also works inside a log line).
_QUERY_SEGMENT = re.compile(r"(?P<sep>[?&])(?P<seg>[^&#\s\"'<>]+(?:[ \t]+=[ \t]*[^&#\s\"'<>]*)?)")
_NAME_VALUE_BREAK = re.compile(r"=|%(?:25)*3[dD]")
_TICKET_IN_TEXT = re.compile(r"(?<![A-Za-z0-9_])ticket\s*=", re.IGNORECASE)


def _fully_decoded(segment: str) -> str:
    """Percent-/plus-decoded until it stops changing (bounded), so a name or an
    `=` that was encoded - even twice - is seen for what it is."""
    for _ in range(4):
        decoded = unquote_plus(segment)
        if decoded == segment:
            break
        segment = decoded
    return segment


def _parameter_name(segment: str) -> str:
    """The raw (still encoded) name part of a segment that carries a ticket, so
    the line keeps saying WHICH parameter was hidden - and nothing of its value."""
    for end in range(1, len(segment) + 1):
        if _fully_decoded(segment[:end]).strip().lower() == TICKET_PARAM:
            return segment[:end].strip()
    return _NAME_VALUE_BREAK.split(segment, maxsplit=1)[
        0
    ].strip()  # the ticket is nested in a value


def mask_ticket_params(text: str) -> str:
    """Hide the value of every `ticket` query parameter in `text`.

    Works on the decoded segment, so it does not depend on parameter ORDER
    (`?ticket=T&a=1`, `?a=1&ticket=T`), on the name or the `=` being
    percent-encoded (`%74icket=T`, `ticket%3DT`), on case, on a repeated
    parameter, or on the whole thing sitting inside another parameter's
    (encoded) value. Anything else in the line is left exactly as it was."""

    def hide(match: re.Match[str]) -> str:
        segment = match.group("seg")
        decoded = _fully_decoded(segment)
        if not _TICKET_IN_TEXT.search(decoded):
            return match.group(0)
        return f"{match.group('sep')}{_parameter_name(segment)}=***"

    return _QUERY_SEGMENT.sub(hide, text)


# --- personal data in query strings and in exception text -----------------------

# The admin screens search with `?q=<name, e-mail or phone>` (attendees) and
# `?email=<address>` (OTP deliveries). Those are GET requests, so uvicorn's
# access log would print what an administrator typed. The VALUE is hidden;
# the parameter stays visible so the line still says what was asked.
_SEARCH_PARAM = re.compile(
    r"(?P<sep>[?&])(?P<name>q|email)(?P<eq>=|%3[dD])[^&#\s\"'<>]*", re.IGNORECASE
)


def mask_search_params(text: str) -> str:
    """`?q=ada%40example.com&limit=5` -> `?q=***&limit=5` (also `email=`)."""
    return _SEARCH_PARAM.sub(r"\g<sep>\g<name>\g<eq>***", text)


# PostgreSQL error DETAIL lines quote the offending VALUES:
#   Key (email)=(ada@example.com) already exists.
#   Failing row contains (12, ada@example.com, ...).
# A constraint violation that reaches the catch-all handler is logged with its
# traceback, so the exception text is scrubbed of both forms and of any bare
# e-mail address.
_DB_KEY_DETAIL = re.compile(r"(Key \([^)]*\)=)\([^)]*\)")
_DB_ROW_DETAIL = re.compile(r"(Failing row contains )\(.*\)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def mask_exception_data(text: str) -> str:
    text = _DB_KEY_DETAIL.sub(r"\1(***)", text)
    text = _DB_ROW_DETAIL.sub(r"\1(***)", text)
    return _EMAIL.sub("***@***", text)


_known_lock = threading.Lock()
_known_secrets: deque[str] = deque(maxlen=2000)


def remember_secret(token: str) -> None:
    """Register a credential this process just issued (an export ticket) so the
    log filter can hide it even where it appears WITHOUT its parameter name
    around it. Bounded: the oldest are forgotten first."""
    if len(token) >= 16:  # never register something short enough to match ordinary text
        with _known_lock:
            _known_secrets.append(token)


def mask_known_tokens(text: str) -> str:
    """Belt and braces for `mask_ticket_params`."""
    with _known_lock:
        known = tuple(_known_secrets)
    for token in known:
        if token in text:
            text = text.replace(token, "***")
    return text


def mask_secrets(text: str) -> str:
    """Everything a log line must not carry: card tokens, export tickets and
    the admin search terms (`q`, `email`) that arrive in a URL."""
    return mask_known_tokens(mask_search_params(mask_ticket_params(mask_card_tokens(text))))


class CardTokenLogFilter(logging.Filter):
    """Rewrites a record's message, string arguments and rendered traceback
    before any handler formats them. uvicorn's access log passes the request
    path as an argument, so masking only `msg` would miss it; an exception's
    text is rendered here (and cached on the record) so a URL inside it is
    masked too."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Mask the FORMATTED message. Masking the format string and the arguments
        # separately would corrupt a template such as "retry %s?ticket=%s" (the `%s`
        # after `ticket=` would be replaced and the call would then fail with "not
        # all arguments converted"). Only a record that actually contained a secret
        # is rewritten (flattened to its masked text); every other record keeps its
        # `msg` and `args` exactly as the caller logged them - code that inspects
        # them (the DEMO_MODE OTP line is read back from `record.args`) is unaffected.
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken log call must not break logging
            message = str(record.msg)
        masked = mask_secrets(message)
        if masked != message:
            record.msg = masked
            record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = mask_exception_data(mask_secrets(record.exc_text))
        if record.stack_info:
            record.stack_info = mask_exception_data(mask_secrets(record.stack_info))
        return True


def install_card_token_log_filter(target: logging.Logger | logging.Handler) -> None:
    """Idempotent — `--reload` re-imports the app."""
    if not any(isinstance(f, CardTokenLogFilter) for f in target.filters):
        target.addFilter(CardTokenLogFilter())


# Loggers whose records carry request paths or exception text.
_MASKED_LOGGERS = ("", "uvicorn", "uvicorn.access", "uvicorn.error", "app", "starlette", "fastapi")


def install_log_masking() -> None:
    """Mask card tokens and export tickets on every logger that can print a
    request URL, AND on each of their handlers: a logger's own filters only see
    records logged directly on it, while a handler's see everything that
    reaches it - including records from child loggers and from libraries."""
    for name in _MASKED_LOGGERS:
        logger = logging.getLogger(name)
        install_card_token_log_filter(logger)
        for handler in logger.handlers:
            install_card_token_log_filter(handler)


class CardPrivacyHeadersMiddleware:
    """No API response is reusable (except a served profile photo, see
    PUBLIC_MEDIA_PREFIX); card responses get extra token protection.

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
                if not (path.startswith(PUBLIC_MEDIA_PREFIX) and message["status"] in (200, 304)):
                    headers["Cache-Control"] = "no-store"
                if path.startswith(CARD_PATH_PREFIXES):
                    for name, value in CARD_PRIVACY_HEADERS.items():
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
