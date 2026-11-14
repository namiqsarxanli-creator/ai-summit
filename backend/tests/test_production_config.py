"""Production configuration guard.

`Settings` is the last line of defence against a misconfigured
deployment: ENVIRONMENT=production must refuse to start on anything
that would make the live site insecure or non-functional. Every case
builds `Settings` from explicit values only (see tests/prod_settings.py).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from tests.prod_settings import VALID_PRODUCTION, production_settings


def test_a_complete_production_configuration_is_accepted() -> None:
    settings = production_settings()
    assert settings.is_production
    assert settings.serves_https  # Secure session cookie follows from the https origin


@pytest.mark.parametrize(
    ("overrides", "complaint"),
    [
        ({"app_origin": "http://summit.facemark-tests.net"}, "APP_ORIGIN must be an https"),
        ({"app_origin": "https://localhost"}, "APP_ORIGIN must be the real public domain"),
        ({"app_origin": "https://127.0.0.1:3443"}, "APP_ORIGIN must be the real public domain"),
        ({"app_origin": "https://192.168.31.211"}, "APP_ORIGIN must be the real public domain"),
        ({"app_origin": "https://summit.example.invalid"}, "APP_ORIGIN must be the real public"),
        ({"app_origin": "https://www.example.com"}, "APP_ORIGIN must be the real public"),
        (
            {"scan_ref_secret": "facemark_dev_only_change_me_scan_ref_secret"},
            "SCAN_REF_SECRET is a known development value",
        ),
        (
            {"scan_ref_secret": "REPLACE_ME_with_output_of_openssl_rand_hex_32"},
            "SCAN_REF_SECRET is a known development value",
        ),
        ({"scan_ref_secret": "too-short-but-unique"}, "SCAN_REF_SECRET must be at least"),
        ({"resend_api_key": None}, "RESEND_API_KEY is required"),
        ({"resend_api_key": ""}, "RESEND_API_KEY is required"),
        ({"resend_api_key": "re_REPLACE_ME"}, "RESEND_API_KEY is required"),
        (
            {"otp_from_email": "FACEMARK <info@summit.example.org>"},
            "OTP_FROM_EMAIL must use your verified sending domain",
        ),
        ({"trusted_proxy_cidrs": ""}, "TRUSTED_PROXY_CIDRS must list"),
        ({"trusted_proxy_cidrs": "0.0.0.0/0"}, "TRUSTED_PROXY_CIDRS may only contain private"),
        (
            {"trusted_proxy_cidrs": "172.30.0.0/24,8.8.8.0/24"},
            "TRUSTED_PROXY_CIDRS may only contain private",
        ),
    ],
)
def test_unsafe_production_values_are_refused(overrides: dict[str, Any], complaint: str) -> None:
    with pytest.raises(ValidationError, match=complaint):
        production_settings(**overrides)


def test_demo_mode_is_refused_in_production() -> None:
    with pytest.raises(ValidationError, match="DEMO_MODE"):
        production_settings(demo_mode=True)


def test_the_from_address_must_be_set_explicitly_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OTP_FROM_EMAIL", raising=False)  # the container may carry one
    values = {k: v for k, v in VALID_PRODUCTION.items() if k != "otp_from_email"}
    with pytest.raises(ValidationError, match="OTP_FROM_EMAIL must be set explicitly"):
        Settings(_env_file=None, **values)


def test_every_problem_is_reported_in_one_run_without_leaking_values() -> None:
    secret = "facemark_dev_only_change_me_scan_ref_secret"
    with pytest.raises(ValidationError) as exc:
        production_settings(
            app_origin="http://localhost:3000",
            scan_ref_secret=secret,
            resend_api_key=None,
            trusted_proxy_cidrs="",
        )
    message = str(exc.value)
    for expected in ("APP_ORIGIN", "SCAN_REF_SECRET", "RESEND_API_KEY", "TRUSTED_PROXY_CIDRS"):
        assert expected in message
    assert secret not in message  # names only, never the values
    assert VALID_PRODUCTION["resend_api_key"] not in message
    assert VALID_PRODUCTION["database_url"] not in message


@pytest.mark.parametrize("typo", ["prod", "producton", "staging", "", "live"])
def test_a_misspelled_environment_is_refused(typo: str) -> None:
    """A typo must not silently mean "not production" and skip the guards."""
    with pytest.raises(ValidationError, match="ENVIRONMENT must be one of"):
        production_settings(environment=typo)


@pytest.mark.parametrize("spelling", ["Production", " production ", "PRODUCTION"])
def test_environment_is_normalised_so_case_cannot_disable_the_guards(spelling: str) -> None:
    assert production_settings(environment=spelling).environment == "production"
    with pytest.raises(ValidationError, match="APP_ORIGIN"):
        production_settings(environment=spelling, app_origin="http://summit.facemark-tests.net")


def test_development_stays_permissive() -> None:
    settings = Settings(
        _env_file=None,
        environment="development",
        database_url="postgresql+psycopg://u:p@db:5432/d",
        app_origin="http://localhost:3000",
        scan_ref_secret="facemark_dev_only_change_me_scan_ref_secret",
    )
    assert not settings.is_production
    assert not settings.serves_https
