"""The demo reset critical section — ARCHITECTURE.md §19, M15.

CLI-only (`app/cli.py`'s `reset-demo` subcommand is the only caller in
production use); no HTTP route exists or ever will (§19.1, §24.8). This
module owns the entire sequence in §19.5, on ONE dedicated, non-pooled
connection — never `app.db.session.SessionLocal` / the application
pool, because a session-scoped advisory lock returned to a pool could
be inherited by an unrelated request (§19.4).

Every function below that takes a plain SQLAlchemy ``Connection``
(not a ``Session``) is deliberate: this whole module runs on the one
dedicated connection `run_demo_reset` opens, from maintenance-on
through unlock, and nothing here may open a second one for its own
convenience — that would defeat the entire point of "the same session
owns the lock throughout" (§19.4).

Failure policy (§19.7), restated because it is easy to get backwards:
maintenance is committed ON *first* (before the fence, before anything
destructive) and is cleared *only* on the full, verified success path
(`_exit_maintenance`, called last but one). No `finally` block anywhere
in this module touches `maintenance_mode` — the `finally` in
`run_demo_reset` releases the advisory lock and closes the connection
only. A restore that fails, or a restore that commits but whose
post-commit realtime coordination or verification then fails, both
leave the system in maintenance for an operator to recover explicitly
(`reset-demo --confirm` retry, or `maintenance --off` —
`_cmd_maintenance_off` in `app/cli.py`).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BindParameter, Connection, bindparam, create_engine, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.core.locks import FACEMARK_RESET_ADMISSION, FACEMARK_RESET_FENCE, RESET_NOTIFY_CHANNEL
from app.core.security import hash_password
from app.modules.attendees.models import UserStatus
from app.modules.booths.models import Booth, BoothInterest
from app.modules.interests.models import Interest
from app.modules.staff.models import StaffAccount
from app.seed import demo_fixtures as fx

logger = logging.getLogger(__name__)

# §19.5 step 4a — the exact, locked truncate list. RESTART IDENTITY
# CASCADE: every FK-dependent table not itself in this list (contact
# cards, sessions, etc. — checked against the live FK graph, not
# guessed) IS itself in this list, so CASCADE never reaches beyond it;
# it is specified anyway because that is the locked contract, not
# because it is expected to do extra work.
_RESET_SENSITIVE_TABLES: tuple[str, ...] = (
    "points_log",
    "idempotency_records",
    "feedback",
    "sessions",
    "otp_challenges",
    "consent_audit",
    "user_interests",
    "contact_cards",
    "users",
)

_FENCE_ACQUIRE_TIMEOUT_MS = 30_000


class ResetGuardError(RuntimeError):
    """A CLI-guard failed (§19.2). Raised before any connection is
    opened — no write of any kind has happened when this is raised."""


class ResetFenceTimeoutError(RuntimeError):
    """The exclusive advisory lock could not be acquired within the
    locked 30s timeout (§19.4). Maintenance is already ON (committed
    before this was attempted) and stays ON; nothing destructive has
    run."""


class ResetAlreadyInProgressError(RuntimeError):
    """Another `reset-demo` invocation already holds the admission
    lock (see `app/core/locks.py`'s `FACEMARK_RESET_ADMISSION`
    docstring). Raised before ANYTHING else — before maintenance is
    even entered — so a second concurrent invocation leaves no trace
    at all: no maintenance write, no fence contention, no risk of
    G→G+2 from two operators both invoking reset around the same
    moment."""


class ResetVerificationError(RuntimeError):
    """The restore committed, but a post-commit check did not confirm
    the expected state (§19.5 step 6 / §19.7). The dataset may already
    be at the new generation; maintenance stays ON for operator
    recovery."""


@dataclass(frozen=True)
class ResetReport:
    """§19.5 step 9: "counts, staff logins, demo card URLs, new
    generation" — everything `app.cli`'s `reset-demo` prints."""

    old_generation: int
    new_generation: int
    booths_restored: int
    interests_restored: int
    attendees: tuple[tuple[str, str], ...]  # (full name, public_token)
    staff_logins: tuple[str, ...]


