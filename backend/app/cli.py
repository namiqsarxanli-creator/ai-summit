"""Operator entrypoint — ARCHITECTURE.md's documented file tree names
this ``app/cli.py`` the "demo reset / seed entrypoint". Reset (M15)
attaches its own subcommand later without changing this shape.

Usage::

    python -m app.cli seed
    python -m app.cli purge-provisional
"""

from __future__ import annotations

import argparse
import os
import sys

from app.db.session import SessionLocal, mutating_transaction
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("seed", help="Seed deterministic demo fixtures (safe to re-run).")
    subparsers.add_parser(
        "purge-provisional", help="Delete PROVISIONAL attendees older than 24h (safe to re-run)."
    )

    args = parser.parse_args(argv)

    if args.command == "seed":
        _cmd_seed()
    elif args.command == "purge-provisional":
        _cmd_purge_provisional()


if __name__ == "__main__":
    main()
