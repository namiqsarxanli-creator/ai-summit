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

from fastapi import APIRouter, FastAPI, HTTPException

from app.core.config import get_settings
from app.db.session import database_is_reachable

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
    """Placeholder registration point.

    The consistent error envelope, the domain-error hierarchy and the
    constraint-name → error-code dispatch table are ARCHITECTURE.md §20
    concerns introduced with the first mutating feature (M02 onward).
    Nothing is registered yet; FastAPI's default handlers apply.
    """
    return


def create_app() -> FastAPI:
    settings = get_settings()  # fails fast here if required config is missing

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
    app.include_router(api_router)

    return app


app = create_app()
