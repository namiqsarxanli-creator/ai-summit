"""Operator entrypoint — ARCHITECTURE.md's documented file tree names
this ``app/cli.py`` the "demo reset / seed entrypoint".

Usage::

    python -m app.cli seed
    python -m app.cli purge-provisional
    python -m app.cli reset-demo --confirm
    python -m app.cli maintenance --off
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import text

from app.db.session import SessionLocal, mutating_transaction
from app.modules.admin.reset import (
    ResetAlreadyInProgressError,
    ResetFenceTimeoutError,
    ResetGuardError,
    ResetVerificationError,
    run_demo_reset,
)
from app.modules.attendees.service import purge_provisional
from app.seed.seeder import seed_all

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


def _cmd_seed() -> None:
    admin_password = _require_env(_ADMIN_PASSWORD_ENV)
    staff_password = _require_env(_STAFF_PASSWORD_ENV)

    db = SessionLocal()
    try:
        result = seed_all(db, admin_password=admin_password, staff_password=staff_password)
    finally:
        db.close()

    print("Seed complete (created this run / already present is 0):")
    print(f"  interests:       {result.interests_created}")
    print(f"  booths:          {result.booths_created}")
    print(f"  booth_interests: {result.booth_interests_created}")
    print(f"  tasks:           {result.tasks_created}")
    print(f"  preset_points:   {result.preset_points_created}")
    print(f"  staff_accounts:  {result.staff_accounts_created}")
    print(f"  attendees:       {result.attendees_created}")
    print(f"  contact_cards:   {result.contact_cards_created}")
    print(f"  event_config:    {'created' if result.event_config_created else 'already present'}")


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("seed", help="Seed deterministic demo fixtures (safe to re-run).")
    subparsers.add_parser(
        "purge-provisional", help="Delete PROVISIONAL attendees older than 24h (safe to re-run)."
    )
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
        _cmd_seed()
    elif args.command == "purge-provisional":
        _cmd_purge_provisional()
    elif args.command == "reset-demo":
        _cmd_reset_demo(confirm=args.confirm)
    elif args.command == "maintenance":
        _cmd_maintenance(off=args.off)


if __name__ == "__main__":
    main()
