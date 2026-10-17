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

import base64
import binascii
import ipaddress
from email.utils import parseaddr
from functools import lru_cache
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.proxy import Network, parse_trusted_cidrs

# --- production hardening ---------------------------------------------------
# Values that are known, published or obviously unfinished. Production
# refuses to start on any of them — a copied template or a development
# secret must fail loudly, not run.
_ENVIRONMENTS = ("development", "production")
_KNOWN_DEV_SCAN_REF_SECRETS = frozenset({"facemark_dev_only_change_me_scan_ref_secret"})
_PLACEHOLDER_MARKERS = (
    "replace",
    "change_me",
    "changeme",
    "change-me",
    "placeholder",
    "dev_only",
    "your_",
    "your-",
    "example",
    "todo",
    "xxxx",
)
_MIN_SCAN_REF_SECRET_LENGTH = 32
# Names that can never be a public, certificate-bearing site or a
# deliverable mail domain.
_RESERVED_DOMAINS = ("localhost", "example.com", "example.org", "example.net")
_RESERVED_SUFFIXES = (".localhost", ".local", ".test", ".invalid", ".example", ".internal", ".lan")


def decode_outbox_key(value: str) -> bytes:
    """The OTP outbox key: URL-safe base64 of exactly 32 random bytes
    (an AES-256 key). Raises ValueError otherwise."""
    try:
        raw = base64.urlsafe_b64decode(value.strip() + "=" * (-len(value.strip()) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("not valid URL-safe base64") from exc
    if len(raw) != 32:
        raise ValueError("must decode to exactly 32 bytes")
    return raw


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _is_unusable_public_host(host: str | None) -> bool:
    """True for a missing host, an IP literal, or a reserved/local name."""
    if not host:
        return True
    host = host.lower().rstrip(".")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        return True
    if "." not in host:
        return True
    return host.endswith(_RESERVED_SUFFIXES) or any(
        host == d or host.endswith("." + d) for d in _RESERVED_DOMAINS
    )


class Settings(BaseSettings):
    """Process-wide configuration, read once from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # A startup failure is printed to the container log; the default
        # error text would echo every input value, secrets included.
        hide_input_in_errors=True,
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

    # Admin CSV exports (app/modules/admin/exports.py). Bounds, not tuning
    # knobs: one export holds one database connection and one consistent
    # snapshot open for as long as it runs, so both its size and its duration
    # are capped. An export over EXPORT_MAX_ROWS is refused before it starts
    # (narrow the filters); one that runs past EXPORT_MAX_SECONDS is aborted
    # mid-stream, which the client sees as a failed download.
    export_max_rows: int = Field(default=100_000, ge=100, le=5_000_000)
    export_max_seconds: int = Field(default=120, ge=5, le=3_600)

    # ARCHITECTURE.md §13.3: the HMAC key signing every `scan_ref`
    # envelope. No fallback (invariant #14) — a guessed key would let
    # anyone forge a staff scan of any attendee token.
    scan_ref_secret: str = Field(...)

    # Real OTP email delivery (app/modules/auth/otp_delivery.py).
    # Optional precisely BECAUSE it is independent of DEMO_MODE: a demo
    # deployment may still want real emails on top of the log/admin-
    # panel visibility. "At least one delivery path exists" is enforced
    # where it is used — `deliver_otp` raises if neither this key nor
    # DEMO_MODE is set — not at Settings() construction, which would
    # make every unrelated config test/tool that builds a minimal
    # Settings fail. No fallback value (invariant #14): an unset key
    # must never be silently treated as configured.
    resend_api_key: str | None = None
    # Not a secret — the From header any recipient already sees. A
    # default is fine here, unlike the key above.
    otp_from_email: str = "AI Summit <info@aiworks.az>"

    # Key for the authenticated encryption of OTP codes while they wait in
    # the delivery outbox (app/modules/auth/otp_outbox.py): URL-safe base64 of
    # 32 random bytes (`python -m app.cli generate-otp-key`). Required in
    # production. Outside production it is optional: without it the legacy
    # in-process sender is used (no outbox, no worker). The previous key may
    # stay configured for one OTP lifetime (10 min) after a rotation so codes
    # queued under it can still be sent.
    otp_outbox_encryption_key: str | None = None
    otp_outbox_encryption_key_previous: str | None = None

    # Aggregate send pacing of the OTP worker (app/modules/auth/otp_throttle.py):
    # at most OTP_DELIVERY_RATE_LIMIT sends may START in any
    # OTP_DELIVERY_RATE_WINDOW_SECONDS. Set it BELOW your email provider's request
    # limit (Resend's default is 2 requests per second per team). A registration
    # burst then queues in the database and drains at this speed instead of
    # running into 429 answers. Per worker process; the supported deployment runs
    # one worker (DEPLOYMENT.md).
    otp_delivery_rate_limit: int = Field(default=2, ge=1, le=500)
    otp_delivery_rate_window_seconds: int = Field(default=1, ge=1, le=3_600)

    # Profile photos (app/core/media.py, app/core/images.py). MEDIA_ROOT is a
    # directory the backend process may write to; in the Compose files it is a
    # named Docker volume mounted there, so photos survive a container rebuild
    # (the container's own filesystem does not). No external storage service.
    media_root: str = "/data/media"
    # Bounds, not tuning knobs: an upload is read into memory and decoded, so
    # both its size in bytes and its size in pixels (what a tiny file can expand
    # to - a "decompression bomb") are capped before any pixel is decoded.
    media_max_upload_bytes: int = Field(default=8 * 1024 * 1024, ge=100_000, le=25 * 1024 * 1024)
    media_max_pixels: int = Field(default=25_000_000, ge=1_000_000, le=100_000_000)
    # How long the shared leaderboard ranking may be reused when no award or
    # profile change was signalled in this process (0 = recompute every request).
    leaderboard_cache_seconds: float = Field(default=5.0, ge=0, le=60)
    # Beyond Now Founder: which activities complete a zone is NOT confirmed by the
    # organiser (competition/founder.py). Until an operator sets this after that
    # decision, no Founder bonus is awarded and no final result names Founders or
    # Lucky Draw entrants.
    founder_rule_confirmed: bool = False

    @field_validator("environment")
    @classmethod
    def _known_environment(cls, value: str) -> str:
        # `is_production` is the switch for every production-only guard, so
        # a typo ("prod", "Production ", "producton") must NOT silently mean
        # "not production".
        normalised = value.strip().lower()
        if normalised not in _ENVIRONMENTS:
            raise ValueError(f"ENVIRONMENT must be one of {', '.join(_ENVIRONMENTS)}")
        return normalised

    @field_validator("app_origin")
    @classmethod
    def _normalise_origin(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("APP_ORIGIN must look like https://host[:port]")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("APP_ORIGIN must be an origin only — no path, query or fragment")
        return f"{parts.scheme}://{parts.netloc}".lower()

    @field_validator("otp_from_email")
    @classmethod
    def _validate_from_address(cls, value: str) -> str:
        # `Display Name <addr@domain>` or a bare address. A malformed
        # sender is rejected by the provider on EVERY send, silently, on
        # a worker thread — so fail at startup instead.
        address = parseaddr(value)[1]
        local, _, domain = address.partition("@")
        if not local or "." not in domain:
            raise ValueError(
                "OTP_FROM_EMAIL must be an address like 'AI Summit <info@example.org>'"
            )
        return value

    @field_validator("otp_outbox_encryption_key", "otp_outbox_encryption_key_previous")
    @classmethod
    def _validate_outbox_key(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None or not value.strip():
            return None
        try:
            decode_outbox_key(value)
        except ValueError as exc:
            # Names the variable, never the value.
            raise ValueError(f"{str(info.field_name).upper()} {exc}") from exc
        return value.strip()

    @field_validator("media_root")
    @classmethod
    def _media_root_is_absolute(cls, value: str) -> str:
        # A relative path would depend on the process's working directory, so a
        # restart from elsewhere would silently look at an empty store.
        cleaned = value.strip()
        if not (PurePosixPath(cleaned).is_absolute() or PureWindowsPath(cleaned).is_absolute()):
            raise ValueError("MEDIA_ROOT must be an absolute path")
        return cleaned

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def _validate_cidrs(cls, value: str) -> str:
        parse_trusted_cidrs(value)  # raises on anything malformed — fail at startup
        return value

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def outbox_enabled(self) -> bool:
        """Email goes through the durable outbox (and the worker process)
        when there is a provider key AND an encryption key; see above for
        the non-production fallback."""
        return bool(self.resend_api_key and self.otp_outbox_encryption_key)

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

    @model_validator(mode="after")
    def _production_hardening(self) -> Settings:
        """Everything production refuses to start without, reported
        together so one run shows the operator the whole list. Messages
        name variables, never values (secrets must not reach logs)."""
        if not self.is_production:
            return self
        problems: list[str] = []

        # Session cookies are Secure and the camera scanner needs a secure
        # context only when the browser-facing origin is HTTPS (§8.3, §13.5).
        if not self.app_origin.startswith("https://"):
            problems.append("APP_ORIGIN must be an https:// origin")
        elif _is_unusable_public_host(urlsplit(self.app_origin).hostname):
            problems.append(
                "APP_ORIGIN must be the real public domain (not localhost/an IP/a placeholder)"
            )

        secret = self.scan_ref_secret
        if secret in _KNOWN_DEV_SCAN_REF_SECRETS or _looks_like_placeholder(secret):
            problems.append("SCAN_REF_SECRET is a known development value or placeholder")
        elif len(secret) < _MIN_SCAN_REF_SECRET_LENGTH:
            problems.append(
                f"SCAN_REF_SECRET must be at least {_MIN_SCAN_REF_SECRET_LENGTH} characters"
            )

        # No demo log to fall back on (DEMO_MODE is rejected above), so
        # without a real provider key no attendee could receive a code.
        if not self.resend_api_key or _looks_like_placeholder(self.resend_api_key):
            problems.append("RESEND_API_KEY is required (a real key, not a placeholder)")
        if "otp_from_email" not in self.model_fields_set:
            problems.append("OTP_FROM_EMAIL must be set explicitly")
        else:
            domain = parseaddr(self.otp_from_email)[1].rpartition("@")[2]
            if _is_unusable_public_host(domain):
                problems.append("OTP_FROM_EMAIL must use your verified sending domain")

        if not self.otp_outbox_encryption_key:
            problems.append("OTP_OUTBOX_ENCRYPTION_KEY is required (see DEPLOYMENT.md)")

        # Empty collapses every visitor onto the proxy's address — one shared
        # rate-limit bucket (§21.7.1). Non-private ranges would let outside
        # hosts choose their own identity through X-Forwarded-For.
        trusted = self.trusted_proxies
        if not trusted:
            problems.append("TRUSTED_PROXY_CIDRS must list the reverse proxy's network")
        elif not all(network.is_private for network in trusted):
            problems.append("TRUSTED_PROXY_CIDRS may only contain private network ranges")

        if problems:
            raise ValueError("unsafe production configuration: " + "; ".join(problems))
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