def _guard_demo_and_environment(settings: Settings) -> None:
    """§19.2: "Refuses to run unless both DEMO_MODE=true and
    ENVIRONMENT != production ... Failing any check aborts with a
    non-zero exit and no writes." `--confirm` is checked by the CLI
    parser itself (`app/cli.py`) before this is ever called."""
    if not settings.demo_mode:
        raise ResetGuardError("DEMO_MODE is not enabled — refusing to run a demo reset.")
    if settings.is_production:
        raise ResetGuardError("ENVIRONMENT=production — refusing to run a demo reset.")


def _try_acquire_admission_lock(conn: Connection) -> bool:
    """Non-blocking. `True` means this invocation may proceed; `False`
    means another reset already holds it — the caller must not touch
    maintenance_mode, the real fence, or anything else."""
    acquired = conn.execute(
        text("SELECT pg_try_advisory_lock(:key)"), {"key": FACEMARK_RESET_ADMISSION}
    ).scalar_one()
    conn.commit()
    return bool(acquired)


def _release_admission_lock(conn: Connection) -> None:
    conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": FACEMARK_RESET_ADMISSION})
    conn.commit()


def _enter_maintenance(conn: Connection) -> None:
    """§19.5 step 1: committed FIRST, fail-safe, before anything else."""
    conn.execute(text("UPDATE event_config SET maintenance_mode = true WHERE id = 1"))
    conn.commit()


def _exit_maintenance(conn: Connection) -> None:
    """§19.5 step 7: cleared ONLY on the full success path — the last
    write this module performs before the fence is released."""
    conn.execute(text("UPDATE event_config SET maintenance_mode = false WHERE id = 1"))
    conn.commit()


def _describe_fence_holder(conn: Connection) -> str:
    """Best-effort diagnostic for a timed-out acquire (§19.5: "reports
    which backend holds it"). Never allowed to raise — a failure here
    must not mask the timeout it is trying to explain."""
    try:
        rows = conn.execute(
            text(
                "SELECT l.pid, a.usename, a.application_name, a.state, a.query_start "
                "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE l.locktype = 'advisory' AND l.mode = 'ExclusiveLock' AND l.granted"
            )
        ).all()
        conn.commit()
    except Exception:  # noqa: BLE001 — diagnostic only, never allowed to mask the real error
        return "unknown (diagnostic query failed)"
    if not rows:
        return "unknown (no exclusive advisory lock holder visible in pg_locks)"
    return "; ".join(
        f"pid={r.pid} user={r.usename} app={r.application_name!r} state={r.state}" for r in rows
    )


def _acquire_fence(conn: Connection) -> None:
    """§19.4 / §19.5 step 3: `pg_advisory_lock`, exclusive, session-
    scoped, on this dedicated connection, bounded by a 30s
    `lock_timeout` — which drains every in-flight mutation, since each
    one holds the shared side of the same key for the duration of its
    own transaction (`app/db/session.py`'s `mutating_transaction`).

    Deliberately session-level `SET`, not `SET LOCAL`: this connection
    is not going through a short-lived transactional unit of work, and
    a 30s cap on later statements in the same session is a harmless,
    mildly protective leftover, not a bug.
    """
    conn.execute(text(f"SET lock_timeout = '{_FENCE_ACQUIRE_TIMEOUT_MS}ms'"))
    try:
        conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": FACEMARK_RESET_FENCE})
    except OperationalError as exc:
        # The failed statement leaves this session's transaction
        # aborted; roll back before running any further statement
        # (including the diagnostic query below) on this connection.
        conn.rollback()
        holder = _describe_fence_holder(conn)
        raise ResetFenceTimeoutError(
            f"could not acquire the reset fence within {_FENCE_ACQUIRE_TIMEOUT_MS}ms; "
            f"held by: {holder}"
        ) from exc


def _release_fence(conn: Connection) -> None:
    """§19.4's `finally` half — best-effort; connection closure
    (`run_demo_reset`'s own `finally`) is the documented backstop if
    even this cannot run."""
    conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": FACEMARK_RESET_FENCE})
    conn.commit()


