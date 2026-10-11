"""Shared fixtures and helpers for the attendee-profile suites (edit, consent,
photo, media): real PostgreSQL, real registration and activation, a throw-away
MEDIA_ROOT per test. Import the fixtures into a test module with
`from tests.profile_support import *  # noqa: F403`.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.core.config import get_settings
from tests.m04_support import (
    captured_otp,
    cleanup_email,
    make_client,
    new_email,
    register_payload,
)

REGISTER = "/api/attendees/register"
VERIFY = "/api/auth/otp/verify"
PROFILE = "/api/attendees/me/profile"
PROFILE_PUT = "/api/attendees/me/profile"
CONSENT = "/api/attendees/me/sponsor-consent"
PHOTO = "/api/attendees/me/photo"

__all__ = [
    "CONSENT",
    "PHOTO",
    "PROFILE",
    "attendee_fixture",
    "emails_fixture",
    "error_of",
    "jpeg_bytes",
    "media_root_fixture",
    "png_bytes",
    "profile_body",
]


def error_of(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def png_bytes(
    size: tuple[int, int] = (200, 160), color: Any = (200, 30, 30), mode: str = "RGB"
) -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, "PNG")
    return out.getvalue()


def jpeg_bytes(size: tuple[int, int] = (300, 200), color: Any = (30, 90, 200)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG")
    return out.getvalue()


def profile_body(profile: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """The PUT body that re-sends the profile as it stands (what an untouched form
    holds), with `overrides` applied."""
    body: dict[str, Any] = {
        "first_name": profile["first_name"],
        "last_name": profile["last_name"],
        "phone": profile["phone"],
        "profession": profile["profession"],
        "linkedin_url": profile["linkedin_url"],
        "interest_slugs": [i["slug"] for i in profile["interests"]],
        "expected_version": profile["version"],
    }
    body.update(overrides)
    return body


@pytest.fixture(name="media_root")
def media_root_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A throw-away photo store: MEDIA_ROOT points at a temporary directory for
    this test only."""
    root = tmp_path / "media"
    monkeypatch.setenv("MEDIA_ROOT", str(root))
    get_settings.cache_clear()
    yield root
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.fixture(name="emails")
def emails_fixture() -> Iterator[Callable[[], str]]:
    issued: list[str] = []

    def issue() -> str:
        email = new_email("prof")
        issued.append(email)
        return email

    yield issue
    for email in issued:
        cleanup_email(email)


@pytest.fixture(name="attendee")
def attendee_fixture(
    otp_log: pytest.LogCaptureFixture,
) -> Iterator[Callable[..., tuple[str, TestClient]]]:
    """Registers and activates. Returns (email, a client holding the session)."""
    clients: list[TestClient] = []

    def make(email: str, **overrides: Any) -> tuple[str, TestClient]:
        c = make_client()
        clients.append(c)
        assert c.post(REGISTER, json=register_payload(email, **overrides)).status_code == 200
        code = captured_otp(otp_log.records, email)
        assert c.post(VERIFY, json={"email": email, "code": code}).status_code == 200
        return email, c

    yield make
    for c in clients:
        c.close()
