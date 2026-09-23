"""CSRF — ARCHITECTURE.md §8.3.1, layer 2 (layer 1 is the session
cookie's SameSite=Lax).

"Every mutating request (POST/PATCH/DELETE) must carry an Origin (or,
failing that, Referer) header matching the configured application
origin. Requests with a missing or mismatched origin are rejected with
CSRF_REJECTED (403) before the handler runs." No token table (§8.3.1).

Pure ASGI middleware, so it covers every mutating route — including
ones added later — without each route having to remember it.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.errors import error_envelope

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalise_origin(value: str) -> str | None:
    """scheme://host[:port], lowercased, default port dropped — the
    exact form a browser sends in `Origin`. None if not an http(s)
    origin at all (including the literal "null" some contexts send)."""
    parts = urlsplit(value.strip())
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None or port == _DEFAULT_PORTS[parts.scheme]:
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


def request_origin(headers: Headers) -> str | None:
    origin = headers.get("origin")
    if origin is not None:
        # An Origin header, even "null", is authoritative: never fall
        # back to Referer when the browser did send one.
        return normalise_origin(origin)
    referer = headers.get("referer")
    return normalise_origin(referer) if referer else None


class OriginAssertionMiddleware:
    def __init__(self, app: ASGIApp, allowed_origin: str) -> None:
        self.app = app
        allowed = normalise_origin(allowed_origin)
        if allowed is None:
            raise ValueError(f"invalid application origin: {allowed_origin!r}")
        self.allowed_origin = allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] in MUTATING_METHODS
            and request_origin(Headers(scope=scope)) != self.allowed_origin
        ):
            response = JSONResponse(
                status_code=403,
                content=error_envelope(
                    "CSRF_REJECTED", "This request did not come from the FACEMARK site."
                ),
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