def _read_generation(conn: Connection) -> int:
    """Read BEFORE anything is touched, so `old_generation + 1` is
    computable even though the row itself is updated later in the same
    transaction (§19.5: "Preserve old generation long enough to
    calculate new_generation")."""
    return int(
        conn.execute(text("SELECT dataset_generation FROM event_config WHERE id = 1")).scalar_one()
    )


def _truncate_reset_sensitive_tables(conn: Connection) -> None:
    """§19.5 step 4a — the exact locked list, one statement, so
    `sessions` is truncated atomically with everything else (§19.6:
    "session invalidation either happens with the restore or not at
    all"). `points_log` and `idempotency_records` truncate together
    for the same reason (§19.6's worked pre-reset-replay case)."""
    tables = ", ".join(_RESET_SENSITIVE_TABLES)
    conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))


def _reconcile_reference_data(
    conn: Connection, *, admin_password: str, staff_password: str
) -> None:
    """§19.5 step 4b: "RECONCILE reference data to the fixture set —
    not merely upsert" (Codex pre-review Blocker 2). Runs strictly
    inside the FK dependency order below; each step's safety is
    argued in its own comment rather than assumed.

    `booth_interests` / `tasks` / `task_preset_points` have no
    downstream referrer anywhere in the schema (verified against the
    live FK graph — `points_log`/`feedback`, the only tables that
    reference `tasks`/`booths`, are already empty from step 4a), so a
    full wipe-and-reinsert is not merely an optimisation — it is the
    simplest form of "delete rows whose natural key is absent from
    fixtures" for a table with no partial-keep case to represent, and
    is strictly stronger than a diff (it also restores any in-place
    edit to a kept row, e.g. a rehearsal-adjusted weight).

    `staff_accounts` cannot use the same wipe-and-reinsert shortcut:
    `admin_audit.staff_account_id` is NOT NULL with no ON DELETE
    CASCADE, and `event_config.updated_by_staff_id` is a nullable
    back-reference — both inspected directly against
    `app/modules/admin/models.py` before writing this (the Codex
    pre-review's specific ask). Only the staff rows actually being
    removed (email absent from the fixture set) have their dependents
    touched: `event_config`'s back-reference is nulled and their own
    `admin_audit` rows are deleted — rehearsal debris for a rehearsal-
    only account, not a blind `TRUNCATE admin_audit`. A kept fixture
    staff account's audit trail, if M16 ever produces one, is left
    alone.
    """
    ids_param: BindParameter[Any] = bindparam("ids", expanding=True)
    slugs_param: BindParameter[Any] = bindparam("slugs", expanding=True)
    emails_param: BindParameter[Any] = bindparam("emails", expanding=True)

    # R1-R3: booth-owned configuration with no downstream referrer —
    # full wipe, reinserted from fixtures below (R10-R12).
    conn.execute(text("DELETE FROM booth_interests"))
    conn.execute(text("DELETE FROM tasks"))
    conn.execute(text("DELETE FROM task_preset_points"))

    # R4: staff reconciliation — delete only the rows actually absent
    # from the fixture set, clearing their FK-dependents precisely
    # first (children before the staff row itself).
    fixture_staff_emails = [s.email for s in fx.STAFF]
    extra_staff_ids = [
        row[0]
        for row in conn.execute(
            text("SELECT id FROM staff_accounts WHERE email NOT IN :emails").bindparams(
                emails_param
            ),
            {"emails": fixture_staff_emails},
        ).all()
    ]
    if extra_staff_ids:
        conn.execute(
            text(
                "UPDATE event_config SET updated_by_staff_id = NULL "
                "WHERE updated_by_staff_id IN :ids"
            ).bindparams(ids_param),
            {"ids": extra_staff_ids},
        )
        conn.execute(
            text("DELETE FROM admin_audit WHERE staff_account_id IN :ids").bindparams(ids_param),
            {"ids": extra_staff_ids},
        )
        conn.execute(
            text("DELETE FROM staff_accounts WHERE id IN :ids").bindparams(ids_param),
            {"ids": extra_staff_ids},
        )

    # R5: booths absent from the fixture set. Safe now: every
    # staff_accounts row that could have pointed at a non-fixture booth
    # was, by construction, itself non-fixture and is already gone (R4)
    # — a kept (fixture) staff row only ever points at a fixture booth.
    fixture_booth_slugs = [b.slug for b in fx.BOOTHS]
    conn.execute(
        text("DELETE FROM booths WHERE slug NOT IN :slugs").bindparams(slugs_param),
        {"slugs": fixture_booth_slugs},
    )

    # R6: interests absent from the fixture set. Safe now: booth_interests
    # (R1) and user_interests (already truncated, step 4a) are both empty.
    fixture_interest_slugs = [i.slug for i in fx.INTERESTS]
    conn.execute(
        text("DELETE FROM interests WHERE slug NOT IN :slugs").bindparams(slugs_param),
        {"slugs": fixture_interest_slugs},
    )

    # R7: upsert fixture interests — DO UPDATE, not DO NOTHING, so an
    # in-place edit to a kept row is also restored (Blocker 2).
    for i in fx.INTERESTS:
        conn.execute(
            pg_insert(Interest)
            .values(slug=i.slug, name=i.name, sort_order=i.sort_order, is_active=True)
            .on_conflict_do_update(
                index_elements=["slug"],
                set_={"name": i.name, "sort_order": i.sort_order, "is_active": True},
            )
        )
    interest_ids = {
        row[0]: row[1] for row in conn.execute(text("SELECT slug, id FROM interests")).all()
    }

    # R8: upsert fixture booths, restoring every fixture-controlled
    # field to its fixture/schema-default value.
    for b in fx.BOOTHS:
        booth_values: dict[str, object] = {
            "slug": b.slug,
            "name": b.name,
            "description": b.description,
            "location_label": b.location_label,
            "logo_url": None,
            "is_active": True,
            "allow_repeat_task_points": None,
        }
        conn.execute(
            pg_insert(Booth)
            .values(**booth_values)
            .on_conflict_do_update(
                index_elements=["slug"],
                set_={k: v for k, v in booth_values.items() if k != "slug"},
            )
        )
    booth_ids = {row[0]: row[1] for row in conn.execute(text("SELECT slug, id FROM booths")).all()}

    # R9: upsert fixture staff — restores password, role, booth
    # assignment and login-security state (a rehearsal lockout must
    # not survive a reset) for every fixture account.
    admin_hash = hash_password(admin_password)
    staff_hash = hash_password(staff_password)
    for s in fx.STAFF:
        password_hash = admin_hash if s.booth_slug is None else staff_hash
        staff_values: dict[str, object] = {
            "email": s.email,
            "password_hash": password_hash,
            "full_name": s.full_name,
            "role": s.role,
            "booth_id": booth_ids[s.booth_slug] if s.booth_slug is not None else None,
            "is_active": True,
            "failed_login_count": 0,
            "locked_until": None,
        }
        conn.execute(
            pg_insert(StaffAccount)
            .values(**staff_values)
            .on_conflict_do_update(
                index_elements=["email"],
                set_={k: v for k, v in staff_values.items() if k != "email"},
            )
        )

    # R10-R12: reinsert booth-owned configuration into the now-empty
    # tables from R1-R3.
    for b in fx.BOOTHS:
        for bi in b.interests:
            conn.execute(
                pg_insert(BoothInterest).values(
                    booth_id=booth_ids[b.slug],
                    interest_id=interest_ids[bi.interest_slug],
                    weight=bi.weight,
                )
            )
        for t in b.tasks:
            conn.execute(
                text(
                    "INSERT INTO tasks (booth_id, title, description, sort_order) "
                    "VALUES (:booth_id, :title, :description, :sort_order)"
                ),
                {
                    "booth_id": booth_ids[b.slug],
                    "title": t.title,
                    "description": t.description,
                    "sort_order": t.sort_order,
                },
            )
    for p in fx.PRESET_POINTS:
        conn.execute(
            text(
                "INSERT INTO task_preset_points (booth_id, points, label, sort_order) "
                "VALUES (NULL, :points, :label, :sort_order)"
            ),
            {"points": p.points, "label": p.label, "sort_order": p.sort_order},
        )


