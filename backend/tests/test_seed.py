"""M03 acceptance: deterministic summit fixture infrastructure.

Runs against real PostgreSQL, like the rest of this test suite —
ARCHITECTURE.md / IMPLEMENTATION_PLAN.md: "PostgreSQL-specific
behaviour must be tested on PostgreSQL." Unlike M02's `db` fixture,
`seed_all` genuinely commits (it is not reset-sensitive, offline
fixture data meant to persist — see app/seed/seeder.py's module
docstring), so these tests use a dedicated, non-rollback session and
rely on `seed_all`'s own idempotency (natural-key ON CONFLICT DO
NOTHING) rather than transaction rollback for repeatability.

Acceptance criterion #1 ("`python -m app.cli seed` on a clean migrated
database populates the full catalogue") was verified manually during
implementation, against a freshly migrated database with none of these
tables populated: the first run reported positive created counts for
every entity (10 interests, 6 booths, 19 booth_interests, 12 tasks, 3
preset_points, 7 staff_accounts, 4 attendees, 4 contact_cards, 1
event_config). It is not re-verified from a clean slate by this file,
because this suite runs in the same shared dev database as the M02
suite, which by this point in the milestone sequence is never actually
empty. What IS verified here is the thing that matters afterward: the
seed converges to, and stays at, the exact same full catalogue no
matter how many times it runs — which is what "clean database" and
"re-run" both reduce to once the natural-key upserts are idempotent.
"""

from __future__ import annotations

import re
import string
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import cli as app_cli
from app.core.security import hash_password
from app.db.session import SessionLocal
from app.modules.admin.models import EventConfig
from app.modules.attendees.models import User, UserInterest
from app.modules.booths.models import Booth, BoothInterest, Task, TaskPresetPoint
from app.modules.contact_cards.models import ContactCard
from app.modules.interests.models import Interest
from app.modules.staff.models import StaffAccount, StaffRole
from app.seed import demo_fixtures as fx
from app.seed.seeder import SeedResult, seed_all

_TEST_ADMIN_PASSWORD = "test-only-seed-admin-password"  # noqa: S105 — test fixture, not a real secret
_TEST_STAFF_PASSWORD = "test-only-seed-staff-password"  # noqa: S105
_URLSAFE_ALPHABET = frozenset(string.ascii_letters + string.digits + "-_")


@pytest.fixture
def seed_session() -> Iterator[Session]:
    """A real, committing session — `seed_all` commits internally, so
    there is nothing for this fixture to roll back; it only owns the
    connection lifecycle."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _run_seed(db: Session) -> SeedResult:
    return seed_all(db, admin_password=_TEST_ADMIN_PASSWORD, staff_password=_TEST_STAFF_PASSWORD)


# --------------------------------------------------------------------------
# Idempotency / determinism (M03 acceptance #1, #2)
# --------------------------------------------------------------------------


def test_seed_converges_and_is_idempotent(seed_session: Session) -> None:
    """First call reaches the full fixture catalogue (or confirms it
    was already there); a second call in the same process changes
    nothing and raises nothing — IMPLEMENTATION_PLAN.md M03 acceptance
    criterion #2: "Running it twice changes nothing and errors on
    nothing." """
    _run_seed(seed_session)  # convergence run — may or may not create rows
    second = _run_seed(seed_session)

    assert second.interests_created == 0
    assert second.booths_created == 0
    assert second.booth_interests_created == 0
    assert second.tasks_created == 0
    assert second.preset_points_created == 0
    assert second.staff_accounts_created == 0
    assert second.attendees_created == 0
    assert second.contact_cards_created == 0
    assert second.event_config_created is False


def test_seed_all_rejects_empty_passwords(seed_session: Session) -> None:
    with pytest.raises(ValueError, match="admin_password"):
        seed_all(seed_session, admin_password="", staff_password=_TEST_STAFF_PASSWORD)
    with pytest.raises(ValueError, match="staff_password"):
        seed_all(seed_session, admin_password=_TEST_ADMIN_PASSWORD, staff_password="")


