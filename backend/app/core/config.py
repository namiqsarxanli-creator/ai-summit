"""Application configuration.

Each milestone adds the settings its features need — never with a
fallback default for anything security-sensitive (ARCHITECTURE.md
invariant #14: "No secret has a source-code fallback default. Missing
configuration fails at startup.").

ENVIRONMENT, DATABASE_URL and APP_ORIGIN are REQUIRED (no default).
Removing any of them causes Settings() to raise a pydantic
ValidationError, which FastAPI's app-factory lets propagate — the
process refuses to start rather than silently guessing. DEMO_MODE and
TRUSTED_PROXY_CIDRS default to their safe values (off / trust nothing).
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.proxy import Network, parse_trusted_cidrs


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

    # The browser-facing origin, e.g. "http://localhost:3000" or
    # "https://summit.example.org". ARCHITECTURE.md §8.3.1: every
    # mutating request's Origin/Referer must match "the configured
    # application origin". No default: a guessed origin would either
    # reject every legitimate request or accept a forged one.
    app_origin: str = Field(...)

    # ARCHITECTURE.md §21.7.1: "TRUSTED_PROXY_CIDRS defaults to empty —
    # trust nothing until it is configured." Comma-separated CIDRs.
    trusted_proxy_cidrs: str = ""

    @field_validator("app_origin")
    @classmethod
    def _normalise_origin(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("APP_ORIGIN must look like https://host[:port]")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("APP_ORIGIN must be an origin only — no path, query or fragment")
        return f"{parts.scheme}://{parts.netloc}".lower()

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def _validate_cidrs(cls, value: str) -> str:
        parse_trusted_cidrs(value)  # raises on anything malformed — fail at startup
        return value

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def trusted_proxies(self) -> tuple[Network, ...]:
        return parse_trusted_cidrs(self.trusted_proxy_cidrs)

    @property
    def serves_https(self) -> bool:
        """§8.3: the session cookie is `Secure` "when served over
        HTTPS". The browser reaches the app at APP_ORIGIN, so its scheme
        is what decides — not the scheme of the internal proxy hop,
        which is plain HTTP even when the public site is HTTPS."""
        return self.app_origin.startswith("https://")

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
