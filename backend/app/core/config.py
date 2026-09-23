"""Application configuration.

M01 scope only: the handful of settings the foundation actually needs
(app environment awareness and database connectivity). Later milestones
add their own required settings here as those features are built —
never with a fallback default for anything security-sensitive
(ARCHITECTURE.md invariant #14: "No secret has a source-code fallback
default. Missing configuration fails at startup.").

Both fields below are REQUIRED (no default). Removing either from the
environment causes Settings() to raise a pydantic ValidationError,
which FastAPI's app-factory lets propagate — the process refuses to
start rather than silently falling back to a guessed value.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide configuration, read once from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # "development" | "production" (later milestones may add more).
    # No default: every deployment must say explicitly what it is.
    environment: str = Field(...)

    # SQLAlchemy connection URL, e.g.
    # postgresql+psycopg://user:password@host:5432/dbname
    # Composed by docker-compose.yml from POSTGRES_* variables when
    # running under Docker Compose (the supported M01 workflow).
    database_url: str = Field(...)

    # ARCHITECTURE.md §8.2: "when DEMO_MODE=true, the OTP is written to
    # the server log ... instead of being emailed." Defaults to False
    # (production-safe by default) — this is a mode switch, not a
    # secret, so unlike the fields above it is allowed a default.
    demo_mode: bool = False

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @model_validator(mode="after")
    def _demo_mode_never_in_production(self) -> Settings:
        # ARCHITECTURE.md §8.2: "config.py raises at startup if
        # DEMO_MODE=true while ENVIRONMENT=production." — the OTP-in-log
        # demo adapter must be structurally unreachable in production,
        # not merely undocumented.
        if self.demo_mode and self.is_production:
            raise ValueError("DEMO_MODE=true is not permitted when ENVIRONMENT=production")
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton.

    A plain module-level ``Settings()`` would be constructed at import
    time, before tests get a chance to monkeypatch the environment.
    ``lru_cache`` defers construction to first use and gives tests a
    single, well-known place (``get_settings.cache_clear()``) to force
    re-evaluation.
    """
    return Settings()  # fields are supplied by the environment, not this call site
