"""Operating the outbox: key requirements, process start-up, CLI.

The process tests start real child processes with explicit environments
(the container's own variables are not inherited), pointed at the suite's
throw-away database where a database is needed.
"""

from __future__ import annotations

import base64
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app import cli
from app.core.config import Settings, decode_outbox_key
from app.modules.auth import otp_outbox as ob
from app.modules.auth.otp_crypto import generate_key
from tests.m04_support import fetch_one
from tests.outbox_support import (
    TEST_KEY,
    clean_outbox,
    count,
    execute,
    make_delivery,
    row,
)
from tests.prod_settings import VALID_PRODUCTION, production_settings

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SECRET_MARKERS = (
    VALID_PRODUCTION["scan_ref_secret"],
    VALID_PRODUCTION["resend_api_key"],
    TEST_KEY,
)


# --- configuration -----------------------------------------------------------------


def test_production_requires_the_encryption_key() -> None:
    with pytest.raises(ValidationError, match="OTP_OUTBOX_ENCRYPTION_KEY is required"):
        production_settings(otp_outbox_encryption_key=None)
    with pytest.raises(ValidationError, match="OTP_OUTBOX_ENCRYPTION_KEY is required"):
        production_settings(otp_outbox_encryption_key="")
    assert production_settings().outbox_enabled


@pytest.mark.parametrize(
    "bad",
    [
        "short",
        "not base64 !!!",
        base64.urlsafe_b64encode(b"x" * 16).decode(),  # 16 bytes: too short for AES-256
        base64.urlsafe_b64encode(b"x" * 48).decode(),
        "REPLACE_ME_with_a_generated_key",
    ],
)
def test_a_malformed_key_is_rejected_without_echoing_it(bad: str) -> None:
    with pytest.raises(ValidationError) as exc:
        production_settings(otp_outbox_encryption_key=bad)
    message = str(exc.value)
    assert "OTP_OUTBOX_ENCRYPTION_KEY" in message and bad not in message
    with pytest.raises(ValidationError, match="OTP_OUTBOX_ENCRYPTION_KEY_PREVIOUS"):
        production_settings(otp_outbox_encryption_key_previous=bad)


def test_outside_production_the_key_is_optional_and_gates_the_outbox() -> None:
    values: dict[str, Any] = {
        "environment": "development",
        "database_url": "postgresql+psycopg://u:p@db:5432/d",
        "app_origin": "http://localhost:3000",
        "scan_ref_secret": "dev-secret",
        "resend_api_key": "re_dev_key",
    }
    assert not Settings(_env_file=None, **values).outbox_enabled  # legacy sender
    assert Settings(_env_file=None, **values, otp_outbox_encryption_key=TEST_KEY).outbox_enabled
    no_email = {k: v for k, v in values.items() if k != "resend_api_key"}
    nothing_to_send = Settings(_env_file=None, **no_email, otp_outbox_encryption_key=TEST_KEY)
    assert not nothing_to_send.outbox_enabled


def test_a_generated_key_is_valid_and_unique() -> None:
    keys = {generate_key() for _ in range(5)}
    assert len(keys) == 5 and all(len(decode_outbox_key(k)) == 32 for k in keys)


def test_generate_otp_key_command_prints_only_a_usable_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.main(["generate-otp-key"])
    out = capsys.readouterr().out.strip()
    assert len(decode_outbox_key(out)) == 32 and "\n" not in out


# --- process start-up -----------------------------------------------------------------


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108
        "DATABASE_URL": os.environ["DATABASE_URL"],  # the suite database
        "ENVIRONMENT": "production",
        "DEMO_MODE": "false",
        "APP_ORIGIN": VALID_PRODUCTION["app_origin"],
        "SCAN_REF_SECRET": VALID_PRODUCTION["scan_ref_secret"],
        "RESEND_API_KEY": VALID_PRODUCTION["resend_api_key"],
        "OTP_FROM_EMAIL": VALID_PRODUCTION["otp_from_email"],
        "TRUSTED_PROXY_CIDRS": VALID_PRODUCTION["trusted_proxy_cidrs"],
        "OTP_OUTBOX_ENCRYPTION_KEY": TEST_KEY,
        "OTP_WORKER_HEARTBEAT_FILE": f"/tmp/otp-heartbeat-{os.getpid()}",  # noqa: S108
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v != "__unset__"}


