"""FastAPI application factory.

M01 scope: app construction, the router registration point, the
exception-handler registration point, and the single /health route
that proves the service and its database connection both work.

No CORS middleware is added here. ARCHITECTURE.md §3.2 designs the
browser out of ever talking to this service directly — the Next.js
rewrite proxy makes every request same-origin, which is exactly what
avoids CORS being load-bearing at all. §21.6 describes CORS as a
developer convenience for /docs only, and /docs itself needs no CORS
grant (Swagger UI calls its own origin). Adding an allow-list now would
mean inventing a CORS_ORIGINS setting with no real consumer, which is
out of scope for this phase — see IMPLEMENTATION_PLAN.md M01 note on
not anticipating later milestones. Revisit if a concrete cross-origin
need appears.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.core.csrf import OriginAssertionMiddleware
from app.core.errors import DomainError, MaintenanceError, RateLimitedError, error_envelope
from app.core.media import get_store
from app.core.privacy import CardPrivacyHeadersMiddleware, install_log_masking
from app.db import all_models  # noqa: F401 — full metadata, or cross-module FKs fail at flush
from app.db.session import database_is_reachable
from app.modules.admin.routers import router as admin_router
from app.modules.attendees.routers import router as attendees_router
from app.modules.auth.routers import router as auth_router
from app.modules.booths.routers import router as booths_router
from app.modules.contact_cards.routers import router as contact_cards_router
from app.modules.feedback.routers import router as feedback_router
from app.modules.interests.routers import router as interests_router
from app.modules.leaderboard.routers import router as leaderboard_router
from app.modules.matching.routers import router as matching_router
from app.modules.media.routers import router as media_router
from app.modules.points.routers import router as points_router
from app.modules.realtime.broker import event_bus
from app.modules.realtime.listener import run_reset_listener
from app.modules.realtime.middleware import (
    DatasetGenerationHeaderMiddleware,
    RealtimeStreamHeadersMiddleware,
)
from app.modules.realtime.routers import router as realtime_router
from app.modules.staff.routers import router as staff_router

logger = logging.getLogger(__name__)

# --- Router registration point -------------------------------------------
# M01 registers only the health route directly. From M02 onward, each
# domain module (app/modules/<name>/routers.py) is included here instead
# of routes being declared in main.py.

api_router = APIRouter(prefix="/api")


@api_router.get("/health")
def health() -> dict[str, str]:
    """Liveness + database reachability.

    The documented public contract (ARCHITECTURE.md, IMPLEMENTATION_PLAN.md
    M01) is the exact body {"status": "ok"}. Database reachability is
    checked as part of serving this route — a database outage causes a
    503 rather than a false "ok" — but the success body is not enlarged
    with extra fields, since the locked contract is exactly one key.
    """
    if not database_is_reachable():
        raise HTTPException(status_code=503, detail="database unreachable")
    return {"status": "ok"}


# Liveness and readiness are different questions, so they are different routes.
#
#   /api/health/live   is this PROCESS able to answer at all? Never touches the
#                      database, so a database outage cannot make a watchdog
#                      restart a backend that restarting would not fix.
#   /api/health/ready  may traffic be sent here? True only while the database
#                      answers. (`/api/health` above is the same check; it keeps
#                      its locked M01 contract and the Docker healthcheck.)
#
# Both are public, so both say only up/down: no versions, hosts, counts, error
# text. The operator's detail is `python -m app.cli ops-check` (a shell on the
# server), never an HTTP route.
_NO_STORE = {"Cache-Control": "no-store"}


@api_router.get("/health/live")
def health_live() -> JSONResponse:
    return JSONResponse({"status": "ok"}, headers=_NO_STORE)


@api_router.get("/health/ready")
def health_ready() -> JSONResponse:
    if not database_is_reachable():
        return JSONResponse({"status": "unavailable"}, status_code=503, headers=_NO_STORE)
    return JSONResponse({"status": "ok"}, headers=_NO_STORE)


def _register_exception_handlers(app: FastAPI) -> None:
    """ARCHITECTURE.md §20: one JSON envelope for every failure, three
    handlers wide enough to cover it. Introduced with the first
    mutating feature (M04) — nothing before this milestone raised a
    DomainError or accepted a request body a client could get wrong.
    """

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        body = error_envelope(exc.code, str(exc), details=exc.details)
        if exc.status_code >= 500:
            logger.error(
                "Unhandled domain error %s on %s", exc.code, request.url.path, exc_info=exc
            )
        headers = (
            {"Retry-After": str(exc.retry_after)} if isinstance(exc, RateLimitedError) else None
        )
        return JSONResponse(status_code=exc.status_code, content=body, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # §20.3: VALIDATION_ERROR, 422, "Pydantic rejects the payload."
        # Only loc/msg/type are returned — not Pydantic's raw error
        # dicts, whose `ctx` can hold a live exception object (not
        # JSON-serialisable) and whose `input` would echo submitted
        # values such as an OTP code straight back.
        errors = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        body = error_envelope(
            "VALIDATION_ERROR",
            "The request body failed validation.",
            details={"errors": errors},
        )
        return JSONResponse(status_code=422, content=body)

    @app.exception_handler(OperationalError)
    async def _operational_error_handler(request: Request, exc: OperationalError) -> JSONResponse:
        # 55P03 = lock_not_available: a fenced mutation's lock_timeout
        # expired (ARCHITECTURE.md §19.4 — maps to MAINTENANCE, 503).
        # Anything else operational is a genuine internal failure.
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            maintenance = MaintenanceError(
                "The service is briefly unavailable. Please try again in a moment."
            )
            body = error_envelope(maintenance.code, str(maintenance))
            return JSONResponse(status_code=maintenance.status_code, content=body)
        body = error_envelope("INTERNAL_ERROR", "An unexpected error occurred.")
        logger.error("Unhandled operational error on %s", request.url.path, exc_info=exc)
        return JSONResponse(status_code=500, content=body)

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # §20.4: "A catch-all handler converts any unhandled exception
        # to INTERNAL_ERROR with a generic message. Stack traces are
        # never serialised to a client." — also §7.14: a raw
        # IntegrityError reaching here is a bug, logged as one.
        body = error_envelope("INTERNAL_ERROR", "An unexpected error occurred.")
        logger.error("Unhandled exception on %s", request.url.path, exc_info=exc)
        return JSONResponse(status_code=500, content=body)


def _configure_logging(demo_mode: bool) -> None:
    """uvicorn configures only its own loggers; without this, INFO
    records from `app.*` are silently dropped by Python's WARNING-level
    default. Idempotent — `--reload` re-imports main.

    The DEMO_MODE OTP line (app/modules/auth/otp_delivery.py) carries a
    full email address, which §21.11 keeps out of INFO-level logs; it is
    written at DEBUG and that one logger is opened to DEBUG only in
    DEMO_MODE, which config already forbids in production.

    Card tokens and CSV export tickets are masked (§21.11) on every
    logger that can print a request URL and on each of their handlers
    (see app/core/privacy.py: `install_log_masking`)."""
    app_logger = logging.getLogger("app")
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s - %(message)s"))
        app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)
    install_log_masking()
    logging.getLogger("app.modules.auth.otp_delivery").setLevel(
        logging.DEBUG if demo_mode else logging.INFO
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """M14: `event_bus` bridges sync publishers (every mutating route
    is a plain `def`, run in FastAPI's threadpool) to the async
    `asyncio.Queue`s each open stream owns, via
    `loop.call_soon_threadsafe` — which needs the actual running event
    loop, obtainable only once one exists, i.e. here, not at
    `EventBus.__init__` (construction happens at import time, before
    any loop is running — including during test collection). Unbound
    again on shutdown so a publish after that is the documented
    best-effort no-op (§17.7.2) rather than a call into a dead loop.

    M15: the reset LISTEN task (`app/modules/realtime/listener.py`) is
    the other half of `event_bus.bind_loop` — it is what lets a demo
    reset, run as a completely separate CLI process, still reach this
    process's real open streams. Started here (process lifetime, same
    as the bound loop) and cancelled on shutdown; a `CancelledError`
    from awaiting it afterward is the expected, silent shutdown path.
    """
    event_bus.bind_loop(asyncio.get_running_loop())
    settings = get_settings()
    # Not fatal (everything but photo uploads works without it), but an operator
    # should learn at start-up, not from an attendee, that the volume is missing,
    # read-only or has the wrong owner.
    if not get_store().check_writable():
        logger.warning(
            "MEDIA_ROOT is not writable: profile photo uploads will answer "
            "503 PHOTO_STORAGE_UNAVAILABLE until the media volume is fixed"
        )
    listener_task = asyncio.create_task(run_reset_listener(settings.database_url, event_bus))
    try:
        yield
    finally:
        listener_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener_task
        event_bus.unbind_loop()


def create_app() -> FastAPI:
    settings = get_settings()  # fails fast here if required config is missing
    _configure_logging(settings.demo_mode)

    app = FastAPI(
        title="FACEMARK API",
        version="0.1.0",
        # Full interactive docs are a development convenience; hidden in
        # production per ARCHITECTURE.md §18 (admin routes excluded from
        # the production OpenAPI schema is the same principle applied
        # more broadly — least exposure by default).
        docs_url="/docs" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    _register_exception_handlers(app)
    # §8.3.1 layer 2: Origin/Referer assertion on every mutating request,
    # before any handler (and therefore before any fence or read) runs.
    app.add_middleware(OriginAssertionMiddleware, allowed_origin=settings.app_origin)
    # §21.11: added before the dataset-generation header, so THAT one is
    # outermost and stamps every response — a card response, an error
    # envelope, a health check, all of it (§19.3).
    app.add_middleware(CardPrivacyHeadersMiddleware)
    app.add_middleware(RealtimeStreamHeadersMiddleware)
    app.add_middleware(DatasetGenerationHeaderMiddleware)
    app.include_router(api_router)
    app.include_router(attendees_router, prefix="/api")
    app.include_router(admin_router, prefix="/api", include_in_schema=not settings.is_production)
    app.include_router(auth_router, prefix="/api")
    app.include_router(booths_router, prefix="/api")
    app.include_router(contact_cards_router, prefix="/api")
    app.include_router(feedback_router, prefix="/api")
    app.include_router(interests_router, prefix="/api")
    app.include_router(leaderboard_router, prefix="/api")
    app.include_router(matching_router, prefix="/api")
    app.include_router(media_router, prefix="/api")
    app.include_router(points_router, prefix="/api")
    app.include_router(realtime_router, prefix="/api")
    app.include_router(staff_router, prefix="/api")

    return app


app = create_app()