def _restore_event_config(conn: Connection, *, new_generation: int) -> None:
    """§19.5 steps 4c/4f: restores the fixture-controlled tunables and
    advances the generation — deliberately EXCLUDES `maintenance_mode`
    (already true; stays true through this whole transaction and
    beyond, per §19.7) and does not blindly apply
    `EVENT_CONFIG_DEFAULTS["dataset_generation"] = 1` (Codex pre-review
    Blocker 2) — `new_generation` is `old_generation + 1`, computed by
    the caller from `_read_generation`'s pre-restore snapshot.
    `updated_by_staff_id` is nulled: this is a system-driven restore,
    attributable to no admin.
    """
    conn.execute(
        text(
            "UPDATE event_config SET "
            "feedback_points = :feedback_points, "
            "allow_repeat_task_points_default = :allow_repeat, "
            "leaderboard_top_n = :leaderboard_top_n, "
            "dataset_generation = :generation, "
            "updated_by_staff_id = NULL, "
            "updated_at = now() "
            "WHERE id = 1"
        ),
        {
            "feedback_points": fx.EVENT_CONFIG_DEFAULTS["feedback_points"],
            "allow_repeat": fx.EVENT_CONFIG_DEFAULTS["allow_repeat_task_points_default"],
            "leaderboard_top_n": fx.EVENT_CONFIG_DEFAULTS["leaderboard_top_n"],
            "generation": new_generation,
        },
    )


