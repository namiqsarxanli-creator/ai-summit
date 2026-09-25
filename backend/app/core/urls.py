"""Validation shared by registration and public-card contact selection."""

from __future__ import annotations

from urllib.parse import urlsplit


def is_linkedin_url(value: str) -> bool:
    """Accept only a real LinkedIn host, never a lookalike or userinfo URL."""
    if value != value.strip() or any(c.isspace() or ord(c) < 32 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        return (
            parsed.scheme in {"http", "https"}
            and host is not None
            and (host == "linkedin.com" or host.endswith(".linkedin.com"))
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
        )
    except ValueError:
        return False
