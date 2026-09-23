"""Operator entrypoint — ARCHITECTURE.md's documented file tree names
this ``app/cli.py`` the "demo reset / seed entrypoint". M03 scope:
``seed`` only. Reset (M15) and ``purge-provisional`` (M04) attach their
own subcommands later without changing this shape.

Usage::

    python -m app.cli seed
"""

from __future__ import annotations

import argparse
import os
import sys

from app.db.session import SessionLocal
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("seed", help="Seed deterministic demo fixtures (safe to re-run).")

    args = parser.parse_args(argv)

    if args.command == "seed":
        _cmd_seed()


if __name__ == "__main__":
    main()
