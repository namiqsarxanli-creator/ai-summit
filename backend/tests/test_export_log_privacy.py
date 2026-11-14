"""CSV export tickets never reach a log line.

The download link carries a one-time ticket in its query string
(`/api/admin/exports/download?ticket=...`). A ticket is short-lived and
single-use, but while it is live it is a credential for personal data, so every
place the application logs a request URL must hide it - whatever the order of
the query parameters and however the name or the `=` is percent-encoded.

The unit tests exercise the masking itself; `test_a_real_uvicorn_never_logs...`
starts the real server in a child process (uvicorn's own logging set-up, then
the application's) and reads everything it printed.
"""

from __future__ import annotations

import logging
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.privacy import (
    CardTokenLogFilter,
    install_log_masking,
    mask_card_tokens,
    mask_known_tokens,
    mask_secrets,
    mask_ticket_params,
    remember_secret,
)
from app.core.sessions import create_staff_session
from app.modules.admin.export_tickets import tickets
from app.modules.staff.models import StaffAccount, StaffRole
from tests.reset_support import subprocess_env

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DOWNLOAD = "/api/admin/exports/download"


def _token() -> str:
    return secrets.token_urlsafe(32)


# --- the masking itself -----------------------------------------------------------

VARIANTS = [
    # order
    "{p}?ticket={t}",
    "{p}?ticket={t}&x=1",
    "{p}?x=1&ticket={t}",
    "{p}?x=1&ticket={t}&y=2",
    "{p}?a=1&b=2&c=3&ticket={t}&d=4",
    # name case
    "{p}?TICKET={t}",
    "{p}?Ticket={t}",
    # percent-encoded name (any letters), upper- and lower-case hex
    "{p}?%74icket={t}",
    "{p}?t%69cket={t}&x=1",
    "{p}?x=1&tick%65t={t}",
    "{p}?%74%69%63%6b%65%74={t}",
    "{p}?%54%49%43%4B%45%54={t}",
    "{p}?%74%69%63%6B%65%74={t}",
    # percent-encoded '='
    "{p}?ticket%3D{t}",
    "{p}?ticket%3d{t}&x=1",
    # encoded twice
    "{p}?%2574icket={t}",
    "{p}?ticket%253D{t}",
    # repeated
    "{p}?ticket={t}&ticket={t}",
    "{p}?ticket={t}&x=1&ticket={t}",
    # separator, fragment, surrounding text
    "{p}?x=1;ticket={t}",
    "{p}?ticket={t}#frag",
    "GET {p}?ticket={t} HTTP/1.1",
    'request: "{p}?ticket={t}" status=200',
    "https://summit.example{p}?ticket={t}&x=1",
    # the whole link inside another parameter's (encoded) value
    "/login?next=%2Fapi%2Fadmin%2Fexports%2Fdownload%3Fticket%3D{t}",
    "/login?next=%252Fapi%252Fadmin%252Fexports%252Fdownload%253Fticket%253D{t}",
    # blanks around the '=' that some loggers add
    "{p}?ticket ={t}",
]


@pytest.mark.parametrize("pattern", VARIANTS)
def test_the_ticket_is_hidden_whatever_the_order_or_encoding(pattern: str) -> None:
    token = _token()
    line = pattern.format(p=DOWNLOAD, t=token)
    masked = mask_ticket_params(line)
    assert token not in masked, masked
    assert "***" in masked
    assert mask_secrets(line).count(token) == 0


def test_other_parameters_and_the_path_survive_masking() -> None:
    token = _token()
    line = f"GET {DOWNLOAD}?x=1&ticket={token}&y=two HTTP/1.1"
    assert mask_ticket_params(line) == f"GET {DOWNLOAD}?x=1&ticket=***&y=two HTTP/1.1"
    assert mask_ticket_params(f"{DOWNLOAD}?ticket={token}&x=1") == f"{DOWNLOAD}?ticket=***&x=1"


