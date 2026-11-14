"""M15 acceptance: the demo reset system — ARCHITECTURE.md §19.

HIGH RISK, per IMPLEMENTATION_PLAN.md: reset is the only operation that
destroys and rebuilds the dataset while clients may be connected. Every
test here runs against the ISOLATED database `reset_support.py`
manages (never the real configured database — see its module
docstring), using real PostgreSQL concurrency and, where the test
matrix explicitly requires it, real separate OS processes.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import httpx
import psycopg
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.locks import FACEMARK_RESET_FENCE
from app.core.security import generate_session_token, hash_session_token
from app.db.session import mutating_transaction
from app.modules.admin import reset as reset_module
from app.modules.admin.reset import (
    ResetFenceTimeoutError,
    ResetGuardError,
    ResetReport,
    ResetVerificationError,
    run_demo_reset,
)
from app.seed import demo_fixtures as fx
from tests.m04_support import ADVISORY_LOCKS_HERE
from tests.reset_support import (
    BACKEND_ROOT,
    SEED_ADMIN_PASSWORD,
    SEED_STAFF_PASSWORD,
    subprocess_env,
)

pytestmark = pytest.mark.usefixtures("clean_reset_db")

_BACKEND_ROOT = str(BACKEND_ROOT)


def _psycopg_url(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _raw_conn(sqlalchemy_url: str) -> psycopg.Connection[Any]:
    conn = psycopg.connect(_psycopg_url(sqlalchemy_url))
    conn.autocommit = False
    return conn


def _run_reset(reset_db_url: str, **overrides: object) -> ResetReport:
    kwargs: dict[str, Any] = {
        "admin_password": SEED_ADMIN_PASSWORD,
        "staff_password": SEED_STAFF_PASSWORD,
        "database_url": reset_db_url,
    }
    kwargs.update(overrides)
    return run_demo_reset(**kwargs)


def _scalar(engine: Engine, sql: str, **params: object) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def _scalar_or_none(engine: Engine, sql: str, **params: object) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one_or_none()


def _rows(engine: Engine, sql: str, **params: object) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params).all())


def _exec(engine: Engine, sql: str, **params: object) -> None:
    with engine.connect() as conn:
        conn.execute(text(sql), params)
        conn.commit()


def _demo_settings(reset_db_url: str, *, demo_mode: bool, environment: str) -> Settings:
    return Settings(
        environment=environment,
        database_url=reset_db_url,
        app_origin="http://localhost:3000",
        scan_ref_secret="test-scan-ref-secret",
        demo_mode=demo_mode,
    )


# ---------------------------------------------------------------------------
# 1-4: CLI guards / no HTTP endpoint
# ---------------------------------------------------------------------------


class TestGuards:
    def test_cli_refuses_without_confirm(self, reset_db_url: str) -> None:
        from app import cli

        with pytest.raises(SystemExit) as exc_info:
            cli.main(["reset-demo"])
        assert exc_info.value.code == 1
        # No write of any kind — maintenance_mode is unchanged.
        from sqlalchemy import create_engine

        engine = create_engine(reset_db_url, future=True)
        try:
            assert (
                _scalar(engine, "SELECT maintenance_mode FROM event_config WHERE id = 1") is False
            )
        finally:
            engine.dispose()

    def test_refuses_demo_mode_false(self, reset_db_url: str) -> None:
        settings = _demo_settings(reset_db_url, demo_mode=False, environment="development")
        with pytest.raises(ResetGuardError, match="DEMO_MODE"):
            run_demo_reset(
                admin_password=SEED_ADMIN_PASSWORD,
                staff_password=SEED_STAFF_PASSWORD,
                settings=settings,
            )

    def test_refuses_production(self) -> None:
        # demo_mode=True + production is impossible via a legally
        # constructed Settings (the settings-level validator itself
        # forbids it) — this exercises _guard_demo_and_environment's
        # production branch directly, duck-typed, independent of that
        # settings-level validator closing the same door one layer up.
        fake_settings = cast(Settings, SimpleNamespace(demo_mode=True, is_production=True))
        with pytest.raises(ResetGuardError, match="production"):
            reset_module._guard_demo_and_environment(fake_settings)

    def test_no_http_reset_endpoint_exists(self) -> None:
        from app.main import app

        schema = app.openapi()
        for path in schema["paths"]:
            segments = path.lower().strip("/").split("/")
            assert "reset" not in segments, f"unexpected reset-related HTTP path: {path}"


# ---------------------------------------------------------------------------
# 5-12: maintenance & fence lifecycle
# ---------------------------------------------------------------------------


class TestFenceLifecycle:
    def test_maintenance_committed_before_fence_acquired(self, reset_db_url: str) -> None:
        from sqlalchemy import create_engine

        engine = create_engine(reset_db_url, future=True)
        conn = engine.connect()
        try:
            reset_module._enter_maintenance(conn)
            # A SEPARATE connection must already see it — proves it was
            # actually committed, not just pending in this session.
            assert _scalar(engine, "SELECT maintenance_mode FROM event_config WHERE id = 1") is True
        finally:
            _exec(engine, "UPDATE event_config SET maintenance_mode = false WHERE id = 1")
            conn.close()
            engine.dispose()

    def test_dedicated_connection_is_never_the_app_pool(self, reset_db_url: str) -> None:
        """`run_demo_reset` must own an entirely separate engine/connection
        — never `app.db.session.engine`/`SessionLocal`, which stay bound
        to whatever DATABASE_URL this test PROCESS was started with, not
        the isolated reset database."""
        from app.db.session import engine as app_engine

        assert str(app_engine.url) != reset_db_url

    def test_same_pg_session_owns_the_lock_throughout(self, reset_db_url: str) -> None:
        """Manually walks the exact §19.5 sequence via the module's own
        primitives, checking from a SEPARATE raw connection at multiple
        points that the exclusive advisory lock is held by one single,
        unchanging backend pid — proving "the same connection owns the
        lock through: maintenance commit, lock acquisition, restore
        transaction, post-commit coordination/verification,
        maintenance-off commit, unlock" end to end."""
        from sqlalchemy import create_engine

        engine = create_engine(reset_db_url, future=True)
        conn = engine.connect()
        checker = _raw_conn(reset_db_url)

        def _lock_holder_pid() -> int | None:
            # Matched on locktype/mode/granted, like m04_support.py's
            # `assert_fenced_before_any_read` — reconstructing the
            # bigint key from classid/objid is an internal pg_locks
            # representation detail not worth depending on precisely.
            # Only ONE exclusive advisory lock (the fence) is ever held
            # at a time in this test, so this is unambiguous here.
            with checker.cursor() as cur:
                cur.execute(
                    f"SELECT pid FROM pg_locks WHERE {ADVISORY_LOCKS_HERE} "
                    "AND mode = 'ExclusiveLock' AND granted"
                )
                row = cur.fetchone()
                return int(row[0]) if row else None

        try:
            reset_module._enter_maintenance(conn)
            backend_pid: Any = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()

            reset_module._acquire_fence(conn)
            pid_after_acquire = _lock_holder_pid()
            assert pid_after_acquire == backend_pid, "fence not held by the acquiring session"

            old_generation = reset_module._read_generation(conn)
            reset_module._truncate_reset_sensitive_tables(conn)
            reset_module._reconcile_reference_data(
                conn, admin_password=SEED_ADMIN_PASSWORD, staff_password=SEED_STAFF_PASSWORD
            )
            new_generation = old_generation + 1
            reset_module._restore_event_config(conn, new_generation=new_generation)
            reset_module._insert_demo_attendees(conn)
            conn.commit()

            pid_after_restore = _lock_holder_pid()
            assert pid_after_restore == backend_pid, "fence moved session during the restore"

            reset_module._notify_reset(conn, new_generation=new_generation)
            reset_module._verify_restore(conn, expected_generation=new_generation)

            pid_after_verify = _lock_holder_pid()
            assert pid_after_verify == backend_pid, "fence moved session during verification"

            reset_module._exit_maintenance(conn)
            pid_after_maintenance_off = _lock_holder_pid()
            assert pid_after_maintenance_off == backend_pid, "fence moved during maintenance-off"

            reset_module._release_fence(conn)
            assert _lock_holder_pid() is None, "fence still held after explicit release"
        finally:
            checker.close()
            conn.close()
            engine.dispose()

    def test_shared_mutation_fence_delays_reset(self, reset_db_url: str) -> None:
        """A real in-flight mutation (holding the SHARED side of the
        fence on its own connection) must block the exclusive acquire
        until it commits."""
        holder = _raw_conn(reset_db_url)
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock_shared(%s)", (FACEMARK_RESET_FENCE,))
        # holder now holds the shared lock, in an open transaction.

        reset_thread_done = threading.Event()
        report_holder: dict[str, ResetReport] = {}

        def do_reset() -> None:
            report_holder["r"] = _run_reset(reset_db_url)
            reset_thread_done.set()

        t = threading.Thread(target=do_reset)
        t.start()
        try:
            assert not reset_thread_done.wait(timeout=1.0), (
                "reset proceeded despite a live shared-fence holder"
            )
        finally:
            holder.commit()  # releases the shared lock — reset should now proceed
            holder.close()
        t.join(timeout=10)
        assert reset_thread_done.is_set()
        assert report_holder["r"].new_generation == 2

    def test_mutation_rollback_also_drains_reset(self, reset_db_url: str) -> None:
        holder = _raw_conn(reset_db_url)
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock_shared(%s)", (FACEMARK_RESET_FENCE,))

        done = threading.Event()

        def do_reset() -> None:
            _run_reset(reset_db_url)
            done.set()

        t = threading.Thread(target=do_reset)
        t.start()
        try:
            assert not done.wait(timeout=1.0)
        finally:
            holder.rollback()  # rollback also releases the transaction-scoped shared lock
            holder.close()
        t.join(timeout=10)
        assert done.is_set()

    def test_fence_acquire_timeout_leaves_dataset_untouched(
        self, reset_db_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(reset_module, "_FENCE_ACQUIRE_TIMEOUT_MS", 300)
        holder = _raw_conn(reset_db_url)
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (FACEMARK_RESET_FENCE,))
        try:
            with pytest.raises(ResetFenceTimeoutError):
                _run_reset(reset_db_url)
        finally:
            with holder.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (FACEMARK_RESET_FENCE,))
            holder.commit()
            holder.close()

        from sqlalchemy import create_engine

        engine = create_engine(reset_db_url, future=True)
        try:
            # Dataset untouched: still generation 1, maintenance left ON
            # (it was committed before the fence acquire was attempted).
            assert _scalar(engine, "SELECT dataset_generation FROM event_config WHERE id = 1") == 1
            assert _scalar(engine, "SELECT maintenance_mode FROM event_config WHERE id = 1") is True
        finally:
            _exec(engine, "UPDATE event_config SET maintenance_mode = false WHERE id = 1")
            engine.dispose()