def _insert_demo_attendees(conn: Connection) -> tuple[tuple[str, str], ...]:
    """§19.5 step 4d: fixed `public_token`s (§19.8 — printed QR
    survival), status ACTIVE, immediately scannable. `users` was just
    `RESTART IDENTITY`-truncated (step 4a), so a plain INSERT is
    correct here, not an upsert — there is nothing to conflict with."""
    now = datetime.now(UTC)
    interest_ids = {
        row[0]: row[1] for row in conn.execute(text("SELECT slug, id FROM interests")).all()
    }
    attendees: list[tuple[str, str]] = []
    for a in fx.ATTENDEES:
        user_id = conn.execute(
            text(
                "INSERT INTO users "
                "(first_name, last_name, email, phone, profession, sponsor_consent, "
                "status, activated_at) "
                "VALUES (:first_name, :last_name, :email, :phone, :profession, false, "
                ":status, :activated_at) RETURNING id"
            ),
            {
                "first_name": a.first_name,
                "last_name": a.last_name,
                "email": a.email,
                "phone": a.phone,
                "profession": a.profession,
                "status": UserStatus.ACTIVE.value,
                "activated_at": now,
            },
        ).scalar_one()
        for slug in a.interest_slugs:
            conn.execute(
                text("INSERT INTO user_interests (user_id, interest_id) VALUES (:u, :i)"),
                {"u": user_id, "i": interest_ids[slug]},
            )
        conn.execute(
            text(
                "INSERT INTO contact_cards (user_id, public_token, issued_at) "
                "VALUES (:u, :token, :issued_at)"
            ),
            {"u": user_id, "token": a.public_token, "issued_at": now},
        )
        attendees.append((f"{a.first_name} {a.last_name}", a.public_token))
    return tuple(attendees)


def _notify_reset(conn: Connection, *, new_generation: int) -> None:
    """§19.5 step 5, cross-process half (Codex pre-review Blocker 1):
    `NOTIFY` is only ever sent on this dedicated connection AFTER the
    restore transaction above has committed — never from inside it,
    since a payload sent inside an uncommitted transaction is only
    delivered to other sessions once (and if) that transaction
    commits, and this module never wants a partially-restored dataset
    to trigger a live client refetch. The serving Uvicorn process's own
    listener (`app/modules/realtime/listener.py`) does the actual
    `event_bus.publish_broadcast` + `event_bus.reset()` — the CLI
    process cannot reach that broker directly, LISTEN/NOTIFY is the
    transport, not the mechanism.

    Best-effort: a lost notification never threatens correctness
    (`dataset_generation` mismatch remains authoritative, §19.3) so
    this deliberately does not retry or verify delivery.
    """
    payload = json.dumps({"dataset_generation": new_generation})
    conn.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": RESET_NOTIFY_CHANNEL, "payload": payload},
    )
    conn.commit()


