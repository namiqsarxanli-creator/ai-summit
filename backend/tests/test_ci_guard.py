"""The CI guard refuses anything that is not the job's own throw-away database
and fake credentials (tests/ci_guard.py)."""

from __future__ import annotations

import pytest

from tests.ci_guard import check_environment

GOOD = {
    "DATABASE_URL": "postgresql+psycopg://ci:ci_only_not_a_secret@localhost:5432/ci_maintenance",
    "ENVIRONMENT": "development",
}


def test_the_workflows_own_settings_pass() -> None:
    assert check_environment(GOOD) == []
    assert check_environment({**GOOD, "RESEND_API_KEY": "", "OTP_OUTBOX_ENCRYPTION_KEY": " "}) == []
    service = GOOD["DATABASE_URL"].replace("localhost", "postgres")
    assert check_environment({**GOOD, "DATABASE_URL": service}) == []


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://facemark:pw@db:5432/facemark",  # the dev stack's database
        "postgresql+psycopg://u:p@db.internal.example.org:5432/ci_x",  # a remote host
        "postgresql+psycopg://ci:pw@localhost:5432/facemark",  # right host, not a CI database
        "postgresql+psycopg://ci:pw@localhost:5432/",  # no database
        "not a url",
    ],
)
def test_a_database_that_is_not_the_jobs_own_is_refused(url: str) -> None:
    problems = check_environment({**GOOD, "DATABASE_URL": url})
    assert problems
    # variable names and rules only: no part of the URL is echoed
    for problem in problems:
        assert "pw" not in problem.split("DATABASE_URL")[0] and "internal.example" not in problem


def test_a_missing_database_url_is_refused() -> None:
    assert check_environment({"ENVIRONMENT": "development"}) == ["DATABASE_URL is not set"]


@pytest.mark.parametrize(
    "name", ["RESEND_API_KEY", "OTP_OUTBOX_ENCRYPTION_KEY", "OTP_OUTBOX_ENCRYPTION_KEY_PREVIOUS"]
)
def test_real_looking_credentials_are_refused_without_being_printed(name: str) -> None:
    secret = "re_live_SuperSecretValue123"
    problems = check_environment({**GOOD, name: secret})
    assert len(problems) == 1 and name in problems[0] and secret not in problems[0]


def test_production_is_refused() -> None:
    assert any("production" in p for p in check_environment({**GOOD, "ENVIRONMENT": "Production"}))