# ---------------------------------------------------------------------------
# 13-19: atomic restore / failure injection
# ---------------------------------------------------------------------------


class TestAtomicRestore:
    def test_generation_increments_exactly_once_on_success(
        self, reset_db_engine: Engine, reset_db_url: str
    ) -> None:
        assert (
            _scalar(reset_db_engine, "SELECT dataset_generation FROM event_config WHERE id = 1")
            == 1
        )
        report = _run_reset(reset_db_url)
        assert report.old_generation == 1
        assert report.new_generation == 2
        assert (
            _scalar(reset_db_engine, "SELECT dataset_generation FROM event_config WHERE id = 1")
            == 2
        )

    def test_maintenance_off_only_on_full_success(
        self, reset_db_engine: Engine, reset_db_url: str
    ) -> None:
        _run_reset(reset_db_url)
        assert (
            _scalar(reset_db_engine, "SELECT maintenance_mode FROM event_config WHERE id = 1")
            is False
        )

    @pytest.mark.parametrize(
        "failing_function",
        [
            "_truncate_reset_sensitive_tables",
            "_reconcile_reference_data",
            "_restore_event_config",
            "_insert_demo_attendees",
        ],
    )
    def test_restore_transaction_failure_rolls_back_completely(
        self,
        reset_db_engine: Engine,
        reset_db_url: str,
        monkeypatch: pytest.MonkeyPatch,
        failing_function: str,
    ) -> None:
        """Failure injected at each stage of the single atomic restore
        transaction (§19.5 step 4): dataset, sessions and generation are
        ALL unchanged, and maintenance stays ON — items 13-16, 18 of the
        test matrix, parametrised across the whole transaction body
        rather than four near-identical test functions."""

        class Boom(RuntimeError):
            pass

        original = getattr(reset_module, failing_function)

        def _boom(*args: object, **kwargs: object) -> object:
            raise Boom("injected failure")

        monkeypatch.setattr(reset_module, failing_function, _boom)

        with pytest.raises(Boom):
            _run_reset(reset_db_url)

        assert (
            _scalar(reset_db_engine, "SELECT dataset_generation FROM event_config WHERE id = 1")
            == 1
        )
        assert (
            _scalar(reset_db_engine, "SELECT maintenance_mode FROM event_config WHERE id = 1")
            is True
        )
        assert _scalar(reset_db_engine, "SELECT count(*) FROM users") == len(fx.ATTENDEES)

        # Clean up maintenance for any later test sharing this DB session
        # scope (clean_reset_db resets event_config fresh per-test anyway,
        # but leaving this ON would confuse a human reading a failed run).
        monkeypatch.setattr(reset_module, failing_function, original)
        _exec(reset_db_engine, "UPDATE event_config SET maintenance_mode = false WHERE id = 1")

    def test_post_commit_notify_failure_leaves_generation_advanced_and_maintenance_on(
        self, reset_db_engine: Engine, reset_db_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The restore itself already committed by the time `_notify_reset`
        runs — a failure here must NOT be (and structurally cannot be)
        rolled back. Generation is already G+1; maintenance stays ON."""

        class Boom(RuntimeError):
            pass

        def _boom(*args: object, **kwargs: object) -> None:
            raise Boom("simulated notify failure")

        monkeypatch.setattr(reset_module, "_notify_reset", _boom)

        with pytest.raises(Boom):
            _run_reset(reset_db_url)

        assert (
            _scalar(reset_db_engine, "SELECT dataset_generation FROM event_config WHERE id = 1")
            == 2
        )
        assert (
            _scalar(reset_db_engine, "SELECT maintenance_mode FROM event_config WHERE id = 1")
            is True
        )

    def test_verification_failure_leaves_maintenance_on(
        self, reset_db_engine: Engine, reset_db_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            reset_module,
            "_verify_restore",
            lambda *a, **k: (_ for _ in ()).throw(ResetVerificationError("forced")),
        )
        with pytest.raises(ResetVerificationError):
            _run_reset(reset_db_url)
        assert (
            _scalar(reset_db_engine, "SELECT dataset_generation FROM event_config WHERE id = 1")
            == 2
        )
        assert (
            _scalar(reset_db_engine, "SELECT maintenance_mode FROM event_config WHERE id = 1")
            is True
        )

    def test_fence_released_even_on_failure(
        self, reset_db_engine: Engine, reset_db_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Boom(RuntimeError):
            pass

        def _boom(*args: object, **kwargs: object) -> None:
            raise Boom

        monkeypatch.setattr(reset_module, "_verify_restore", _boom)
        with pytest.raises(Boom):
            _run_reset(reset_db_url)

        checker = _raw_conn(reset_db_url)
        try:
            with checker.cursor() as cur:
                cur.execute(
                    f"SELECT count(*) FROM pg_locks WHERE {ADVISORY_LOCKS_HERE} "
                    "AND mode = 'ExclusiveLock' AND granted"
                )
                (count,) = cur.fetchone()  # type: ignore[misc]
            assert count == 0, "the fence was not released after a failure"
        finally:
            checker.close()


# ---------------------------------------------------------------------------
# 20-23: reference reconciliation / QR stability
# ---------------------------------------------------------------------------


class TestReconciliation:
    def test_reconciliation_removes_a_rehearsal_extra_booth(
        self, reset_db_engine: Engine, reset_db_url: str
    ) -> None:
        _exec(
            reset_db_engine,
            "INSERT INTO booths (slug, name) VALUES ('rehearsal-extra', 'Rehearsal Extra Booth')",
        )
        assert (
            _scalar_or_none(reset_db_engine, "SELECT id FROM booths WHERE slug = 'rehearsal-extra'")
            is not None
        )

        _run_reset(reset_db_url)

        assert (
            _scalar_or_none(reset_db_engine, "SELECT id FROM booths WHERE slug = 'rehearsal-extra'")
            is None
        )
        assert _scalar(reset_db_engine, "SELECT count(*) FROM booths") == len(fx.BOOTHS)

    def test_reconciliation_removes_a_rehearsal_extra_interest(
        self, reset_db_engine: Engine, reset_db_url: str
    ) -> None:
        _exec(
            reset_db_engine,
            "INSERT INTO interests (slug, name) VALUES ('rehearsal-extra', 'Rehearsal Extra')",
        )
        _run_reset(reset_db_url)
        assert (
            _scalar_or_none(
                reset_db_engine, "SELECT id FROM interests WHERE slug = 'rehearsal-extra'"
            )
            is None
        )
        assert _scalar(reset_db_engine, "SELECT count(*) FROM interests") == len(fx.INTERESTS)

    def test_reconciliation_restores_an_in_place_edit_to_a_kept_booth(
        self, reset_db_engine: Engine, reset_db_url: str
    ) -> None:
        fixture_booth = fx.BOOTHS[0]
        _exec(
            reset_db_engine,
            "UPDATE booths SET description = 'REHEARSAL EDIT', is_active = false "
            "WHERE slug = :slug",
            slug=fixture_booth.slug,
        )
        _run_reset(reset_db_url)
        row = _rows(
            reset_db_engine,
            "SELECT description, is_active FROM booths WHERE slug = :slug",
            slug=fixture_booth.slug,
        )[0]
        assert row.description == fixture_booth.description
        assert row.is_active is True

    def test_reconciliation_restores_booth_interest_weights_and_tasks(
        self, reset_db_engine: Engine, reset_db_url: str
    ) -> None:
        _exec(reset_db_engine, "DELETE FROM booth_interests")
        _exec(reset_db_engine, "UPDATE tasks SET title = 'REHEARSAL TASK EDIT'")
        _run_reset(reset_db_url)
        expected_bi = sum(len(b.interests) for b in fx.BOOTHS)
        expected_tasks = sum(len(b.tasks) for b in fx.BOOTHS)
        assert _scalar(reset_db_engine, "SELECT count(*) FROM booth_interests") == expected_bi
        assert _scalar(reset_db_engine, "SELECT count(*) FROM tasks") == expected_tasks
        assert (
            _scalar(reset_db_engine, "SELECT count(*) FROM tasks WHERE title LIKE 'REHEARSAL%'")
            == 0
        )

    def test_reconciliation_restores_a_locked_out_rehearsal_staff_account(
        self, reset_db_engine: Engine, reset_db_url: str
    ) -> None:
        fixture_staff = fx.STAFF[0]
        _exec(
            reset_db_engine,
            "UPDATE staff_accounts SET is_active = false, failed_login_count = 99, "
            "locked_until = now() + interval '1 hour' WHERE email = :email",
            email=fixture_staff.email,
        )
        _run_reset(reset_db_url)
        row = _rows(
            reset_db_engine,
            "SELECT is_active, failed_login_count, locked_until FROM staff_accounts "
            "WHERE email = :email",
            email=fixture_staff.email,
        )[0]
        assert row.is_active is True
        assert row.failed_login_count == 0
        assert row.locked_until is None

    def test_fk_dependent_rehearsal_staff_reconciled_without_violation(
        self, reset_db_engine: Engine, reset_db_url: str
    ) -> None:
        """Codex pre-review Blocker 2's specific ask: a rehearsal-created
        staff account referenced by BOTH `admin_audit.staff_account_id`
        (NOT NULL, no CASCADE) and `event_config.updated_by_staff_id`
        (nullable) must be reconcilable without an FK violation, and
        without a blind `TRUNCATE admin_audit`."""
        with reset_db_engine.connect() as conn:
            booth_id: Any = conn.execute(
                text("SELECT id FROM booths WHERE slug = :slug"), {"slug": fx.BOOTHS[0].slug}
            ).scalar_one()
            extra_staff_id: Any = conn.execute(
                text(
                    "INSERT INTO staff_accounts (email, password_hash, full_name, role, booth_id) "
                    "VALUES ('rehearsal-staff@test.facemark.demo', 'x', 'Rehearsal Staff', "
                    "'BOOTH_STAFF', :booth_id) RETURNING id"
                ),
                {"booth_id": booth_id},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO admin_audit (staff_account_id, action, target_type, target_id) "
                    "VALUES (:sid, 'booth.update', 'booth', 'rehearsal')"
                ),
                {"sid": extra_staff_id},
            )
            conn.execute(
                text("UPDATE event_config SET updated_by_staff_id = :sid WHERE id = 1"),
                {"sid": extra_staff_id},
            )
            conn.commit()

        _run_reset(reset_db_url)  # must not raise an IntegrityError

        assert (
            _scalar_or_none(
                reset_db_engine,
                "SELECT id FROM staff_accounts WHERE email = 'rehearsal-staff@test.facemark.demo'",
            )
            is None
        )
        assert (
            _scalar_or_none(
                reset_db_engine,
                "SELECT id FROM admin_audit WHERE staff_account_id = :sid",
                sid=extra_staff_id,
            )
            is None
        )
        assert (
            _scalar_or_none(
                reset_db_engine, "SELECT updated_by_staff_id FROM event_config WHERE id = 1"
            )
            is None
        )

    def test_exact_fixture_state_restored(self, reset_db_engine: Engine, reset_db_url: str) -> None:
        _run_reset(reset_db_url)
        assert _scalar(reset_db_engine, "SELECT count(*) FROM booths") == len(fx.BOOTHS)
        assert _scalar(reset_db_engine, "SELECT count(*) FROM interests") == len(fx.INTERESTS)
        assert _scalar(reset_db_engine, "SELECT count(*) FROM staff_accounts") == len(fx.STAFF)
        assert _scalar(reset_db_engine, "SELECT count(*) FROM users") == len(fx.ATTENDEES)
        for b in fx.BOOTHS:
            row = _rows(reset_db_engine, "SELECT name FROM booths WHERE slug = :s", s=b.slug)[0]
            assert row.name == b.name

    def test_seeded_qr_token_stable_across_reset(
        self, reset_db_engine: Engine, reset_db_url: str
    ) -> None:
        attendee = fx.ATTENDEES[0]
        before = _scalar(
            reset_db_engine,
            "SELECT public_token FROM contact_cards c JOIN users u ON u.id = c.user_id "
            "WHERE u.email = :email",
            email=attendee.email,
        )
        assert before == attendee.public_token

        _run_reset(reset_db_url)

        row = _rows(
            reset_db_engine,
            "SELECT c.public_token, u.first_name, u.status FROM contact_cards c "
            "JOIN users u ON u.id = c.user_id WHERE c.public_token = :token",
            token=attendee.public_token,
        )
        assert len(row) == 1, "the fixed token must resolve to exactly one identity after reset"
        assert row[0].public_token == attendee.public_token
        assert row[0].first_name == attendee.first_name
        assert row[0].status == "ACTIVE"


# ---------------------------------------------------------------------------
# 24-33: sessions / OTP / provisional invalidation
# ---------------------------------------------------------------------------


def _mint_attendee_session(engine: Engine, *, user_email: str) -> str:
    session = Session(bind=engine)
    try:
        user_id: Any = session.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": user_email}
        ).scalar_one()
        raw_token = generate_session_token()
        session.execute(
            text(
                "INSERT INTO sessions (token_hash, principal_type, user_id, issued_at, expires_at) "
                "VALUES (:h, 'ATTENDEE', :uid, now(), now() + interval '12 hours')"
            ),
            {"h": hash_session_token(raw_token), "uid": user_id},
        )
        session.commit()
        return raw_token
    finally:
        session.close()


def _mint_staff_session(engine: Engine, *, staff_email: str) -> str:
    session = Session(bind=engine)
    try:
        staff_id: Any = session.execute(
            text("SELECT id FROM staff_accounts WHERE email = :e"), {"e": staff_email}
        ).scalar_one()
        raw_token = generate_session_token()
        session.execute(
            text(
                "INSERT INTO sessions "
                "(token_hash, principal_type, staff_account_id, issued_at, expires_at) "
                "VALUES (:h, 'STAFF', :sid, now(), now() + interval '12 hours')"
            ),
            {"h": hash_session_token(raw_token), "sid": staff_id},
        )
        session.commit()
        return raw_token
    finally:
        session.close()


class TestSessionAndOtpInvalidation:
    def test_runtime_attendee_removed(self, reset_db_engine: Engine, reset_db_url: str) -> None:
        _exec(
            reset_db_engine,
            "INSERT INTO users (first_name, last_name, email, phone, profession, "
            "sponsor_consent, status, activated_at) VALUES "
            "('Runtime', 'Attendee', 'runtime@test.facemark.demo', '+1', 'Eng', false, "
            "'ACTIVE', now())",
        )
        _run_reset(reset_db_url)
        assert (
            _scalar_or_none(
                reset_db_engine, "SELECT id FROM users WHERE email = 'runtime@test.facemark.demo'"
            )
            is None
        )
        assert _scalar(reset_db_engine, "SELECT count(*) FROM users") == len(fx.ATTENDEES)

    def test_provisional_user_removed(self, reset_db_engine: Engine, reset_db_url: str) -> None:
        _exec(
            reset_db_engine,
            "INSERT INTO users (first_name, last_name, email, phone, profession, "
            "sponsor_consent, status) VALUES "
            "('Prov', 'Isional', 'provisional@test.facemark.demo', '+1', 'Eng', false, "
            "'PROVISIONAL')",
        )
        _run_reset(reset_db_url)
        assert (
            _scalar_or_none(
                reset_db_engine,
                "SELECT id FROM users WHERE email = 'provisional@test.facemark.demo'",
            )
            is None
        )

    def test_attendee_session_invalidated(self, reset_db_engine: Engine, reset_db_url: str) -> None:
        token = _mint_attendee_session(reset_db_engine, user_email=fx.ATTENDEES[0].email)
        assert (
            _scalar_or_none(
                reset_db_engine,
                "SELECT id FROM sessions WHERE token_hash = :h",
                h=hash_session_token(token),
            )
            is not None
        )

        _run_reset(reset_db_url)

        assert _scalar(reset_db_engine, "SELECT count(*) FROM sessions") == 0
        from app.core.errors import UnauthenticatedError
        from app.core.sessions import resolve_attendee_session

        session = Session(bind=reset_db_engine)
        try:
            with pytest.raises(UnauthenticatedError):
                resolve_attendee_session(session, token)
        finally:
            session.close()

    def test_staff_session_invalidated(self, reset_db_engine: Engine, reset_db_url: str) -> None:
        token = _mint_staff_session(reset_db_engine, staff_email=fx.STAFF[0].email)
        _run_reset(reset_db_url)

        from app.core.errors import UnauthenticatedError
        from app.core.sessions import resolve_staff_session

        session = Session(bind=reset_db_engine)
        try:
            with pytest.raises(UnauthenticatedError):
                resolve_staff_session(session, token)
        finally:
            session.close()

    def test_stale_attendee_cookie_rejected_even_with_reused_numeric_id(
        self, reset_db_engine: Engine, reset_db_url: str
    ) -> None:
        """§19.3/§19.6: `users` id sequences RESTART — after reset, a
        NEW row can legitimately reuse id=1. The OLD session, minted
        against the pre-reset id=1, must still fail — proving rejection
        is by TOKEN validity, never inferred from a numeric id."""
        token = _mint_attendee_session(reset_db_engine, user_email=fx.ATTENDEES[0].email)
        pre_reset_id = _scalar(
            reset_db_engine, "SELECT id FROM users WHERE email = :e", e=fx.ATTENDEES[0].email
        )

        _run_reset(reset_db_url)

        post_reset_id = _scalar(
            reset_db_engine, "SELECT id FROM users WHERE email = :e", e=fx.ATTENDEES[0].email
        )
        assert post_reset_id == pre_reset_id, "identity restarted from 1 as expected"

        from app.core.errors import UnauthenticatedError
        from app.core.sessions import resolve_attendee_session

        session = Session(bind=reset_db_engine)
        try:
            with pytest.raises(UnauthenticatedError):
                resolve_attendee_session(session, token)
        finally:
            session.close()

    def test_otp_challenges_removed(self, reset_db_engine: Engine, reset_db_url: str) -> None:
        from app.core.security import hash_otp_code

        _exec(
            reset_db_engine,
            "INSERT INTO otp_challenges (email, purpose, code_hash, expires_at) VALUES "
            "('otp-test@test.facemark.demo', 'ACTIVATION', :h, now() + interval '10 minutes')",
            h=hash_otp_code("123456"),
        )
        _run_reset(reset_db_url)
        assert _scalar(reset_db_engine, "SELECT count(*) FROM otp_challenges") == 0

    def test_old_otp_cannot_activate_a_post_reset_identity(
        self, reset_db_engine: Engine, reset_db_url: str
    ) -> None:
        from app.core.security import hash_otp_code
        from app.modules.attendees.service import verify_otp

        email = "otp-reuse@test.facemark.demo"
        with reset_db_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (first_name, last_name, email, phone, profession, "
                    "sponsor_consent, status) VALUES "
                    "('Otp', 'Reuse', :email, '+1', 'Eng', false, 'PROVISIONAL')"
                ),
                {"email": email},
            )
            conn.execute(
                text(
                    "INSERT INTO otp_challenges (email, purpose, code_hash, expires_at) VALUES "
                    "(:email, 'ACTIVATION', :h, now() + interval '10 minutes')"
                ),
                {"email": email, "h": hash_otp_code("654321")},
            )
            conn.commit()

        _run_reset(reset_db_url)  # removes the provisional user AND the otp_challenges row

        session = Session(bind=reset_db_engine)
        try:
            with mutating_transaction(session) as tx:
                outcome = verify_otp(tx, email=email, code="654321")
            assert not outcome.ok, "an OTP challenge from before reset must never verify afterward"
        finally:
            session.close()


# ---------------------------------------------------------------------------
# 32-37: idempotency / points / feedback semantics
# ---------------------------------------------------------------------------


def _fixture_ids(engine: Engine) -> tuple[int, int, int]:
    """(user_id, booth_id, booth_staff_account_id) for the first fixture
    attendee, the first fixture booth, and that booth's staff account."""
    booth_staff = next(s for s in fx.STAFF if s.booth_slug is not None)
    with engine.connect() as conn:
        user_id: Any = conn.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": fx.ATTENDEES[0].email}
        ).scalar_one()
        booth_id: Any = conn.execute(
            text("SELECT id FROM booths WHERE slug = :s"), {"s": booth_staff.booth_slug}
        ).scalar_one()
        staff_id: Any = conn.execute(
            text("SELECT id FROM staff_accounts WHERE email = :e"), {"e": booth_staff.email}
        ).scalar_one()
    return int(user_id), int(booth_id), int(staff_id)