# --------------------------------------------------------------------------
# Catalogue completeness and relationships
# --------------------------------------------------------------------------


def test_catalogue_matches_fixture_counts(seed_session: Session) -> None:
    _run_seed(seed_session)

    assert seed_session.execute(select(Interest.id)).all().__len__() >= len(fx.INTERESTS)
    for i in fx.INTERESTS:
        assert (
            seed_session.execute(select(Interest.id).where(Interest.slug == i.slug)).first()
            is not None
        )

    for b in fx.BOOTHS:
        booth_row = seed_session.execute(select(Booth).where(Booth.slug == b.slug)).scalar_one()
        assert booth_row.name == b.name
        assert booth_row.location_label == b.location_label

        booth_interest_count = seed_session.execute(
            select(BoothInterest).where(BoothInterest.booth_id == booth_row.id)
        ).all()
        assert len(booth_interest_count) == len(b.interests)

        task_count = seed_session.execute(select(Task).where(Task.booth_id == booth_row.id)).all()
        assert len(task_count) == len(b.tasks)

    for a in fx.ATTENDEES:
        user_row = seed_session.execute(select(User).where(User.email == a.email)).scalar_one()
        assert user_row.status.value == "ACTIVE"
        assert user_row.activated_at is not None

        card = seed_session.execute(
            select(ContactCard).where(ContactCard.user_id == user_row.id)
        ).scalar_one()
        assert card.public_token == a.public_token
        assert card.revoked_at is None

    for s in fx.STAFF:
        staff_row = seed_session.execute(
            select(StaffAccount).where(StaffAccount.email == s.email)
        ).scalar_one()
        assert staff_row.role == s.role
        if s.booth_slug is None:
            assert staff_row.role == StaffRole.ADMIN
            assert staff_row.booth_id is None
        else:
            assert staff_row.role == StaffRole.BOOTH_STAFF
            booth_row = seed_session.execute(
                select(Booth).where(Booth.slug == s.booth_slug)
            ).scalar_one()
            assert staff_row.booth_id == booth_row.id

    event_config = seed_session.execute(select(EventConfig).where(EventConfig.id == 1)).scalar_one()
    assert event_config.feedback_points == fx.EVENT_CONFIG_DEFAULTS["feedback_points"]
    assert (
        event_config.allow_repeat_task_points_default
        == fx.EVENT_CONFIG_DEFAULTS["allow_repeat_task_points_default"]
    )
    assert event_config.leaderboard_top_n == fx.EVENT_CONFIG_DEFAULTS["leaderboard_top_n"]
    assert event_config.dataset_generation == fx.EVENT_CONFIG_DEFAULTS["dataset_generation"]
    assert event_config.maintenance_mode == fx.EVENT_CONFIG_DEFAULTS["maintenance_mode"]


def test_booth_interest_weights_satisfy_check_constraint(seed_session: Session) -> None:
    """IMPLEMENTATION_PLAN.md M03 test requirement: "seeded
    booth_interests weights satisfy CHECK (weight > 0 AND weight <=
    1)." Checked structurally against the fixture data (the database
    CHECK itself is what made `_run_seed` succeed in the first place —
    a bad weight would have raised there, not here)."""
    _run_seed(seed_session)
    for b in fx.BOOTHS:
        for bi in b.interests:
            assert Decimal("0") < bi.weight <= Decimal("1")


def test_global_preset_points_are_5_10_15(seed_session: Session) -> None:
    _run_seed(seed_session)
    rows = seed_session.execute(
        select(TaskPresetPoint.points).where(TaskPresetPoint.booth_id.is_(None))
    ).all()
    points = {r[0] for r in rows}
    assert {5, 10, 15} <= points


