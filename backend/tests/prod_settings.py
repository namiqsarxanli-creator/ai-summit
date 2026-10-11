"""A complete, VALID production `Settings` for tests, built from
explicit values (no `.env`, no inherited container environment).

Every value is a test value: the domain is made up, the secrets are
random-looking strings generated for this file. Nothing here is, or
resembles, a real credential."""

from __future__ import annotations

from typing import Any

from app.core.config import Settings

VALID_PRODUCTION: dict[str, Any] = {
    "environment": "production",
    "database_url": "postgresql+psycopg://u:p@db:5432/d",
    "app_origin": "https://summit.facemark-tests.net",
    "scan_ref_secret": "k3J9vQ2mT7xZpL4nB8rD1wY6hC0sGfAe",
    "demo_mode": False,
    "resend_api_key": "re_testkey_0123456789abcdef",
    "otp_from_email": "FACEMARK <info@facemark-tests.net>",
    "trusted_proxy_cidrs": "172.30.0.0/24",
    # Test-only key (32 arbitrary bytes, URL-safe base64) - never used anywhere real.
    "otp_outbox_encryption_key": "AwoRGB8mLTQ7QklQV15lbHN6gYiPlp2kq7K5wMfO1dw=",
}


def production_settings(**overrides: Any) -> Settings:
    values = {**VALID_PRODUCTION, **overrides}
    return Settings(_env_file=None, **values)