class TestIdempotencyPointsFeedback:
    def test_idempotency_records_removed_with_points_log(
        self, reset_db_engine: Engine, reset_db_url: str
    ) -> None:
        user_id, booth_id, staff_id = _fixture_ids(reset_db_engine)
        with reset_db_engine.connect() as conn:
            points_log_id: Any = conn.execute(
                text(
                    "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                    "entered_by_staff_id, entered_by_system, dedupe_key) VALUES "
                    "(:u, :b, 'TASK', 'AWARD', 10, :s, false, :dk) RETURNING id"
                ),
                {"u": user_id, "b": booth_id, "s": staff_id, "dk": f"m15-{uuid.uuid4().hex}"},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO idempotency_records "
                    "(staff_account_id, idempotency_key, request_fingerprint, points_log_id, "
                    "completed_at) VALUES (:s, :k, 'fp', :pl, now())"
                ),
                {"s": staff_id, "k": str(uuid.uuid4()), "pl": points_log_id},
            )
            conn.commit()

        _run_reset(reset_db_url)

        assert _scalar(reset_db_engine, "SELECT count(*) FROM points_log") == 0
        assert _scalar(reset_db_engine, "SELECT count(*) FROM idempotency_records") == 0

    def test_points_and_reversals_removed(self, reset_db_engine: Engine, reset_db_url: str) -> None:
        user_id, booth_id, staff_id = _fixture_ids(reset_db_engine)
        with reset_db_engine.connect() as conn:
            award_id: Any = conn.execute(
                text(
                    "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                    "entered_by_staff_id, entered_by_system) VALUES "
                    "(:u, :b, 'TASK', 'AWARD', 10, :s, false) RETURNING id"
                ),
                {"u": user_id, "b": booth_id, "s": staff_id},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                    "entered_by_staff_id, entered_by_system, reverses_points_log_id) VALUES "
                    "(:u, :b, 'TASK', 'REVERSAL', -10, :s, false, :rev)"
                ),
                {"u": user_id, "b": booth_id, "s": staff_id, "rev": award_id},
            )
            conn.commit()

        _run_reset(reset_db_url)
        assert _scalar(reset_db_engine, "SELECT count(*) FROM points_log") == 0

    def test_feedback_rows_removed(self, reset_db_engine: Engine, reset_db_url: str) -> None:
        user_id, booth_id, _ = _fixture_ids(reset_db_engine)
        _exec(
            reset_db_engine,
            "INSERT INTO feedback (user_id, booth_id, rating) VALUES (:u, :b, 5)",
            u=user_id,
            b=booth_id,
        )
        _run_reset(reset_db_url)
        assert _scalar(reset_db_engine, "SELECT count(*) FROM feedback") == 0

    def test_feedback_eligibility_restored_by_full_reset(
        self, reset_db_engine: Engine, reset_db_url: str
    ) -> None:
        user_id, booth_id, _ = _fixture_ids(reset_db_engine)
        with reset_db_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                    "entered_by_system) VALUES (:u, :b, 'FEEDBACK', 'AWARD', 5, true)"
                ),
                {"u": user_id, "b": booth_id},
            )
            conn.commit()
            # A second FEEDBACK AWARD for the same (user, booth) is
            # structurally blocked while the ledger row survives —
            # proving the "before" half of this test actually means
            # something.
            with pytest.raises(Exception):  # noqa: B017, PT011 — IntegrityError from the DB driver
                conn.execute(
                    text(
                        "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                        "entered_by_system) VALUES (:u, :b, 'FEEDBACK', 'AWARD', 5, true)"
                    ),
                    {"u": user_id, "b": booth_id},
                )
                conn.commit()
            conn.rollback()

        _run_reset(reset_db_url)

        # The old ledger row is gone entirely — the SAME (user, booth)
        # pair (fixture ids are stable across reset) can be rewarded.
        with reset_db_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                    "entered_by_system) VALUES (:u, :b, 'FEEDBACK', 'AWARD', 5, true)"
                ),
                {"u": user_id, "b": booth_id},
            )
            conn.commit()  # must not raise
        assert (
            _scalar(
                reset_db_engine,
                "SELECT count(*) FROM points_log WHERE user_id = :u AND booth_id = :b",
                u=user_id,
                b=booth_id,
            )
            == 1
        )

    def test_reversal_alone_does_not_restore_feedback_eligibility(
        self, reset_db_engine: Engine
    ) -> None:
        """Independent of reset entirely — confirms M15 has not altered
        the existing M11/M12 semantic: a REVERSAL never frees the
        (user, booth) feedback slot, because the original AWARD row is
        never deleted or mutated (uq_points_feedback_user_booth is
        scoped to entry_type = 'AWARD')."""
        user_id, booth_id, _ = _fixture_ids(reset_db_engine)
        with reset_db_engine.connect() as conn:
            award_id: Any = conn.execute(
                text(
                    "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                    "entered_by_system) VALUES (:u, :b, 'FEEDBACK', 'AWARD', 5, true) RETURNING id"
                ),
                {"u": user_id, "b": booth_id},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                    "entered_by_system, reverses_points_log_id) VALUES "
                    "(:u, :b, 'FEEDBACK', 'REVERSAL', -5, true, :rev)"
                ),
                {"u": user_id, "b": booth_id, "rev": award_id},
            )
            conn.commit()

            with pytest.raises(Exception):  # noqa: B017, PT011
                conn.execute(
                    text(
                        "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                        "entered_by_system) VALUES (:u, :b, 'FEEDBACK', 'AWARD', 5, true)"
                    ),
                    {"u": user_id, "b": booth_id},
                )
                conn.commit()
            conn.rollback()


