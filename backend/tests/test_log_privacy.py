"""Logs must not carry personal data or credentials (ARCHITECTURE.md 21.11).

Two leaks this file pins down, beyond the card-token and export-ticket masking
covered in test_contact_cards.py / test_export_log_privacy.py:

* the admin screens search with GET parameters (`?q=<name, e-mail or phone>`,
  `?email=<address>`), and uvicorn's access log prints the whole URL;
* a database error's DETAIL line quotes the offending VALUES
  (`Key (email)=(ada@example.com) already exists`), and an unhandled one is
  logged with its traceback.
"""

from __future__ import annotations

import logging
import sys

import pytest

from app.core.privacy import (
    CardTokenLogFilter,
    mask_exception_data,
    mask_search_params,
    mask_secrets,
)
from app.worker import run_once
from tests.outbox_support import (
    FakeProvider,
    accepted,
    clean_outbox,
    make_delivery,
    outbox_settings,
)


@pytest.mark.parametrize(
    ("line", "must_not_contain", "must_contain"),
    [
        (
            '127.0.0.1 - "GET /api/admin/attendees?q=ada%40example.com&limit=50 HTTP/1.1" 200',
            "ada",
            "?q=***&limit=50",
        ),
        (
            '"GET /api/admin/attendees?limit=50&q=%2B994500000000&status=ACTIVE HTTP/1.1" 200',
            "994",
            "limit=50&q=***",
        ),
        (
            '"GET /api/admin/otp-deliveries?email=n%2A%2A%2A@gmail.com&limit=20 HTTP/1.1" 200',
            "gmail",
            "email=***&limit=20",
        ),
        ('"GET /api/admin/attendees?Q=Ada+Lovelace HTTP/1.1" 200', "Lovelace", "Q=***"),
        ('"GET /api/admin/attendees?q%3Dada@example.com HTTP/1.1" 200', "ada@", "q%3D***"),
    ],
)
def test_admin_search_terms_are_hidden_in_request_lines(
    line: str, must_not_contain: str, must_contain: str
) -> None:
    masked = mask_secrets(line)
    assert must_not_contain not in masked and must_contain in masked


def test_other_parameters_and_text_are_left_alone() -> None:
    line = '"GET /api/admin/audit?limit=50&action=EXPORT HTTP/1.1" 200 the q is quiet'
    assert mask_secrets(line) == line
    assert mask_search_params("a squad=3&eq=1") == "a squad=3&eq=1"


def test_database_error_detail_values_are_hidden() -> None:
    detail = (
        "psycopg.errors.UniqueViolation: duplicate key value violates unique constraint "
        '"uq_users_email"\nDETAIL:  Key (email)=(ada@example.com) already exists.\n'
        "DETAIL:  Failing row contains (12, ada@example.com, +994500000000, Ada).\n"
    )
    masked = mask_exception_data(detail)
    for leaked in ("ada@example.com", "+994500000000", "Ada)"):
        assert leaked not in masked
    assert "Key (email)=(***) already exists" in masked and "uq_users_email" in masked


def _record_with_traceback(message: str) -> logging.LogRecord:
    try:
        raise RuntimeError(message)
    except RuntimeError:
        return logging.LogRecord(
            "app.main",
            logging.ERROR,
            __file__,
            1,
            "Unhandled exception on %s",
            ("/x",),
            sys.exc_info(),
        )


def test_the_log_filter_scrubs_an_exceptions_text_before_any_handler_prints_it() -> None:
    record = _record_with_traceback("Key (email)=(grace@example.org) already exists; ?q=grace")
    CardTokenLogFilter().filter(record)
    assert "grace@example.org" not in (record.exc_text or "")
    assert "Key (email)=(***)" in (record.exc_text or "")


def test_an_ordinary_log_line_keeps_its_arguments() -> None:
    # only a record that actually held a secret is rewritten
    record = logging.LogRecord(
        "app", logging.INFO, __file__, 1, "OTP delivery %s: %s", (7, "accepted"), None
    )
    CardTokenLogFilter().filter(record)
    assert record.args == (7, "accepted") and record.getMessage() == "OTP delivery 7: accepted"


def test_a_full_delivery_logs_neither_the_address_nor_the_code(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Whole path in one go: enqueue, claim, render, send, record - at DEBUG,
    on every logger (the worker's, SQLAlchemy's, httpx's)."""
    settings = outbox_settings()
    monkeypatch.setattr("app.modules.auth.otp_outbox.get_settings", lambda: settings)
    clean_outbox()
    try:
        caplog.set_level(logging.DEBUG)
        _, _, address = make_delivery(code="918273")
        provider = FakeProvider(accepted())
        assert run_once(provider=provider, worker_id="privacy") == 1
        assert provider.messages and "918273" in provider.messages[0].text  # it really was sent
        for secret in (address, address.split("@")[0], "918273", settings.resend_api_key or "x"):
            assert secret not in caplog.text, secret
    finally:
        clean_outbox()