@pytest.mark.parametrize(
    "command",
    [
        [sys.executable, "-m", "app.worker"],
        [sys.executable, "-c", "import app.main"],
    ],
    ids=["worker", "web"],
)
def test_production_processes_refuse_to_start_without_the_key(command: list[str]) -> None:
    done = subprocess.run(  # noqa: S603 - fixed argv
        command,
        cwd=BACKEND_ROOT,
        env=_env(OTP_OUTBOX_ENCRYPTION_KEY="__unset__"),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert done.returncode != 0
    assert "OTP_OUTBOX_ENCRYPTION_KEY is required" in done.stderr
    for secret in SECRET_MARKERS:
        assert secret not in done.stderr + done.stdout


def _run_worker_briefly(env: dict[str, str], seconds: float = 4.0) -> tuple[int, str]:
    proc = subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "app.worker"],
        cwd=BACKEND_ROOT,
        env=env,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    time.sleep(seconds)
    assert proc.poll() is None, "the worker exited by itself"
    proc.send_signal(signal.SIGTERM)
    _, err = proc.communicate(timeout=30)
    return proc.returncode, err


def test_the_worker_starts_heartbeats_and_stops_cleanly_on_sigterm() -> None:
    heartbeat = Path(_env()["OTP_WORKER_HEARTBEAT_FILE"])
    heartbeat.unlink(missing_ok=True)

    code, err = _run_worker_briefly(_env())

    assert code == 0
    assert "OTP worker" in err and "started" in err and "stopped" in err
    assert heartbeat.exists()
    heartbeat.unlink(missing_ok=True)
    for secret in SECRET_MARKERS:
        assert secret not in err


def test_the_worker_picks_up_a_queued_row_in_a_real_process() -> None:
    """End to end in a child process, with the provider made unreachable
    (a dead proxy): the real worker wiring must claim the row and record a
    network failure, and its logs must stay free of secrets."""
    clean_outbox()
    _, delivery_id, address = make_delivery(code="864209")
    try:
        env = _env(HTTPS_PROXY="http://127.0.0.1:9", ALL_PROXY="http://127.0.0.1:9")
        code, err = _run_worker_briefly(env, seconds=5.0)

        assert code == 0
        r = row(delivery_id)
        assert r["attempt_count"] >= 1
        assert r["status"] in {"pending", "processing", "failed"}
        for secret in (address, "864209", VALID_PRODUCTION["resend_api_key"]):
            assert secret not in err, secret
    finally:
        clean_outbox()


def test_without_an_outbox_a_development_worker_idles_instead_of_crashing() -> None:
    env = _env(ENVIRONMENT="development", OTP_OUTBOX_ENCRYPTION_KEY="__unset__")
    env.update(
        APP_ORIGIN="http://localhost:3000",
        TRUSTED_PROXY_CIDRS="",
        SCAN_REF_SECRET="dev-only-secret",
    )

    code, err = _run_worker_briefly(env, seconds=3.0)

    assert code == 0 and "idling" in err


# --- cleanup command ---------------------------------------------------------------------


def test_cleanup_command_expires_lapsed_rows_and_deletes_old_finished_ones(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_outbox()
    try:
        lapsed = make_delivery()[1]
        execute(
            "UPDATE otp_deliveries SET expires_at = now() - interval '1 minute' WHERE id = :i",
            i=lapsed,
        )
        old = make_delivery()[1]
        execute(
            "UPDATE otp_deliveries SET status = 'failed', reason_code = 'network_error', "
            "payload_ciphertext = NULL, payload_key_id = NULL, "
            "finished_at = now() - interval '45 days' WHERE id = :i",
            i=old,
        )
        live = make_delivery()[1]

        cli.main(["otp-outbox-cleanup", "--older-than-days", "30"])

        out = capsys.readouterr().out
        assert "expired (lapsed before sending): 1" in out
        assert "deleted (> 30 days): 1" in out
        assert row(lapsed)["status"] == "expired" and row(lapsed)["payload_ciphertext"] is None
        assert count("otp_deliveries", "id = :i", i=old) == 0
        assert row(live)["status"] == "pending" and row(live)["payload_ciphertext"] is not None
        assert fetch_one("SELECT count(*) FROM otp_challenges")[0] >= 1  # challenges untouched
    finally:
        clean_outbox()


def test_cleanup_command_refuses_a_nonsensical_retention() -> None:
    with pytest.raises(ValueError):
        cli.main(["otp-outbox-cleanup", "--older-than-days", "0"])
    assert ob.DEFAULT_RETENTION_DAYS == 30