# ---------------------------------------------------------------------------
# 52: read-only requests see a coherent committed dataset, never partial
# ---------------------------------------------------------------------------


class TestReadOnlyConsistency:
    def test_concurrent_reader_never_sees_a_partially_restored_dataset(
        self, reset_db_engine: Engine, reset_db_url: str
    ) -> None:
        """Polls `(dataset_generation, users count, booths count)` from a
        SEPARATE connection throughout a real reset run and asserts every
        single observed snapshot is INTERNALLY CONSISTENT — either
        exactly the pre-reset triple or exactly the post-reset triple,
        never a mix (e.g. new generation with old attendee count) —
        which is exactly what one atomic restore transaction (§19.5 step
        4) guarantees and a multi-statement, non-transactional restore
        would not."""
        pre = (1, len(fx.ATTENDEES), len(fx.BOOTHS))
        post = (2, len(fx.ATTENDEES), len(fx.BOOTHS))
        observed: list[tuple[int, int, int]] = []
        stop = threading.Event()

        def poll() -> None:
            reader = _raw_conn(reset_db_url)
            reader.autocommit = True
            try:
                while not stop.is_set():
                    with reader.cursor() as cur:
                        cur.execute(
                            "SELECT dataset_generation, "
                            "(SELECT count(*) FROM users), (SELECT count(*) FROM booths) "
                            "FROM event_config WHERE id = 1"
                        )
                        row = cur.fetchone()
                        if row:
                            observed.append((int(row[0]), int(row[1]), int(row[2])))
                    time.sleep(0.002)
            finally:
                reader.close()

        poller = threading.Thread(target=poll)
        poller.start()
        try:
            _run_reset(reset_db_url)
        finally:
            stop.set()
            poller.join(timeout=10)

        bad = [o for o in observed if o != pre and o != post]
        assert not bad, f"observed an incoherent partially-restored snapshot: {bad}"
        assert pre in observed or post in observed  # the poll actually ran and saw something


