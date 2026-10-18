"""Operator entrypoint — ARCHITECTURE.md's documented file tree names
this ``app/cli.py`` the "demo reset / seed entrypoint".

Real event (production-safe)::

    python -m app.cli bootstrap                         # settings only, no content
    python -m app.cli create-admin --email E --name N   # password asked, hidden
    python -m app.cli demo-inventory [--json]           # read-only demo-data report
    python -m app.cli masterclass-skeleton              # event, 2 blocks, 5 rooms; no masterclasses
    python -m app.cli competition-report                # read-only dry run before activation
    python -m app.cli competition-backfill-profile --confirm
    python -m app.cli founder-recompute [--confirm]   # only once FOUNDER_RULE_CONFIRMED=true

Demo / rehearsal only (DEMO_MODE=true, never production)::

    python -m app.cli seed-demo

OTP email delivery::

    python -m app.cli generate-otp-key              # new OTP_OUTBOX_ENCRYPTION_KEY value
    python -m app.cli otp-outbox-cleanup [--older-than-days N]
    python -m app.cli ops-check [--json] [--no-db] [--worker] [--media]
    #   ^ read-only health report; exit 0 ok / 1 warn / 2 crit
    # the worker itself:  python -m app.worker

Maintenance::

    python -m app.cli purge-provisional
    python -m app.cli reset-demo --confirm
    python -m app.cli maintenance --off
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core import ops_status
from app.core.config import get_settings
from app.core.media import get_store
from app.core.media_guard import janitor_session
from app.db.session import SessionLocal, mutating_transaction
from app.modules.admin.reset import (
    ResetAlreadyInProgressError,
    ResetFenceTimeoutError,
    ResetGuardError,
    ResetVerificationError,
    run_demo_reset,
)
from app.modules.attendees.models import User
from app.modules.attendees.service import purge_provisional
from app.modules.auth import otp_outbox
from app.modules.auth.otp_crypto import generate_key
from app.modules.staff.initial_admin import (
    InitialAdminError,
    create_initial_admin,
    ensure_not_a_conflicting_account,
    find_account,
    normalise_email,
    normalise_name,
    validate_password,
)
from app.seed.baseline import seed_baseline
from app.seed.inventory import build_inventory, format_report
from app.seed.seeder import DemoSeedRefusedError, ensure_demo_seed_allowed, seed_all

# No fallback default, by design — ARCHITECTURE.md invariant #14 ("No
# secret has a source-code fallback default. Missing configuration
# fails at startup") and §19.8 ("Staff passwords come from environment
# variables with no fallback; the CLI fails loudly if unset").
_ADMIN_PASSWORD_ENV = "SEED_ADMIN_PASSWORD"
_STAFF_PASSWORD_ENV = "SEED_STAFF_PASSWORD"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(
            f"error: {name} is not set — refusing to seed with a guessed password",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return value


def _cmd_seed_demo() -> None:
    """Loads the fictional summit. The environment guard runs first —
    before any password is read and before a database session exists —
    so a refused run cannot have written anything."""
    try:
        ensure_demo_seed_allowed(get_settings())
    except DemoSeedRefusedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    admin_password = _require_env(_ADMIN_PASSWORD_ENV)
    staff_password = _require_env(_STAFF_PASSWORD_ENV)

    db = SessionLocal()
    try:
        result = seed_all(db, admin_password=admin_password, staff_password=staff_password)
    finally:
        db.close()

    print("Demo seed complete (created this run / already present is 0):")
    print(f"  interests:       {result.interests_created}")
    print(f"  booths:          {result.booths_created}")
    print(f"  booth_interests: {result.booth_interests_created}")
    print(f"  tasks:           {result.tasks_created}")
    print(f"  preset_points:   {result.preset_points_created}")
    print(f"  staff_accounts:  {result.staff_accounts_created}")
    print(f"  attendees:       {result.attendees_created}")
    print(f"  contact_cards:   {result.contact_cards_created}")
    print(f"  event_config:    {'created' if result.event_config_created else 'already present'}")


def _cmd_bootstrap() -> None:
    """Real-event initial setup: the settings the application cannot run
    without, and nothing else (see app/seed/baseline.py). Creates no
    booths, tasks, attendees or staff accounts, needs no password, and
    never overwrites a value that already exists."""
    db = SessionLocal()
    try:
        with mutating_transaction(db) as tx:
            result = seed_baseline(tx)
    finally:
        db.close()

    print("Bootstrap complete (created this run / already present is 0):")
    print(f"  preset_points: {result.preset_points_created}")
    print(f"  event_config:  {'created' if result.event_config_created else 'already present'}")
    print("No booths, tasks, attendees or staff accounts are created by this command.")
    print("Next: python -m app.cli create-admin --email <address> --name <full name>")


def _cmd_create_admin(*, email: str, name: str) -> None:
    """Creates the first administrator. The password is read from a
    hidden interactive prompt only — it is not accepted as an argument or
    environment variable, is never printed or logged, and is stored only
    as an Argon2id hash. If the account already exists nothing is
    changed (its password in particular), and no password is asked."""
    try:
        address = normalise_email(email)
        full_name = normalise_name(name)

        db = SessionLocal()
        try:
            existing = find_account(db, address)
            if existing is not None:
                ensure_not_a_conflicting_account(existing)
                print(f"Administrator {address} already exists. Nothing was changed.")
                return
            db.rollback()

            if not sys.stdin.isatty():
                print(
                    "error: the password is asked interactively; run this in a terminal "
                    "(with Docker Compose use `docker compose exec backend ...`, not `-T`).",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            password = getpass.getpass("New administrator password: ")
            if getpass.getpass("Repeat the password: ") != password:
                print(
                    "error: the two passwords do not match; nothing was written.", file=sys.stderr
                )
                raise SystemExit(1)
            validate_password(password)

            with mutating_transaction(db) as tx:
                result = create_initial_admin(
                    tx, email=address, full_name=full_name, password=password
                )
        finally:
            db.close()
    except InitialAdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if result.created:
        print(f"Administrator {result.email} created. Sign in at /staff/login.")
    else:
        print(f"Administrator {result.email} already exists. Nothing was changed.")


def _cmd_demo_inventory(*, as_json: bool) -> None:
    """Read-only report of demo-looking data in the current database."""
    db = SessionLocal()
    try:
        report = build_inventory(db)
    finally:
        db.close()

    if as_json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        print(format_report(report))


def _cmd_generate_otp_key() -> None:
    """Prints a fresh OTP_OUTBOX_ENCRYPTION_KEY. Writes nothing anywhere:
    copy it into the environment file yourself and keep it secret."""
    print(generate_key())


def _cmd_otp_outbox_cleanup(*, older_than_days: int) -> None:
    """Housekeeping for the delivery outbox (the worker also does this
    continuously): expires rows whose code has lapsed - wiping their
    encrypted payload - and deletes finished rows older than the retention
    period. Never touches a live delivery or any OTP challenge."""
    db = SessionLocal()
    try:
        with mutating_transaction(db) as tx:
            expired, lost = otp_outbox.run_maintenance(tx)
            purged = otp_outbox.purge_finished(tx, older_than_days=older_than_days)
    finally:
        db.close()
    print(f"expired (lapsed before sending): {expired}")
    print(f"given up (worker kept dying):    {lost}")
    print(f"finished records deleted (> {older_than_days} days): {purged}")


def _cmd_ops_check(*, as_json: bool, no_db: bool, worker: bool, media: bool) -> int:
    """Read-only health report for the operator (see app/core/ops_status.py).

    Prints counts, ages and fixed reason codes - never an address, a code, a
    token or a connection string. The exit code is the worst level (0 ok,
    1 warn, 2 crit). `--worker` reads the worker's own status files, so run it
    inside the worker container; `--media` probes the media volume, so run it
    where that volume is mounted (the backend or cli container)."""
    results: list[ops_status.CheckResult] = []
    if not no_db:
        settings = get_settings()
        # Its own short-lived engine with hard timeouts: a health check that hangs
        # on a dead database is worse than none.
        engine = create_engine(
            settings.database_url,
            poolclass=NullPool,
            connect_args={"connect_timeout": 5, "options": "-c statement_timeout=5000"},
        )
        results.append(ops_status.check_database(engine))
        try:
            if results[0].level == "ok":
                with Session(engine) as db:
                    results.append(ops_status.check_migrations(db))
                    results.extend(ops_status.check_outbox(db))
        finally:
            engine.dispose()
    if worker:
        from app.worker import HEARTBEAT_FILE, STATUS_FILE

        results.append(ops_status.check_worker(STATUS_FILE, heartbeat_file=HEARTBEAT_FILE))
    if media:
        results.extend(ops_status.check_media(get_store()))
    print(ops_status.format_json(results) if as_json else ops_status.format_text(results))
    return ops_status.exit_code(results)


def _cmd_purge_provisional() -> None:
    """ARCHITECTURE.md §8.1: removes PROVISIONAL users older than 24h,
    with their interests, contact card and challenges. Idempotent.

    Run standalone (this CLI command), this can race with live
    registration/OTP traffic, so it goes through the same shared
    reset-fence unit of work every other mutation does
    (app/db/session.py's `mutating_transaction`) — no second locking
    mechanism. `purge_provisional` itself stays fence-agnostic (it only
    takes a plain Session) so M15's reset can call it directly on its
    own already-locked connection without this CLI-level wrapper
    double-acquiring the fence.
    """
    db = SessionLocal()
    try:
        with mutating_transaction(db) as tx:
            count = purge_provisional(tx)
    finally:
        db.close()

    print(f"Purged {count} PROVISIONAL user(s) older than 24h.")


def _cmd_purge_orphan_photos(*, grace_minutes: int, dry_run: bool) -> None:
    """Removes profile-photo files no attendee references any more (a crash
    between writing the files and committing the row, a deleted or reset
    account, a replaced photo whose cleanup failed).

    Only files older than the grace period are considered, because an upload
    writes its files BEFORE its row commits: a young file may belong to one in
    flight. Reads the referenced keys (a plain read, no write, no fence) and
    touches only `<MEDIA_ROOT>/avatars/<key>-<size>.jpg` and its own temporary
    files - never another name. `--dry-run` reports without deleting."""
    db = SessionLocal()
    try:
        referenced = {
            key
            for key in db.execute(select(User.photo_key).where(User.photo_key.is_not(None)))
            .scalars()
            .all()
            if key is not None
        }
        # Pair photos (migration 0025) share the media volume: their files are in use too.
        from app.modules.people.pair_photos import referenced_keys

        referenced |= referenced_keys(db)
    finally:
        db.close()
    store = get_store()
    if dry_run:
        report = store.purge_orphans(referenced, grace_seconds=grace_minutes * 60.0, dry_run=True)
    else:
        # A backup holds the media lock exclusively while it dumps and archives
        # (app/core/media_guard.py). Deleting then is exactly what it must prevent.
        with janitor_session() as allowed:
            if not allowed:
                print(
                    "error: a backup is running (it holds the media lock); nothing was "
                    "removed. Run this again when it has finished.",
                    file=sys.stderr,
                )
                raise SystemExit(3)
            report = store.purge_orphans(
                referenced, grace_seconds=grace_minutes * 60.0, dry_run=False
            )
    verb = "would remove" if dry_run else "removed"
    print(f"{verb} orphan photo files:  {report.removed_files}")
    print(f"{verb} stale temporary files: {report.removed_temporary}")
    print(f"kept (referenced):          {report.kept_referenced}")
    print(f"kept (younger than {grace_minutes} min): {report.kept_recent}")


def _cmd_reset_demo(*, confirm: bool) -> None:
    """§19.1/§19.2: the ONLY entrypoint for a demo reset — there is no
    HTTP path (§24.8). `--confirm` is checked here, before
    `run_demo_reset` ever opens a connection, alongside DEMO_MODE and
    ENVIRONMENT (checked inside `run_demo_reset` itself, so the same
    guard applies to a direct Python caller too, e.g. a test) — all
    three refuse before any write (§19.2: "Failing any check aborts
    with a non-zero exit and no writes")."""
    if not confirm:
        print(
            "error: refusing to run a demo reset without --confirm "
            "(this destroys and rebuilds the entire dataset)",
            file=sys.stderr,
        )
        raise SystemExit(1)

    admin_password = _require_env(_ADMIN_PASSWORD_ENV)
    staff_password = _require_env(_STAFF_PASSWORD_ENV)

    try:
        report = run_demo_reset(admin_password=admin_password, staff_password=staff_password)
    except ResetGuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except ResetAlreadyInProgressError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "Nothing was touched by this invocation. Wait for the other reset to finish.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    except ResetFenceTimeoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "The dataset was not touched. Maintenance mode is ON. "
            "Investigate the holding backend, then retry.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    except ResetVerificationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "The restore may have committed but could not be verified. "
            "Maintenance mode remains ON for operator recovery — "
            "see `python -m app.cli maintenance --off` once you have confirmed the dataset.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    except Exception as exc:  # noqa: BLE001 — every failure path here still reports ON, then exits
        print(f"error: demo reset failed: {exc}", file=sys.stderr)
        print("Maintenance mode remains ON for operator recovery.", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"Demo reset complete. generation: {report.old_generation} -> {report.new_generation}")
    print(f"  booths restored:    {report.booths_restored}")
    print(f"  interests restored: {report.interests_restored}")
    print("  staff logins:")
    for email in report.staff_logins:
        print(f"    {email}")
    print("  demo attendees (name, card token):")
    for name, token in report.attendees:
        print(f"    {name}: {token}")
    print("Maintenance mode is OFF. The system is ready.")


def _cmd_maintenance(*, off: bool) -> None:
    """§19.7's documented manual recovery path: `maintenance --off`.
    Deliberate operator action only — never automatic, and never
    invoked by any failure path above."""
    if not off:
        print("error: nothing to do — pass --off to clear maintenance mode", file=sys.stderr)
        raise SystemExit(1)

    db = SessionLocal()
    try:
        db.execute(text("UPDATE event_config SET maintenance_mode = false WHERE id = 1"))
        db.commit()
    finally:
        db.close()

    print("Maintenance mode is OFF.")


def _cmd_masterclass_skeleton() -> None:
    """Adds the confirmed frame of the masterclass programme (event, blocks,
    rooms) - no masterclass titles or speakers. Safe to re-run: only what is
    missing is added. Fenced like every other mutation."""
    from app.modules.masterclasses.skeleton import create_program_skeleton

    db = SessionLocal()
    try:
        with mutating_transaction(db) as tx:
            report = create_program_skeleton(tx)
    finally:
        db.close()
    print(
        f"Masterclass programme: event {'created' if report.event_created else 'already present'}, "
        f"{report.blocks_created} block(s) and {report.rooms_created} room(s) added."
    )


def _cmd_competition_report() -> int:
    """Read-only: what activating the competition would change, then whether the
    event is configured for the day (competition/readiness.py). Exit 1 while a
    readiness check is missing. Prints counts, times and row ids - no names,
    contact data or setting values."""
    from app.modules.competition.activation import report
    from app.modules.competition.readiness import check, ready

    db = SessionLocal()
    try:
        r = report(db)
        checks = check(db)
    finally:
        db.rollback()
        db.close()
    print(f"competition_mode: {r.competition_mode}")
    close = r.closes_at.isoformat() if r.closes_at else "NOT SET (nothing can be earned)"
    print(f"closes_at: {close}")
    print(f"active attendees: {r.active_attendees}")
    print(f"excluded by the organiser: {r.excluded_attendees}")
    print(
        f"legacy booth points that stop counting: {r.legacy_points_total} in {r.legacy_rows} rows"
    )
    print(f"competition points so far: {r.competition_points_total}")
    print(f"complete profiles not yet paid PROFILE: {r.profile_backfill_candidates}")
    print()
    print("event readiness (read-only):")
    for c in checks:
        print(f"  [{c.status:<7}] {c.key}: {c.detail}")
    verdict = "READY" if ready(checks) else "NOT READY - fix the [missing] lines"
    print(f"event readiness: {verdict}")
    return 0 if ready(checks) else 1


def _cmd_founder_recompute(*, confirm: bool) -> None:
    """BEYOND_NOW.md, "Runtime safeguards": after the organiser confirms the Founder
    rule and FOUNDER_RULE_CONFIRMED is set. Without --confirm: a read-only report."""
    from app.core.config import get_settings
    from app.modules.competition.founder import recompute_all

    if not get_settings().founder_rule_confirmed:
        raise SystemExit(
            "Refusing: FOUNDER_RULE_CONFIRMED is false. The organiser must confirm the "
            "Founder rule first (BEYOND_NOW.md)."
        )
    db = SessionLocal()
    try:
        with mutating_transaction(db) as tx:
            counts = recompute_all(tx, apply=confirm)
    finally:
        db.close()
    for key, value in counts.items():
        print(f"{key}: {value}")
    if not confirm:
        print(
            "Dry run: nothing was written. Re-run with --confirm, then take a new final snapshot."
        )


def _cmd_competition_backfill_profile(*, confirm: bool) -> None:
    if not confirm:
        raise SystemExit("Refusing without --confirm. Run competition-report first.")
    from app.modules.competition.activation import backfill_profile

    db = SessionLocal()
    try:
        with mutating_transaction(db) as tx:
            paid = backfill_profile(tx)
    finally:
        db.close()
    print(f"PROFILE paid to {paid} complete profile(s).")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "seed-demo",
        help="Load the fictional demo summit (safe to re-run). DEMO_MODE only; never production.",
    )
    subparsers.add_parser("seed", help="Removed - use `bootstrap` (real) or `seed-demo` (demo).")
    subparsers.add_parser(
        "competition-report", help="Read-only report before activating the competition."
    )
    backfill_parser = subparsers.add_parser(
        "competition-backfill-profile", help="Pay PROFILE to existing complete profiles."
    )
    backfill_parser.add_argument("--confirm", action="store_true")
    founder_parser = subparsers.add_parser(
        "founder-recompute",
        help="After the Founder rule is confirmed: align every bonus (dry run by default).",
    )
    founder_parser.add_argument("--confirm", action="store_true")
    subparsers.add_parser(
        "masterclass-skeleton",
        help="Add the masterclass event, its 2 blocks and 5 rooms (no masterclasses; re-runnable).",
    )
    subparsers.add_parser(
        "bootstrap",
        help="Real event: create only the required initial settings (safe to re-run).",
    )
    admin_parser = subparsers.add_parser(
        "create-admin", help="Create the first administrator; the password is asked, hidden."
    )
    admin_parser.add_argument("--email", required=True)
    admin_parser.add_argument("--name", required=True, help="Full name")
    subparsers.add_parser(
        "generate-otp-key", help="Print a new OTP_OUTBOX_ENCRYPTION_KEY (stores nothing)."
    )
    cleanup_parser = subparsers.add_parser(
        "otp-outbox-cleanup", help="Expire lapsed OTP deliveries and delete old finished records."
    )
    cleanup_parser.add_argument(
        "--older-than-days",
        type=int,
        default=otp_outbox.DEFAULT_RETENTION_DAYS,
        help="Retention for finished records (default %(default)s).",
    )
    ops_parser = subparsers.add_parser(
        "ops-check",
        help="Read-only health report (database, migrations, OTP outbox, worker, media). "
        "Exit code 0 ok, 1 warning, 2 critical.",
    )
    ops_parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    ops_parser.add_argument(
        "--no-db", action="store_true", help="Skip the database, migration and outbox checks."
    )
    ops_parser.add_argument(
        "--worker",
        action="store_true",
        help="Also check the worker's status files (run in the worker container).",
    )
    ops_parser.add_argument(
        "--media",
        action="store_true",
        help="Also probe the media volume (run where it is mounted).",
    )
    inventory_parser = subparsers.add_parser(
        "demo-inventory", help="Read-only report of demo data in this database."
    )
    inventory_parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    subparsers.add_parser(
        "purge-provisional", help="Delete PROVISIONAL attendees older than 24h (safe to re-run)."
    )
    photos_parser = subparsers.add_parser(
        "purge-orphan-photos",
        help="Delete profile-photo files no attendee references (safe to re-run).",
    )
    photos_parser.add_argument(
        "--grace-minutes",
        type=int,
        default=60,
        help="Only files older than this are touched (default %(default)s).",
    )
    photos_parser.add_argument("--dry-run", action="store_true", help="Report, delete nothing.")
    reset_parser = subparsers.add_parser(
        "reset-demo", help="Destroy and rebuild the entire demo dataset (§19). Requires --confirm."
    )
    reset_parser.add_argument("--confirm", action="store_true", help="Required — no default yes.")
    maintenance_parser = subparsers.add_parser(
        "maintenance", help="Manual maintenance-mode recovery (§19.7)."
    )
    maintenance_parser.add_argument(
        "--off", action="store_true", help="Clear maintenance mode after operator verification."
    )

    args = parser.parse_args(argv)

    if args.command == "seed":
        print(
            "error: `seed` was split. Use `bootstrap` for a real event, "
            "or `seed-demo` for the demo (DEMO_MODE=true).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    elif args.command == "seed-demo":
        _cmd_seed_demo()
    elif args.command == "bootstrap":
        _cmd_bootstrap()
    elif args.command == "create-admin":
        _cmd_create_admin(email=args.email, name=args.name)
    elif args.command == "generate-otp-key":
        _cmd_generate_otp_key()
    elif args.command == "otp-outbox-cleanup":
        _cmd_otp_outbox_cleanup(older_than_days=args.older_than_days)
    elif args.command == "ops-check":
        raise SystemExit(
            _cmd_ops_check(
                as_json=args.json, no_db=args.no_db, worker=args.worker, media=args.media
            )
        )
    elif args.command == "demo-inventory":
        _cmd_demo_inventory(as_json=args.json)
    elif args.command == "purge-provisional":
        _cmd_purge_provisional()
    elif args.command == "purge-orphan-photos":
        _cmd_purge_orphan_photos(grace_minutes=args.grace_minutes, dry_run=args.dry_run)
    elif args.command == "reset-demo":
        _cmd_reset_demo(confirm=args.confirm)
    elif args.command == "maintenance":
        _cmd_maintenance(off=args.off)
    elif args.command == "masterclass-skeleton":
        _cmd_masterclass_skeleton()
    elif args.command == "competition-report":
        raise SystemExit(_cmd_competition_report())
    elif args.command == "competition-backfill-profile":
        _cmd_competition_backfill_profile(confirm=args.confirm)
    elif args.command == "founder-recompute":
        _cmd_founder_recompute(confirm=args.confirm)


if __name__ == "__main__":
    main()