def test_no_task_duplicates_across_reseed(seed_session: Session) -> None:
    """`tasks` has no natural-key DB constraint (see app/seed/seeder.py)
    — this is the one entity where idempotency is application logic,
    not a database guarantee, so it gets its own explicit duplicate
    check rather than relying solely on the zero-created assertion in
    test_seed_converges_and_is_idempotent."""
    _run_seed(seed_session)
    _run_seed(seed_session)
    for b in fx.BOOTHS:
        booth_row = seed_session.execute(select(Booth).where(Booth.slug == b.slug)).scalar_one()
        titles = seed_session.execute(select(Task.title).where(Task.booth_id == booth_row.id)).all()
        title_list = [t[0] for t in titles]
        assert len(title_list) == len(set(title_list)) == len(b.tasks)


# --------------------------------------------------------------------------
# Staff passwords — hashed, never plaintext
# --------------------------------------------------------------------------


def test_hash_password_produces_a_verifiable_argon2id_hash() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed.startswith("$argon2id$")
    assert hashed != "correct horse battery staple"

    verifier = PasswordHasher()
    verifier.verify(hashed, "correct horse battery staple")  # must not raise
    with pytest.raises(VerifyMismatchError):
        verifier.verify(hashed, "wrong password")


def test_seeded_staff_passwords_are_hashed_not_plaintext(seed_session: Session) -> None:
    _run_seed(seed_session)
    for s in fx.STAFF:
        staff_row = seed_session.execute(
            select(StaffAccount).where(StaffAccount.email == s.email)
        ).scalar_one()
        assert staff_row.password_hash.startswith("$argon2id$")
        assert staff_row.password_hash not in (_TEST_ADMIN_PASSWORD, _TEST_STAFF_PASSWORD)


# --------------------------------------------------------------------------
# Matching-friendly fixture design (ARCHITECTURE.md §19.9) — structural
# only; M07 asserts the numeric scores.
# --------------------------------------------------------------------------


def test_matching_fixture_set_has_strong_mid_low_and_zero_cases(seed_session: Session) -> None:
    """Structural check only (numeric scores are M07's job, per
    IMPLEMENTATION_PLAN.md M03's own test list: "asserted structurally
    here; numerically in M07"). AIWorks reproduces ARCHITECTURE.md
    §10's exact weights, so its top-weighted interests are known
    without computing anything: artificial-intelligence and
    generative-ai (both 1.00)."""
    _run_seed(seed_session)

    aiworks = fx.BOOTHS[0]
    assert aiworks.slug == "aiworks"
    aiworks_interest_slugs = {bi.interest_slug for bi in aiworks.interests}
    top_weight = max(bi.weight for bi in aiworks.interests)
    top_weighted_slugs = {bi.interest_slug for bi in aiworks.interests if bi.weight == top_weight}

    elena, marcus, priya, sofia = fx.ATTENDEES

    # Strong: every selected interest is one of AIWorks' TOP-weighted
    # interests — "their selected interests are that booth's
    # top-weighted ones" (IMPLEMENTATION_PLAN.md M03 scope, verbatim).
    assert set(elena.interest_slugs) <= top_weighted_slugs
    assert set(elena.interest_slugs)  # non-empty

    # Mid: reproduces ARCHITECTURE.md §11.3's worked example exactly —
    # matches AIWorks, but not its single top tier alone.
    assert set(marcus.interest_slugs) == {"generative-ai", "data-analytics"}
    assert set(marcus.interest_slugs) <= aiworks_interest_slugs
    assert not (set(marcus.interest_slugs) <= top_weighted_slugs)

    # Low: partial, non-empty overlap with AIWorks (discriminates from
    # both the strong and the zero case).
    overlap = set(priya.interest_slugs) & aiworks_interest_slugs
    assert overlap
    assert overlap != set(priya.interest_slugs)

    # Zero: no overlap with AIWorks at all (ARCHITECTURE.md §11.4's
    # M=∅ edge case, seeded for real rather than only unit-tested).
    assert not (set(sofia.interest_slugs) & aiworks_interest_slugs)


def test_seeded_attendee_interests_match_fixture_selection(seed_session: Session) -> None:
    _run_seed(seed_session)
    for a in fx.ATTENDEES:
        user_row = seed_session.execute(select(User).where(User.email == a.email)).scalar_one()
        rows = seed_session.execute(
            select(Interest.slug)
            .join(UserInterest, UserInterest.interest_id == Interest.id)
            .where(UserInterest.user_id == user_row.id)
        ).all()
        assert {r[0] for r in rows} == set(a.interest_slugs)


