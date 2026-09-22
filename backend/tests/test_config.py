"""M01 acceptance: required configuration fails loudly, never silently.

ARCHITECTURE.md invariant #14: "No secret has a source-code fallback
default. Missing configuration fails at startup." DATABASE_URL and
ENVIRONMENT are the two required M01 settings; both must raise rather
than fall back to a guessed value.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_missing_database_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "development")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_missing_environment_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/d")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_load_successfully_when_both_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/d")
    settings = Settings(_env_file=None)
    assert settings.environment == "development"
    assert settings.is_production is False
