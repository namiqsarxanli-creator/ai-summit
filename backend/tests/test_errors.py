"""M02 acceptance: the constraint-name -> domain-error mapping foundation.

ARCHITECTURE.md §7.14 / invariant #24: dispatch by constraint NAME,
never by which error PostgreSQL happens to report first. An unmapped
name is a bug, not a guess.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import (
    CONSTRAINT_ERROR_MAP,
    DomainError,
    DuplicateFeedbackRewardError,
    DuplicateTaskRewardError,
    IdempotencyKeyConflict,
    PointsAlreadyReversedError,
    UnexpectedConstraintViolationError,
    translate_integrity_error,
)


def _fake_integrity_error(constraint_name: str | None) -> IntegrityError:
    """Builds a minimal IntegrityError whose `.orig.diag.constraint_name`
    matches what psycopg actually populates, without needing a live
    database for this unit-level test."""
    diag = MagicMock()
    diag.constraint_name = constraint_name
    orig = MagicMock()
    orig.diag = diag
    return IntegrityError(statement="INSERT ...", params={}, orig=orig)


@pytest.mark.parametrize(
    ("constraint_name", "expected_cls"),
    [
        ("uq_points_feedback_user_booth", DuplicateFeedbackRewardError),
        ("uq_points_dedupe_key", DuplicateTaskRewardError),
        ("pk_idempotency_records", IdempotencyKeyConflict),
        # a row can be reversed once: a second reversal is a user-facing 409 now (M-reversal)
        ("uq_points_reverses", PointsAlreadyReversedError),
        ("uq_contact_card_live_user", UnexpectedConstraintViolationError),
        ("ck_staff_role_booth", UnexpectedConstraintViolationError),
        ("fk_points_task_booth", UnexpectedConstraintViolationError),
        ("pk_user_interests", UnexpectedConstraintViolationError),
        ("uq_preset_global_points", UnexpectedConstraintViolationError),
    ],
)
def test_every_architecture_named_constraint_is_mapped(
    constraint_name: str, expected_cls: type[Exception]
) -> None:
    """The nine constraints ARCHITECTURE.md §7.14 names exactly all
    resolve to their documented outcome."""
    result = translate_integrity_error(_fake_integrity_error(constraint_name))
    assert isinstance(result, expected_cls)


def test_unmapped_constraint_is_not_guessed() -> None:
    """An unrecognised (but present) constraint name is NOT silently
    matched to something — it becomes UnexpectedConstraintViolationError
    (INTERNAL_ERROR), explicitly logged as unmapped, never invented."""
    result = translate_integrity_error(
        _fake_integrity_error("some_future_constraint_nobody_registered")
    )
    assert isinstance(result, UnexpectedConstraintViolationError)
    assert result.code == "INTERNAL_ERROR"


def test_violation_with_no_constraint_name_is_returned_unchanged() -> None:
    """Not every IntegrityError has a constraint name (e.g. some NOT
    NULL violations). translate_integrity_error must not crash on
    this — it hands back the original error rather than guessing."""
    original = _fake_integrity_error(None)
    result = translate_integrity_error(original)
    assert result is original


def test_idempotency_conflict_is_not_a_domain_error() -> None:
    """ARCHITECTURE.md §13.4: a pk_idempotency_records conflict is a
    control-flow signal (wait-then-replay), never a terminal error —
    IdempotencyKeyConflict is deliberately outside the DomainError
    hierarchy so it cannot be accidentally propagated straight to an
    HTTP response."""
    assert not issubclass(IdempotencyKeyConflict, DomainError)


def test_domain_errors_carry_their_documented_catalogue_code() -> None:
    assert DuplicateFeedbackRewardError().code == "DUPLICATE_FEEDBACK"
    assert DuplicateFeedbackRewardError.status_code == 409
    assert DuplicateTaskRewardError().code == "DUPLICATE_TASK_REWARD"
    assert DuplicateTaskRewardError.status_code == 409


def test_constraint_map_has_no_duplicate_targets_by_accident() -> None:
    """Sanity check on the map itself: every one of the nine
    architecture-named constraints is present exactly once."""
    architecture_named = {
        "uq_points_feedback_user_booth",
        "uq_points_dedupe_key",
        "pk_idempotency_records",
        "uq_points_reverses",
        "uq_contact_card_live_user",
        "ck_staff_role_booth",
        "fk_points_task_booth",
        "pk_user_interests",
        "uq_preset_global_points",
    }
    # All nine are registered, including the two whose PostgreSQL name
    # is convention-generated rather than hand-specified
    # (pk_idempotency_records, pk_user_interests) — registration in
    # this map is about the OUTCOME (replay vs. error vs. unexpected),
    # not about whether the name itself needed a naming-convention
    # override to reach its final form.
    registered = architecture_named & CONSTRAINT_ERROR_MAP.keys()
    assert registered == architecture_named