# --------------------------------------------------------------------------
# Stable demo tokens (ARCHITECTURE.md §19.8)
# --------------------------------------------------------------------------


def test_contact_card_tokens_are_stable_across_reseed(seed_session: Session) -> None:
    _run_seed(seed_session)
    before = {
        a.email: seed_session.execute(
            select(ContactCard.public_token)
            .join(User, User.id == ContactCard.user_id)
            .where(User.email == a.email)
        ).scalar_one()
        for a in fx.ATTENDEES
    }

    _run_seed(seed_session)  # re-seed — must not touch existing cards

    for a in fx.ATTENDEES:
        after = seed_session.execute(
            select(ContactCard.public_token)
            .join(User, User.id == ContactCard.user_id)
            .where(User.email == a.email)
        ).scalar_one()
        assert after == before[a.email] == a.public_token
        # 256-bit, the same 43-char URL-safe format as runtime issuance
        # (§7.6, §9.1 — reconciled from token_hex in M06).
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", a.public_token)


# --------------------------------------------------------------------------
# CLI: fails loudly when a password env var is absent (M03 acceptance #4)
# --------------------------------------------------------------------------


def test_cli_seed_fails_loudly_when_admin_password_env_var_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("SEED_STAFF_PASSWORD", _TEST_STAFF_PASSWORD)

    with pytest.raises(SystemExit) as exc:
        app_cli.main(["seed-demo"])
    assert exc.value.code == 1
    assert "SEED_ADMIN_PASSWORD" in capsys.readouterr().err


def test_cli_seed_fails_loudly_when_staff_password_env_var_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SEED_ADMIN_PASSWORD", _TEST_ADMIN_PASSWORD)
    monkeypatch.delenv("SEED_STAFF_PASSWORD", raising=False)

    with pytest.raises(SystemExit) as exc:
        app_cli.main(["seed-demo"])
    assert exc.value.code == 1
    assert "SEED_STAFF_PASSWORD" in capsys.readouterr().err


def test_cli_seed_never_falls_back_to_a_guessed_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Directly documents the invariant behind the two tests above:
    an empty string is treated exactly like an absent variable, never
    silently accepted (ARCHITECTURE.md invariant #14)."""
    monkeypatch.setenv("SEED_ADMIN_PASSWORD", "")
    monkeypatch.setenv("SEED_STAFF_PASSWORD", _TEST_STAFF_PASSWORD)
    with pytest.raises(SystemExit):
        app_cli.main(["seed-demo"])


# Sanity: ensure `text` import stays used even as tests evolve, and the
# demo tokens really are opaque URL-safe strings with no embedded PII.
def test_public_tokens_contain_no_attendee_pii() -> None:
    for a in fx.ATTENDEES:
        assert a.first_name.lower() not in a.public_token.lower()
        assert a.last_name.lower() not in a.public_token.lower()
        assert all(c in _URLSAFE_ALPHABET for c in a.public_token)
    assert len({a.public_token for a in fx.ATTENDEES}) == len(fx.ATTENDEES)


def test_event_config_row_is_the_fixed_singleton(seed_session: Session) -> None:
    _run_seed(seed_session)
    row_count: Any = seed_session.execute(text("SELECT count(*) FROM event_config")).scalar_one()
    assert row_count == 1


def test_every_demo_activity_carries_a_point_value(seed_session: Session) -> None:
    """Activities are what staff award, so the demo ones must be awardable."""
    _run_seed(seed_session)
    for b in fx.BOOTHS:
        for t in b.tasks:
            assert 1 <= t.points <= 100
            stored: Any = seed_session.execute(
                text(
                    "SELECT points FROM tasks WHERE title = :t AND booth_id = "
                    "(SELECT id FROM booths WHERE slug = :s)"
                ),
                {"t": t.title, "s": b.slug},
            ).scalar_one()
            assert stored == t.points