@pytest.mark.parametrize(
    "line",
    [
        "GET /api/admin/attendees?q=ticket&limit=50 HTTP/1.1",
        "GET /api/admin/attendees?q=ticket%20desk HTTP/1.1",
        "GET /api/admin/attendees?ticketing=1&x=2 HTTP/1.1",
        "GET /api/admin/points-log?my_ticket=abc HTTP/1.1",
        "GET /api/health HTTP/1.1",
        "the ticket was refused",
        "ticket = needed",
        "",
    ],
)
def test_text_without_a_ticket_parameter_is_left_alone(line: str) -> None:
    assert mask_ticket_params(line) == line


def test_a_recently_issued_ticket_is_hidden_even_without_its_parameter_name() -> None:
    token = tickets.issue(actor_id=1, kind="attendees", filters=None)
    try:
        assert token not in mask_known_tokens(f"error while serving {token}: oops")
        assert token not in mask_secrets(f"{{'download': '{token}'}}")
    finally:
        tickets.clear()
    remember_secret("short")  # too short to register: ordinary words are never masked
    assert mask_known_tokens("a short sentence") == "a short sentence"


def test_card_token_masking_is_unchanged() -> None:
    card = _token()
    assert mask_card_tokens(f"GET /c/{card}") == "GET /c/***"
    assert mask_secrets(f"/api/cards/{card}?x=1") == "/api/cards/***?x=1"
    both = mask_secrets(f"/c/{card} then {DOWNLOAD}?ticket={_token()}")
    assert card not in both and both.startswith("/c/***") and "ticket=***" in both


def _record(msg: str, args: Any = None, exc_info: Any = None) -> logging.LogRecord:
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, msg, args, exc_info)


def test_the_log_filter_masks_message_arguments_and_exception_text() -> None:
    token = _token()
    path = f"{DOWNLOAD}?ticket={token}"
    flt = CardTokenLogFilter()

    access = _record('%s - "%s %s HTTP/%s" %d', ("203.0.113.9:1", "GET", path, "1.1", 200))
    assert flt.filter(access)
    assert token not in access.getMessage()
    assert "ticket=***" in access.getMessage()

    plain = _record(f"download failed for {path}")
    flt.filter(plain)
    assert token not in plain.getMessage()

    keyed = _record("%(path)s -> %(code)d", {"path": path, "code": 410})
    flt.filter(keyed)
    assert token not in keyed.getMessage()

    try:
        raise ValueError(f"cannot serve {path}")
    except ValueError:
        failing = _record("unhandled", exc_info=sys.exc_info())
    flt.filter(failing)
    rendered = logging.Formatter("%(message)s").format(failing)
    assert token not in rendered
    assert "ticket=***" in rendered and "Traceback" in rendered  # the traceback itself is kept


def test_a_record_without_a_secret_keeps_its_message_and_arguments_untouched() -> None:
    """Code reads records back (the DEMO_MODE OTP line is taken from `record.args`),
    so only a record that really held a secret may be rewritten."""
    flt = CardTokenLogFilter()
    args = ("a@x.test", "ACTIVATION", "123456")
    record = _record("[DEMO_MODE] OTP for %s (purpose=%s): %s", args)
    assert flt.filter(record)
    assert record.msg == "[DEMO_MODE] OTP for %s (purpose=%s): %s"
    assert record.args == args

    secret = _record("served %s?ticket=%s", (DOWNLOAD, _token()))
    flt.filter(secret)
    assert secret.args is None and "ticket=***" in secret.getMessage()


def test_masking_is_installed_on_the_loggers_and_on_their_handlers() -> None:
    probe = logging.StreamHandler()
    logging.getLogger().addHandler(probe)
    try:
        install_log_masking()
        install_log_masking()  # idempotent: --reload imports the app again
        for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "app"):
            logger = logging.getLogger(name)
            assert sum(isinstance(f, CardTokenLogFilter) for f in logger.filters) == 1, name
        assert sum(isinstance(f, CardTokenLogFilter) for f in probe.filters) == 1
        # a record from a CHILD logger that only a handler filter can see
        out: list[str] = []
        probe.setFormatter(logging.Formatter("%(message)s"))
        probe.emit = lambda record: out.append(probe.format(record))  # type: ignore[method-assign]
        token = _token()
        child = logging.getLogger("some.library.client")
        child.propagate = True
        child.warning("retrying %s?ticket=%s", DOWNLOAD, token)
        assert out and token not in out[0] and "ticket=***" in out[0]
    finally:
        logging.getLogger().removeHandler(probe)


