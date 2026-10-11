"""Operator visibility: liveness vs readiness, `ops-check`, media write failures.

Public endpoints answer up/down and nothing else; the detail is a read-only
CLI whose output is made only of counts, ages and fixed reason codes.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import cli
from app.core import ops_status as ops
from app.core.config import get_settings
from app.core.media import MediaStorageError, MediaStore
from app.db.session import SessionLocal
from tests.outbox_support import clean_outbox, execute, make_delivery, outbox_settings

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _outbox(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    settings = outbox_settings()
    monkeypatch.setattr("app.modules.auth.otp_outbox.get_settings", lambda: settings)
    clean_outbox()
    yield
    clean_outbox()


def levels(results: list[ops.CheckResult]) -> dict[str, str]:
    return {r.name: r.level for r in results}


def outbox_results() -> list[ops.CheckResult]:
    db = SessionLocal()
    try:
        return ops.check_outbox(db)
    finally:
        db.close()


def finish(delivery_id: int, status: str, reason: str) -> None:
    execute(
        "UPDATE otp_deliveries SET status = :s, reason_code = :r, payload_ciphertext = NULL, "
        "payload_key_id = NULL, finished_at = now() WHERE id = :i",
        s=status,
        r=reason,
        i=delivery_id,
    )


# --- public endpoints: liveness is not readiness ---------------------------------------


def test_liveness_never_touches_the_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode() -> bool:
        raise AssertionError("liveness must not query the database")

    monkeypatch.setattr("app.main.database_is_reachable", explode)
    response = client.get("/api/health/live")
    assert response.status_code == 200 and response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"


def test_readiness_follows_the_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = client.get("/api/health/ready")
    assert ok.status_code == 200 and ok.json() == {"status": "ok"}

    monkeypatch.setattr("app.main.database_is_reachable", lambda: False)
    down = client.get("/api/health/ready")
    assert down.status_code == 503 and down.json() == {"status": "unavailable"}
    assert down.headers["cache-control"] == "no-store"
    # still alive while not ready: that is the whole difference
    assert client.get("/api/health/live").status_code == 200


def test_public_health_bodies_carry_no_diagnostics(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.main.database_is_reachable", lambda: False)
    body = client.get("/api/health/ready").text + client.get("/api/health/live").text
    secret = get_settings().database_url
    for forbidden in (
        secret.split("@")[-1],
        "postgres",
        "Traceback",
        "version",
        "worker",
        "outbox",
    ):
        assert forbidden.lower() not in body.lower()


# --- the database and migrations ------------------------------------------------------------


def test_database_check_reports_ok_and_never_leaks_the_connection_string() -> None:
    engine = create_engine(get_settings().database_url)
    assert ops.check_database(engine).level == "ok"

    dead = create_engine(
        "postgresql+psycopg://nobody:Sup3rSecretPw@127.0.0.1:1/none",
        connect_args={"connect_timeout": 1},
    )
    result = ops.check_database(dead)
    assert result.level == "crit" and "unreachable" in result.detail
    assert "Sup3rSecretPw" not in result.detail and "127.0.0.1" not in result.detail


def test_migration_check_compares_the_database_with_the_code() -> None:
    db = SessionLocal()
    try:
        head = ops.code_heads()
        assert ops.check_migrations(db).level == "ok"
        stale = ops.check_migrations(db, heads=lambda: {"9999_not_a_revision"})
        assert stale.level == "warn" and "run the migrate step" in stale.detail
        assert len(head) == 1
    finally:
        db.close()


# --- the OTP outbox ---------------------------------------------------------------------------


def test_an_empty_outbox_is_healthy() -> None:
    assert levels(outbox_results()) == {"outbox-backlog": "ok", "outbox-failures": "ok"}


def test_a_row_waiting_too_long_means_a_stopped_or_blocked_worker() -> None:
    _, delivery_id, _ = make_delivery()
    for age, expected in ((10, "ok"), (90, "warn"), (240, "crit")):
        execute(
            "UPDATE otp_deliveries SET next_attempt_at = now() - make_interval(secs => :a) "
            "WHERE id = :i",
            a=age,
            i=delivery_id,
        )
        assert levels(outbox_results())["outbox-backlog"] == expected, age


def test_an_abandoned_lease_is_reported() -> None:
    _, delivery_id, _ = make_delivery()
    execute(
        "UPDATE otp_deliveries SET status = 'processing', locked_by = 'w', "
        "locked_until = now() - interval '10 minutes' WHERE id = :i",
        i=delivery_id,
    )
    backlog = next(r for r in outbox_results() if r.name == "outbox-backlog")
    assert backlog.level == "warn" and "expired leases=1" in backlog.detail


def test_retrying_rows_are_counted_but_are_not_an_alarm() -> None:
    _, delivery_id, _ = make_delivery()
    execute(
        "UPDATE otp_deliveries SET attempt_count = 2, "
        "next_attempt_at = now() + interval '30 seconds' WHERE id = :i",
        i=delivery_id,
    )
    backlog = next(r for r in outbox_results() if r.name == "outbox-backlog")
    assert backlog.level == "ok" and "retrying=1" in backlog.detail


def test_systemic_failures_are_critical_and_a_single_bounce_is_a_warning() -> None:
    _, bounced, _ = make_delivery()
    finish(bounced, "failed", "provider_rejected_message")
    failures = next(r for r in outbox_results() if r.name == "outbox-failures")
    assert failures.level == "warn" and "failed:provider_rejected_message=1" in failures.detail

    _, rejected_key, _ = make_delivery()
    finish(rejected_key, "failed", "provider_auth_rejected")
    failures = next(r for r in outbox_results() if r.name == "outbox-failures")
    assert failures.level == "crit" and "systemic: provider_auth_rejected" in failures.detail


def test_codes_that_lapsed_unsent_escalate_with_their_number() -> None:
    ids = [make_delivery()[1] for _ in range(5)]
    finish(ids[0], "expired", "otp_expired")
    assert levels(outbox_results())["outbox-failures"] == "warn"
    for i in ids[1:]:
        finish(i, "expired", "otp_expired")
    assert levels(outbox_results())["outbox-failures"] == "crit"


def test_superseded_codes_are_normal_and_old_failures_age_out() -> None:
    _, consumed, _ = make_delivery()
    finish(consumed, "expired", "challenge_consumed")
    _, old, _ = make_delivery()
    finish(old, "failed", "provider_unavailable")
    execute(
        "UPDATE otp_deliveries SET finished_at = now() - interval '2 hours' WHERE id = :i", i=old
    )
    assert levels(outbox_results())["outbox-failures"] == "ok"


def test_outbox_details_contain_no_addresses_codes_or_ciphertext() -> None:
    _, delivery_id, address = make_delivery(code="918273")
    finish(delivery_id, "failed", "provider_rejected_message")
    _, due_id, _ = make_delivery(code="555666")
    execute(
        "UPDATE otp_deliveries SET next_attempt_at = now() - interval '5 minutes' WHERE id = :i",
        i=due_id,
    )
    rendered = ops.format_text(outbox_results()) + ops.format_json(outbox_results())
    for secret in (address, "918273", "555666", "ciphertext"):
        assert secret not in rendered


# --- the worker's own files --------------------------------------------------------------------


def write_status(path: Path, **fields: object) -> None:
    path.write_text(json.dumps(fields), encoding="utf-8")


def test_worker_check_distinguishes_healthy_stalled_failing_and_paused(tmp_path: Path) -> None:
    status = tmp_path / "status.json"

    def check() -> ops.CheckResult:
        return ops.check_worker(status, now=NOW)

    write_status(status, loop_at=NOW - 2, last_ok_at=NOW - 2, consecutive_failures=0)
    assert check().level == "ok"

    write_status(status, loop_at=NOW - 300, last_ok_at=NOW - 300, consecutive_failures=0)
    stalled = check()
    assert stalled.level == "crit" and "stalled" in stalled.detail

    write_status(status, loop_at=NOW - 1, last_ok_at=NOW - 1, consecutive_failures=4)
    assert check().level == "warn"
    write_status(status, loop_at=NOW - 1, last_ok_at=NOW - 1, consecutive_failures=12)
    assert check().level == "crit"

    # the loop turns but the database has not answered for minutes
    write_status(status, loop_at=NOW - 1, last_ok_at=NOW - 400, consecutive_failures=2)
    assert check().level == "crit"

    write_status(
        status, loop_at=NOW - 1, last_ok_at=NOW - 1, consecutive_failures=0, paused_for_seconds=42
    )
    paused = check()
    assert paused.level == "warn" and "provider rate limit" in paused.detail


def test_worker_check_with_no_files_or_only_a_heartbeat(tmp_path: Path) -> None:
    status, beat = tmp_path / "status.json", tmp_path / "beat"
    assert ops.check_worker(status, now=NOW, heartbeat_file=beat).level == "crit"

    beat.touch()
    os.utime(beat, (NOW - 5, NOW - 5))
    assert ops.check_worker(status, now=NOW, heartbeat_file=beat).level == "ok"
    os.utime(beat, (NOW - 500, NOW - 500))
    assert ops.check_worker(status, now=NOW, heartbeat_file=beat).level == "crit"

    status.write_text("not json", encoding="utf-8")
    assert ops.check_worker(status, now=NOW).level == "crit"


def test_an_idle_worker_without_an_outbox_is_not_an_alarm(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    write_status(status, loop_at=NOW - 1, mode="idle")
    assert ops.check_worker(status, now=NOW).level == "ok"


# --- media ----------------------------------------------------------------------------------------


def test_media_check_ok_on_a_writable_store_and_critical_when_it_is_not(tmp_path: Path) -> None:
    assert levels(ops.check_media(MediaStore(tmp_path / "media"))) == {
        "media-write": "ok",
        "media-space": "ok",
    }
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    broken = ops.check_media(MediaStore(blocker / "media"))
    assert broken[0].level == "crit" and "503" in broken[0].detail


def test_low_disk_space_warns_then_goes_critical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    for free, expected in ((50, "ok"), (10, "warn"), (2, "crit")):
        monkeypatch.setattr(shutil, "disk_usage", lambda _p, f=free: usage(100, 100 - f, f))
        space = ops.check_media(MediaStore(tmp_path / "media"))[1]
        assert space.level == expected, free


def test_a_failed_photo_write_logs_a_stable_token_without_any_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "volume-that-is-a-file"
    blocker.write_text("x")
    store = MediaStore(blocker / "secret-media-dir")
    with caplog.at_level(logging.ERROR, logger="app.core.media"), pytest.raises(MediaStorageError):
        store.write("a" * 32, {128: b"jpeg"})
    assert "MEDIA_WRITE_FAILED" in caplog.text
    assert "secret-media-dir" not in caplog.text and str(tmp_path) not in caplog.text


# --- the CLI ------------------------------------------------------------


def test_ops_check_cli_exit_code_is_the_worst_level(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(SystemExit) as healthy:
        cli.main(["ops-check", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert healthy.value.code == 0 and report["status"] == "ok"
    assert {c["name"] for c in report["checks"]} >= {"database", "migrations", "outbox-backlog"}

    _, delivery_id, _ = make_delivery()
    execute(
        "UPDATE otp_deliveries SET next_attempt_at = now() - interval '10 minutes' WHERE id = :i",
        i=delivery_id,
    )
    with pytest.raises(SystemExit) as broken:
        cli.main(["ops-check"])
    out = capsys.readouterr().out
    assert broken.value.code == 2 and "CRIT  outbox-backlog" in out


def test_ops_check_output_never_contains_the_database_url_or_password(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # never probe the real media volume from a test
    monkeypatch.setattr(cli, "get_store", lambda: MediaStore(tmp_path / "media"))
    with pytest.raises(SystemExit):
        cli.main(["ops-check", "--json", "--media"])
    out = capsys.readouterr().out
    url = get_settings().database_url
    password = url.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
    assert password not in out and url not in out


def test_ops_check_can_skip_the_database_and_check_only_the_worker(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.worker as worker_module

    status = tmp_path / "status.json"
    monkeypatch.setattr(worker_module, "STATUS_FILE", status)
    monkeypatch.setattr(worker_module, "HEARTBEAT_FILE", tmp_path / "hb")
    write_status(status, loop_at=time.time() - 1, last_ok_at=time.time(), consecutive_failures=0)
    with pytest.raises(SystemExit) as ok:
        cli.main(["ops-check", "--no-db", "--worker", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert ok.value.code == 0 and [c["name"] for c in report["checks"]] == ["worker"]

    status.unlink()
    with pytest.raises(SystemExit) as missing:
        cli.main(["ops-check", "--no-db", "--worker"])
    assert missing.value.code == 2


def test_ops_check_cli_reports_a_dead_database_as_critical(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Dead:
        database_url = "postgresql+psycopg://nobody:Sup3rSecretPw@127.0.0.1:1/none"

    monkeypatch.setattr(cli, "get_settings", lambda: _Dead())
    started = time.monotonic()
    with pytest.raises(SystemExit) as exit_:
        cli.main(["ops-check"])
    out = capsys.readouterr().out
    assert exit_.value.code == 2 and "CRIT  database: unreachable" in out
    assert "Sup3rSecretPw" not in out
    assert time.monotonic() - started < 15  # it has a connect timeout; it does not hang


def test_session_type_is_used_read_only() -> None:
    # check_outbox ends its snapshot: no idle-in-transaction connection is left behind
    db = Session(create_engine(get_settings().database_url))
    ops.check_outbox(db)
    assert not db.in_transaction()
    db.close()