# ---------------------------------------------------------------------------
# 38-46: cross-process realtime coordination — the hard acceptance gate.
#
# PROCESS A below is a genuine `uvicorn` OS process (not a background
# thread in this test process — M14's own test suite already proved
# in-process delivery; what M15 adds is exactly the cross-process gap a
# thread cannot exercise, since a thread shares this process's Python
# heap, including its EventBus). PROCESS B is the genuine reset CLI,
# `python -m app.cli reset-demo --confirm`, run as its own subprocess.
# The only channel between them is PostgreSQL itself.
# ---------------------------------------------------------------------------


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_health(base_url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/api/health", timeout=1.0)
            if r.status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001 — retry until the deadline
            last_exc = exc
        time.sleep(0.2)
    raise RuntimeError(f"uvicorn subprocess never became healthy: {last_exc}")


class _UvicornSubprocess:
    def __init__(self, database_url: str) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        env = subprocess_env(database_url)
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=_BACKEND_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        _wait_for_health(self.base_url)

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


@pytest.fixture
def uvicorn_subprocess(reset_db_url: str) -> Iterator[_UvicornSubprocess]:
    server = _UvicornSubprocess(reset_db_url)
    try:
        yield server
    finally:
        server.stop()


def _iter_sse_events(resp: httpx.Response) -> Iterator[tuple[str, dict[str, Any]]]:
    event_name: str | None = None
    data_lines: list[str] = []
    for line in resp.iter_lines():
        if line == "":
            if event_name is not None:
                payload = json.loads("".join(data_lines)) if data_lines else {}
                yield event_name, payload
            event_name, data_lines = None, []
            continue
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())