# --- the real server ---------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Server:
    def __init__(self, url: str, port: int) -> None:
        self.port = port
        self.output: list[str] = []
        env = subprocess_env(url, ENVIRONMENT="development")
        self.proc = subprocess.Popen(  # noqa: S603 - fixed argv
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=str(BACKEND_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.output.append(line)

    def wait_ready(self) -> None:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                if (
                    httpx.get(f"http://127.0.0.1:{self.port}/api/health", timeout=2).status_code
                    == 200
                ):
                    return
            except httpx.HTTPError:
                time.sleep(0.3)
        raise AssertionError("the server did not start:\n" + "".join(self.output[-20:]))

    def stop(self) -> str:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self._reader.join(timeout=10)
        return "".join(self.output)


@pytest.fixture
def server(reset_db_url: str, clean_reset_db: str) -> Iterator[Server]:
    srv = Server(reset_db_url, _free_port())
    try:
        srv.wait_ready()
        yield srv
    finally:
        if srv.proc.poll() is None:
            srv.stop()


def test_a_real_uvicorn_never_logs_a_ticket_or_a_session_whatever_the_url_looks_like(
    server: Server, reset_db_engine: Engine
) -> None:
    with Session(reset_db_engine) as db:
        admin = (
            db.execute(select(StaffAccount).where(StaffAccount.role == StaffRole.ADMIN))
            .scalars()
            .first()
        )
        assert admin is not None
        session_token = create_staff_session(db, staff_account_id=admin.id, now=datetime.now(UTC))
        db.commit()

    base = f"http://127.0.0.1:{server.port}"
    secrets_seen: list[str] = [session_token]
    card = _token()
    with httpx.Client(
        base_url=base,
        headers={"Origin": get_settings().app_origin},
        cookies={"session": session_token},
        timeout=30,
    ) as client:

        def fresh() -> str:
            response = client.post("/api/admin/exports/attendees", json={})
            assert response.status_code == 200, response.text
            token = str(response.json()["download_url"]).partition("ticket=")[2]
            secrets_seen.append(token)
            return token

        # each variant needs its own ticket: they are single-use
        variants: list[Callable[[str], str]] = [
            lambda t: f"{DOWNLOAD}?ticket={t}",
            lambda t: f"{DOWNLOAD}?x=1&ticket={t}",
            lambda t: f"{DOWNLOAD}?ticket={t}&x=1",
            lambda t: f"{DOWNLOAD}?%74icket={t}",
            lambda t: f"{DOWNLOAD}?t%69cket={t}&x=1",
            lambda t: f"{DOWNLOAD}?%54%49%43%4B%45%54={t}",  # upper-case name: not the parameter
        ]
        statuses = []
        for make in variants:
            token = fresh()
            statuses.append(client.get(make(token)).status_code)
        assert statuses[:5] == [200, 200, 200, 200, 200], statuses

        used = fresh()
        client.get(f"{DOWNLOAD}?ticket={used}")
        assert (
            client.get(f"{DOWNLOAD}?ticket={used}").status_code == 410
        )  # replay: an error log path
        garbage = _token()
        secrets_seen.append(garbage)
        assert client.get(f"{DOWNLOAD}?ticket={garbage}").status_code == 410
        client.get(f"/c/{card}")  # a card token still gets masked next to it

    output = server.stop()

    for secret in secrets_seen:
        assert secret not in output, "a credential reached the server's log"
    assert card not in output
    assert output.count("ticket=***") >= 6  # the access lines are there, masked
    assert "/c/***" in output
    assert DOWNLOAD in output  # ... and the log was not simply empty
