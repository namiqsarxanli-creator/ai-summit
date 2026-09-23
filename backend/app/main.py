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

import logging

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.core.csrf import OriginAssertionMiddleware
from app.core.errors import DomainError, MaintenanceError, RateLimitedError, error_envelope
from app.core.privacy import CardPrivacyHeadersMiddleware, install_card_token_log_filter
from app.db import all_models  # noqa: F401 — full metadata, or cross-module FKs fail at flush
from app.db.session import database_is_reachable
from app.modules.attendees.routers import router as attendees_router
from app.modules.auth.routers import router as auth_router
from app.modules.booths.routers import router as booths_router
from app.modules.contact_cards.routers import router as contact_cards_router
from app.modules.interests.routers import router as interests_router
from app.modules.matching.routers import router as matching_router
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


def _register_exception_handlers(app: FastAPI) -> None:
    """ARCHITECTURE.md §20: one JSON envelope for every failure, three
    handlers wide enough to cover it. Introduced with the first
    mutating feature (M04) — nothing before this milestone raised a
    DomainError or accepted a request body a client could get wrong.
    """

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        body = error_envelope(exc.code, str(exc))
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

    Card tokens are masked (§21.11) on uvicorn's access and error
    loggers, where request paths are recorded, and on the app handler,
    whose error lines name the request path."""
    app_logger = logging.getLogger("app")
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s - %(message)s"))
        app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)
    for app_handler in app_logger.handlers:
        install_card_token_log_filter(app_handler)
    install_card_token_log_filter(logging.getLogger("uvicorn.access"))
    install_card_token_log_filter(logging.getLogger("uvicorn.error"))
    logging.getLogger("app.modules.auth.otp_delivery").setLevel(
        logging.DEBUG if demo_mode else logging.INFO
    )


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
        redoc_url=None,
    )

    _register_exception_handlers(app)
    # §8.3.1 layer 2: Origin/Referer assertion on every mutating request,
    # before any handler (and therefore before any fence or read) runs.
    app.add_middleware(OriginAssertionMiddleware, allowed_origin=settings.app_origin)
    # §21.11: added last, so it is outermost and stamps its headers on
    # every card-path response, whichever layer produced it.
    app.add_middleware(CardPrivacyHeadersMiddleware)
    app.include_router(api_router)
    app.include_router(attendees_router, prefix="/api")
    app.include_router(auth_router, prefix="/api")
    app.include_router(booths_router, prefix="/api")
    app.include_router(contact_cards_router, prefix="/api")
    app.include_router(interests_router, prefix="/api")
    app.include_router(matching_router, prefix="/api")
    app.include_router(staff_router, prefix="/api")

    return app


app = create_app()