def _run_reset_subprocess(
    reset_db_url: str, *, timeout: float = 60.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.cli", "reset-demo", "--confirm"],
        cwd=_BACKEND_ROOT,
        env=subprocess_env(reset_db_url),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class TestCrossProcessRealtime:
    def test_real_reset_cli_notifies_the_real_serving_process(
        self, reset_db_engine: Engine, reset_db_url: str, uvicorn_subprocess: _UvicornSubprocess
    ) -> None:
        token = _mint_attendee_session(reset_db_engine, user_email=fx.ATTENDEES[0].email)

        with (
            httpx.Client(
                base_url=uvicorn_subprocess.base_url, cookies={"session": token}, timeout=20.0
            ) as stream_client,
            stream_client.stream("GET", "/api/realtime/stream") as resp,
        ):
            assert resp.status_code == 200
            events = _iter_sse_events(resp)

            # 1. stream connected under generation G.
            name, data = next(events)
            assert name == "ready"
            assert data["dataset_generation"] == 1

            # 2. PROCESS B: the real reset CLI, an entirely separate
            # OS process, restores the dataset and commits G+1.
            result = _run_reset_subprocess(reset_db_url)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "generation: 1 -> 2" in result.stdout

            # 3-5. the serving process (PROCESS A) learned about the
            # reset AFTER that commit, via LISTEN/NOTIFY, and pushed
            # dataset_reset carrying the COMMITTED new generation.
            name, data = next(events)
            assert name == "dataset_reset"
            assert data.get("dataset_generation") == 2

            # the stream then closes — no further frames, real EOF.
            with pytest.raises(StopIteration):
                next(events)

        # 6. the serving process's own broker/subscriber state is not
        # just cleared but genuinely HEALTHY afterward: a fresh
        # connection works normally and reports the new generation.
        new_token = _mint_attendee_session(reset_db_engine, user_email=fx.ATTENDEES[0].email)
        with httpx.Client(
            base_url=uvicorn_subprocess.base_url, cookies={"session": new_token}, timeout=20.0
        ) as fresh_client:
            with fresh_client.stream("GET", "/api/realtime/stream") as resp2:
                assert resp2.status_code == 200
                name2, data2 = next(_iter_sse_events(resp2))
                assert name2 == "ready"
                assert data2["dataset_generation"] == 2

            # 8. next HTTP generation-bearing response reports G+1.
            health = fresh_client.get("/api/health")
            assert health.headers["X-Dataset-Generation"] == "2"

            # 7. a stale pre-reset queued hint has nothing left to
            # target: the OLD session is dead, so the old identity
            # cannot even authenticate to receive anything.
            stale = httpx.Client(base_url=uvicorn_subprocess.base_url, cookies={"session": token})
            try:
                r = stale.get("/api/attendees/me")
                assert r.status_code == 401
            finally:
                stale.close()

    def test_realtime_coordination_failure_leaves_maintenance_on(
        self, reset_db_engine: Engine, reset_db_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In-process unit-level companion to the real cross-process test
        above: if `_notify_reset` itself fails (e.g. the dedicated
        connection drops right after the restore commits), the CLI must
        report failure and leave maintenance ON — never silently succeed
        having skipped the live-client notification."""
        monkeypatch.setattr(
            reset_module,
            "_notify_reset",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated NOTIFY failure")),
        )
        with pytest.raises(RuntimeError):
            _run_reset(reset_db_url)
        assert (
            _scalar(reset_db_engine, "SELECT maintenance_mode FROM event_config WHERE id = 1")
            is True
        )
        assert (
            _scalar(reset_db_engine, "SELECT dataset_generation FROM event_config WHERE id = 1")
            == 2
        )


# ---------------------------------------------------------------------------
# 50-51: concurrent reset attempts
# ---------------------------------------------------------------------------


class TestConcurrentResets:
    def test_two_concurrent_real_reset_processes_cannot_both_run(
        self, reset_db_engine: Engine, reset_db_url: str
    ) -> None:
        env = subprocess_env(reset_db_url)
        p1 = subprocess.Popen(
            [sys.executable, "-m", "app.cli", "reset-demo", "--confirm"],
            cwd=_BACKEND_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        p2 = subprocess.Popen(
            [sys.executable, "-m", "app.cli", "reset-demo", "--confirm"],
            cwd=_BACKEND_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out1, err1 = p1.communicate(timeout=60)
        out2, err2 = p2.communicate(timeout=60)

        results = [(p1.returncode, out1, err1), (p2.returncode, out2, err2)]
        successes = [r for r in results if r[0] == 0]
        failures = [r for r in results if r[0] != 0]

        assert len(successes) == 1, f"expected exactly one success: {results}"
        assert len(failures) == 1, f"expected exactly one refusal: {results}"
        assert "already in progress" in failures[0][2].lower()

        # No G→G+2: exactly one reset actually ran.
        assert (
            _scalar(reset_db_engine, "SELECT dataset_generation FROM event_config WHERE id = 1")
            == 2
        )
        # No maintenance-off gap: the system ends up open, not stuck.
        assert (
            _scalar(reset_db_engine, "SELECT maintenance_mode FROM event_config WHERE id = 1")
            is False
        )

    def test_process_crash_releases_the_advisory_lock(
        self, reset_db_engine: Engine, reset_db_url: str
    ) -> None:
        """§19.4/§19.7: "PostgreSQL releases the session-scoped lock
        when the connection dies." A real subprocess acquires
        maintenance + the real fence via the module's own code path,
        announces it holds them, then is SIGKILLed (no `finally` can
        run) — the lock must still disappear from `pg_locks`, and
        `maintenance_mode = true` must still persist in the database
        (fail-safe survives an ungraceful death, not just a clean one).
        """
        script = f"""
import sys
sys.path.insert(0, {_BACKEND_ROOT!r})
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from app.modules.admin import reset as reset_module

engine = create_engine({reset_db_url!r}, poolclass=NullPool, future=True)
conn = engine.connect()
reset_module._try_acquire_admission_lock(conn)
reset_module._enter_maintenance(conn)
reset_module._acquire_fence(conn)
print("LOCKED", flush=True)
import time
time.sleep(120)
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=_BACKEND_ROOT,
            env=subprocess_env(reset_db_url),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            assert proc.stdout is not None
            line = proc.stdout.readline()
            assert line.strip() == "LOCKED", f"process never confirmed it held the lock: {line!r}"

            assert (
                _scalar(reset_db_engine, "SELECT maintenance_mode FROM event_config WHERE id = 1")
                is True
            )
            checker = _raw_conn(reset_db_url)
            try:
                with checker.cursor() as cur:
                    cur.execute(
                        f"SELECT count(*) FROM pg_locks WHERE {ADVISORY_LOCKS_HERE} "
                        "AND mode = 'ExclusiveLock' AND granted"
                    )
                    (count_before,) = cur.fetchone()  # type: ignore[misc]
                assert count_before >= 1, "the subprocess never actually held the fence"

                proc.send_signal(signal.SIGKILL)
                proc.wait(timeout=10)

                deadline = time.monotonic() + 10
                count_after = None
                while time.monotonic() < deadline:
                    with checker.cursor() as cur:
                        cur.execute(
                            f"SELECT count(*) FROM pg_locks WHERE {ADVISORY_LOCKS_HERE} "
                            "AND mode = 'ExclusiveLock' AND granted"
                        )
                        (count_after,) = cur.fetchone()  # type: ignore[misc]
                    if count_after == 0:
                        break
                    time.sleep(0.1)
                assert count_after == 0, (
                    "PostgreSQL did not release the lock after the process died"
                )
            finally:
                checker.close()

            # Fail-safe persists across the crash — still ON, needs an
            # explicit operator action to clear.
            assert (
                _scalar(reset_db_engine, "SELECT maintenance_mode FROM event_config WHERE id = 1")
                is True
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            _exec(reset_db_engine, "UPDATE event_config SET maintenance_mode = false WHERE id = 1")