def _verify_restore(conn: Connection, *, expected_generation: int) -> None:
    """§19.5 step 6 / §19.7: "abort to the failure path if they
    disagree" — maintenance stays ON (the caller never reaches
    `_exit_maintenance` if this raises)."""
    generation = conn.execute(
        text("SELECT dataset_generation FROM event_config WHERE id = 1")
    ).scalar_one()
    if generation != expected_generation:
        raise ResetVerificationError(
            f"expected dataset_generation {expected_generation}, found {generation}"
        )
    attendee_count = conn.execute(text("SELECT count(*) FROM users")).scalar_one()
    if attendee_count != len(fx.ATTENDEES):
        raise ResetVerificationError(
            f"expected {len(fx.ATTENDEES)} demo attendees, found {attendee_count}"
        )
    booth_count = conn.execute(text("SELECT count(*) FROM booths")).scalar_one()
    if booth_count != len(fx.BOOTHS):
        raise ResetVerificationError(f"expected {len(fx.BOOTHS)} booths, found {booth_count}")
    conn.commit()


def run_demo_reset(
    *,
    admin_password: str,
    staff_password: str,
    settings: Settings | None = None,
    database_url: str | None = None,
) -> ResetReport:
    """The full §19.5 sequence, on one dedicated `NullPool` connection.

    `settings`/`database_url` are overridable only for tests (an
    isolated database, never the real one — see `tests/test_reset.py`
    for how destructive reset tests avoid touching genuine data). The
    real CLI entrypoint (`app/cli.py`) calls this with neither, so both
    default to the process's real configuration.
    """
    cfg = settings if settings is not None else get_settings()
    _guard_demo_and_environment(cfg)
    url = database_url if database_url is not None else cfg.database_url

    # Dedicated, non-pooled engine (§19.4) — never
    # `app.db.session.SessionLocal` / the application's pooled engine.
    engine = create_engine(url, poolclass=NullPool, future=True)
    conn = engine.connect()
    admission_acquired = False
    lock_acquired = False
    try:
        try:
            # Checked before ANYTHING else — before maintenance, before
            # the real fence. See FACEMARK_RESET_ADMISSION's docstring
            # (app/core/locks.py): this is what stops a second
            # concurrent `reset-demo` from silently running the whole
            # destructive sequence twice (G -> G+1 -> G+2).
            admission_acquired = _try_acquire_admission_lock(conn)
            if not admission_acquired:
                raise ResetAlreadyInProgressError(
                    "another demo reset is already in progress — refusing to run a second one"
                )

            _enter_maintenance(conn)
            _acquire_fence(conn)
            lock_acquired = True

            old_generation = _read_generation(conn)
            _truncate_reset_sensitive_tables(conn)
            _reconcile_reference_data(
                conn, admin_password=admin_password, staff_password=staff_password
            )
            new_generation = old_generation + 1
            _restore_event_config(conn, new_generation=new_generation)
            attendees = _insert_demo_attendees(conn)
            conn.commit()  # §19.5 step 4 — the single atomic restore transaction

            _notify_reset(conn, new_generation=new_generation)  # step 5
            _verify_restore(conn, expected_generation=new_generation)  # step 6
            _exit_maintenance(conn)  # step 7 — success only
        except Exception:
            # Never clears maintenance (module docstring). Rolls back
            # any transaction still open on this connection — a no-op
            # if the restore already committed, which is exactly what
            # a post-commit coordination/verification failure needs:
            # nothing left to undo, nothing here undoes it.
            conn.rollback()
            raise
    finally:
        if lock_acquired:
            try:
                _release_fence(conn)  # step 8 — released LAST, never before maintenance-off above
            except Exception:
                logger.exception("reset: failed to release the advisory lock explicitly")
        if admission_acquired:
            try:
                _release_admission_lock(conn)
            except Exception:
                logger.exception("reset: failed to release the admission lock explicitly")
        conn.close()  # backstop (§19.4): PostgreSQL frees a session-scoped
        engine.dispose()  # advisory lock when the owning session ends, even if unlock itself failed

    return ResetReport(
        old_generation=old_generation,
        new_generation=new_generation,
        booths_restored=len(fx.BOOTHS),
        interests_restored=len(fx.INTERESTS),
        attendees=attendees,
        staff_logins=tuple(s.email for s in fx.STAFF),
    )
