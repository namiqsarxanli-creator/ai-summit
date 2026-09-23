"""M02 acceptance: every database-level invariant fails at the DATABASE,
not in application code.

ARCHITECTURE.md §7.14: "A SELECT that checks for an existing row before
inserting is explicitly forbidden as an enforcement mechanism." These
tests never pre-check; they attempt the forbidden state directly and
assert PostgreSQL itself rejects it, then confirm the name Python sees
back is exactly the one app/core/errors.py's map keys on.

Real PostgreSQL only (IMPLEMENTATION_PLAN.md M02): every test here uses
the `db` fixture, a genuine psycopg-backed SQLAlchemy session.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def _constraint_name(exc: pytest.ExceptionInfo[IntegrityError]) -> str | None:
    diag = getattr(exc.value.orig, "diag", None)
    return getattr(diag, "constraint_name", None)


# --------------------------------------------------------------------------
# users / citext
# --------------------------------------------------------------------------


def test_duplicate_case_insensitive_email_rejected(db: Session) -> None:
    """citext: 'Ada@X.com' and 'ada@x.com' collide on the same UNIQUE
    constraint (ARCHITECTURE.md §8.1 "Email normalised... citext
    uniqueness enforced by the database")."""
    db.execute(
        text(
            "INSERT INTO users (first_name, last_name, email, phone, profession, "
            "sponsor_consent, status, activated_at) VALUES "
            "('Ada', 'A', 'Ada@Example.com', '+1', 'Eng', false, 'ACTIVE', now())"
        )
    )
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO users (first_name, last_name, email, phone, profession, "
                "sponsor_consent, status, activated_at) VALUES "
                "('Eve', 'B', 'ada@example.com', '+2', 'Eng', false, 'ACTIVE', now())"
            )
        )
    assert _constraint_name(exc) == "uq_users_email"


def test_provisional_user_without_activated_at_is_allowed(db: Session) -> None:
    """The status/activated_at CHECK must NOT reject the ordinary
    provisional case — only PROVISIONAL-with-no-activated_at is fine;
    it's ACTIVE-with-no-activated_at that's forbidden (next test)."""
    db.execute(
        text(
            "INSERT INTO users (first_name, last_name, email, phone, profession, "
            "sponsor_consent, status) VALUES "
            "('Provo', 'Sional', 'provo@example.com', '+1', 'Eng', false, 'PROVISIONAL')"
        )
    )  # must not raise


def test_active_user_without_activated_at_rejected(db: Session) -> None:
    """ARCHITECTURE.md §8.1: CHECK (status = 'PROVISIONAL' OR
    activated_at IS NOT NULL) — an ACTIVE user with no activation
    timestamp is structurally impossible."""
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO users (first_name, last_name, email, phone, profession, "
                "sponsor_consent, status, activated_at) VALUES "
                "('Bad', 'State', 'bad@example.com', '+1', 'Eng', false, 'ACTIVE', NULL)"
            )
        )
    assert _constraint_name(exc) == "ck_users_status_requires_activated_at"


def test_sponsor_consent_true_without_timestamp_rejected(db: Session) -> None:
    """ARCHITECTURE.md §7.1: CHECK (sponsor_consent = false OR
    sponsor_consent_at IS NOT NULL)."""
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO users (first_name, last_name, email, phone, profession, "
                "sponsor_consent, sponsor_consent_at, status) VALUES "
                "('Con', 'Sent', 'consent@example.com', '+1', 'Eng', true, NULL, 'PROVISIONAL')"
            )
        )
    assert _constraint_name(exc) == "ck_users_sponsor_consent_requires_timestamp"


# --------------------------------------------------------------------------
# user_interests
# --------------------------------------------------------------------------


def test_duplicate_interest_selection_rejected(
    db: Session, seeded_user_id: int, seeded_interest_id: int
) -> None:
    """ARCHITECTURE.md §7.3: composite PK (user_id, interest_id)
    "prevents duplicate selection at the database level."""
    db.execute(
        text("INSERT INTO user_interests (user_id, interest_id) VALUES (:u, :i)"),
        {"u": seeded_user_id, "i": seeded_interest_id},
    )
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text("INSERT INTO user_interests (user_id, interest_id) VALUES (:u, :i)"),
            {"u": seeded_user_id, "i": seeded_interest_id},
        )
    assert _constraint_name(exc) == "pk_user_interests"


# --------------------------------------------------------------------------
# booth_interests
# --------------------------------------------------------------------------


def test_booth_interest_weight_out_of_range_rejected(
    db: Session, seeded_booth_id: int, seeded_interest_id: int
) -> None:
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO booth_interests (booth_id, interest_id, weight) VALUES (:b, :i, 1.5)"
            ),
            {"b": seeded_booth_id, "i": seeded_interest_id},
        )
    assert _constraint_name(exc) == "ck_booth_interests_weight_range"


# --------------------------------------------------------------------------
# contact_cards — one live card per attendee
# --------------------------------------------------------------------------


def test_duplicate_live_contact_card_rejected(db: Session, seeded_user_id: int) -> None:
    """ARCHITECTURE.md §7.6: partial unique (user_id) WHERE revoked_at
    IS NULL — "an attendee has at most one live card"."""
    db.execute(
        text(
            "INSERT INTO contact_cards (user_id, public_token, issued_at) "
            "VALUES (:u, 'tok-a', now())"
        ),
        {"u": seeded_user_id},
    )
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO contact_cards (user_id, public_token, issued_at) "
                "VALUES (:u, 'tok-b', now())"
            ),
            {"u": seeded_user_id},
        )
    assert _constraint_name(exc) == "uq_contact_card_live_user"


def test_revoked_card_does_not_block_a_new_live_card(db: Session, seeded_user_id: int) -> None:
    """The partial index only guards LIVE cards — a revoked one must
    not block rotation (ARCHITECTURE.md §9.6)."""
    db.execute(
        text(
            "INSERT INTO contact_cards (user_id, public_token, issued_at, revoked_at) "
            "VALUES (:u, 'tok-old', now(), now())"
        ),
        {"u": seeded_user_id},
    )
    db.execute(
        text(
            "INSERT INTO contact_cards (user_id, public_token, issued_at) "
            "VALUES (:u, 'tok-new', now())"
        ),
        {"u": seeded_user_id},
    )  # must not raise


# --------------------------------------------------------------------------
# staff_accounts — the booth invariant
# --------------------------------------------------------------------------


def test_booth_staff_without_booth_rejected(db: Session) -> None:
    """ARCHITECTURE.md §7.7 / §12.2: "not representable... for a booth
    staff account to lack a booth."."""
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO staff_accounts (email, password_hash, full_name, role, booth_id) "
                "VALUES ('nb@example.com', 'x', 'No Booth', 'BOOTH_STAFF', NULL)"
            )
        )
    assert _constraint_name(exc) == "ck_staff_role_booth"


def test_admin_with_a_booth_rejected(db: Session, seeded_booth_id: int) -> None:
    """The other half of the same CHECK: an ADMIN must NOT have a booth."""
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO staff_accounts (email, password_hash, full_name, role, booth_id) "
                "VALUES ('adm@example.com', 'x', 'Admin', 'ADMIN', :b)"
            ),
            {"b": seeded_booth_id},
        )
    assert _constraint_name(exc) == "ck_staff_role_booth"


def test_admin_without_booth_is_allowed(db: Session) -> None:
    db.execute(
        text(
            "INSERT INTO staff_accounts (email, password_hash, full_name, role, booth_id) "
            "VALUES ('adm2@example.com', 'x', 'Admin', 'ADMIN', NULL)"
        )
    )  # must not raise


def test_booth_staff_with_booth_is_allowed(db: Session, seeded_booth_id: int) -> None:
    db.execute(
        text(
            "INSERT INTO staff_accounts (email, password_hash, full_name, role, booth_id) "
            "VALUES ('bs@example.com', 'x', 'Staff', 'BOOTH_STAFF', :b)"
        ),
        {"b": seeded_booth_id},
    )  # must not raise


# --------------------------------------------------------------------------
# sessions — exactly one principal
# --------------------------------------------------------------------------


def test_session_with_no_principal_rejected(db: Session) -> None:
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO sessions (token_hash, principal_type, issued_at, expires_at) "
                "VALUES ('th1', 'ATTENDEE', now(), now() + interval '1 hour')"
            )
        )
    # Either CHECK can legitimately fire first for this row shape;
    # both are structurally correct rejections of the same bad state.
    assert _constraint_name(exc) in {
        "ck_sessions_exactly_one_principal",
        "ck_sessions_principal_type_matches_reference",
    }


def test_session_with_both_principals_rejected(
    db: Session, seeded_user_id: int, seeded_staff_id: int
) -> None:
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO sessions (token_hash, principal_type, user_id, staff_account_id, "
                "issued_at, expires_at) VALUES "
                "('th2', 'ATTENDEE', :u, :s, now(), now() + interval '1 hour')"
            ),
            {"u": seeded_user_id, "s": seeded_staff_id},
        )
    assert _constraint_name(exc) == "ck_sessions_exactly_one_principal"


def test_session_principal_type_mismatch_rejected(db: Session, seeded_staff_id: int) -> None:
    """principal_type says ATTENDEE but only staff_account_id is set."""
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO sessions (token_hash, principal_type, staff_account_id, "
                "issued_at, expires_at) VALUES "
                "('th3', 'ATTENDEE', :s, now(), now() + interval '1 hour')"
            ),
            {"s": seeded_staff_id},
        )
    assert _constraint_name(exc) == "ck_sessions_principal_type_matches_reference"


# --------------------------------------------------------------------------
# task_preset_points — the corrected global/booth partial uniqueness
# --------------------------------------------------------------------------


def test_duplicate_global_preset_rejected(db: Session) -> None:
    """The exact bug ARCHITECTURE.md §7.10 corrects: two booth_id=NULL
    rows with the same points must collide, unlike a plain
    UNIQUE(booth_id, points)."""
    db.execute(text("INSERT INTO task_preset_points (booth_id, points) VALUES (NULL, 5)"))
    with pytest.raises(IntegrityError) as exc:
        db.execute(text("INSERT INTO task_preset_points (booth_id, points) VALUES (NULL, 5)"))
    assert _constraint_name(exc) == "uq_preset_global_points"


def test_duplicate_booth_specific_preset_rejected(db: Session, seeded_booth_id: int) -> None:
    db.execute(
        text("INSERT INTO task_preset_points (booth_id, points) VALUES (:b, 15)"),
        {"b": seeded_booth_id},
    )
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text("INSERT INTO task_preset_points (booth_id, points) VALUES (:b, 15)"),
            {"b": seeded_booth_id},
        )
    assert _constraint_name(exc) == "uq_task_preset_points_booth_id_points"


def test_same_points_value_global_and_booth_specific_coexist(
    db: Session, seeded_booth_id: int
) -> None:
    """A global +5 and a booth-specific +5 are DIFFERENT rows under
    the two partial indexes — this must be ALLOWED, proving the two
    partial indexes are independent, not one disguised full unique."""
    db.execute(text("INSERT INTO task_preset_points (booth_id, points) VALUES (NULL, 5)"))
    db.execute(
        text("INSERT INTO task_preset_points (booth_id, points) VALUES (:b, 5)"),
        {"b": seeded_booth_id},
    )  # must not raise


def test_preset_points_out_of_bounds_rejected(db: Session) -> None:
    with pytest.raises(IntegrityError) as exc:
        db.execute(text("INSERT INTO task_preset_points (booth_id, points) VALUES (NULL, 0)"))
    assert _constraint_name(exc) == "ck_task_preset_points_points_range"

    db.rollback()
    with pytest.raises(IntegrityError) as exc2:
        db.execute(text("INSERT INTO task_preset_points (booth_id, points) VALUES (NULL, 101)"))
    assert _constraint_name(exc2) == "ck_task_preset_points_points_range"


# --------------------------------------------------------------------------
# feedback
# --------------------------------------------------------------------------


def test_feedback_rating_out_of_range_rejected(
    db: Session, seeded_user_id: int, seeded_booth_id: int
) -> None:
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text("INSERT INTO feedback (user_id, booth_id, rating) VALUES (:u, :b, 6)"),
            {"u": seeded_user_id, "b": seeded_booth_id},
        )
    assert _constraint_name(exc) == "ck_feedback_rating_range"


def test_multiple_feedback_submissions_for_same_booth_allowed(
    db: Session, seeded_user_id: int, seeded_booth_id: int
) -> None:
    """ARCHITECTURE.md §7.11: feedback itself is deliberately NOT
    unique on (user_id, booth_id) — only the reward is."""
    db.execute(
        text("INSERT INTO feedback (user_id, booth_id, rating) VALUES (:u, :b, 5)"),
        {"u": seeded_user_id, "b": seeded_booth_id},
    )
    db.execute(
        text("INSERT INTO feedback (user_id, booth_id, rating) VALUES (:u, :b, 3)"),
        {"u": seeded_user_id, "b": seeded_booth_id},
    )  # must not raise — two feedback rows for the same (user, booth)


# --------------------------------------------------------------------------
# points_log — the most constraint-dense table
# --------------------------------------------------------------------------


def _insert_task_award(
    db: Session,
    *,
    user_id: int,
    booth_id: int,
    staff_id: int,
    points: int = 15,
    dedupe_key: str | None = None,
) -> int:
    row = db.execute(
        text(
            "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
            "entered_by_staff_id, entered_by_system, dedupe_key) VALUES "
            "(:u, :b, 'TASK', 'AWARD', :points, :staff, false, :dedupe) RETURNING id"
        ),
        {"u": user_id, "b": booth_id, "points": points, "staff": staff_id, "dedupe": dedupe_key},
    ).one()
    return int(row[0])


def test_points_zero_rejected(
    db: Session, seeded_user_id: int, seeded_booth_id: int, seeded_staff_id: int
) -> None:
    """points=0 on an AWARD row violates BOTH ck_points_log_points_nonzero
    (points <> 0) AND ck_points_log_entry_type_consistency (AWARD
    requires points > 0) simultaneously — 0 is neither. points_nonzero
    is consequently redundant given entry_type_consistency (any row
    satisfying the latter already satisfies the former), which is
    harmless defense-in-depth rather than a bug. PostgreSQL's choice of
    which CHECK to report first for a multiply-violating row is not
    contractual, so either name is accepted here — ARCHITECTURE.md
    §7.14 already establishes that dispatch happens by name, not by
    report order, precisely to avoid a test (or a service) depending
    on that ordering."""
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                "entered_by_staff_id, entered_by_system) VALUES "
                "(:u, :b, 'TASK', 'AWARD', 0, :s, false)"
            ),
            {"u": seeded_user_id, "b": seeded_booth_id, "s": seeded_staff_id},
        )
    assert _constraint_name(exc) in {
        "ck_points_log_points_nonzero",
        "ck_points_log_entry_type_consistency",
    }


def test_task_award_with_no_staff_attribution_rejected(
    db: Session, seeded_user_id: int, seeded_booth_id: int
) -> None:
    """ARCHITECTURE.md §7.12: TASK entries require entered_by_staff_id
    NOT NULL and entered_by_system = false. This is exactly the
    "invalid PointsLog attribution" case the negative matrix asks for."""
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                "entered_by_staff_id, entered_by_system) VALUES "
                "(:u, :b, 'TASK', 'AWARD', 15, NULL, false)"
            ),
            {"u": seeded_user_id, "b": seeded_booth_id},
        )
    assert _constraint_name(exc) == "ck_points_log_source_attribution"


def test_task_award_marked_system_generated_rejected(
    db: Session, seeded_user_id: int, seeded_booth_id: int, seeded_staff_id: int
) -> None:
    """A TASK row cannot ALSO claim entered_by_system=true — attribution
    must be unambiguous."""
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                "entered_by_staff_id, entered_by_system) VALUES "
                "(:u, :b, 'TASK', 'AWARD', 15, :s, true)"
            ),
            {"u": seeded_user_id, "b": seeded_booth_id, "s": seeded_staff_id},
        )
    assert _constraint_name(exc) == "ck_points_log_source_attribution"


def test_feedback_award_with_staff_attribution_rejected(
    db: Session, seeded_user_id: int, seeded_booth_id: int, seeded_staff_id: int
) -> None:
    """FEEDBACK entries must be entered_by_system=true and carry NO
    staff attribution — a staff-attributed feedback row is exactly the
    "staff awarding feedback points" shape the architecture forbids."""
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                "entered_by_staff_id, entered_by_system) VALUES "
                "(:u, :b, 'FEEDBACK', 'AWARD', 5, :s, true)"
            ),
            {"u": seeded_user_id, "b": seeded_booth_id, "s": seeded_staff_id},
        )
    assert _constraint_name(exc) == "ck_points_log_source_attribution"


def test_award_with_negative_points_rejected(
    db: Session, seeded_user_id: int, seeded_booth_id: int, seeded_staff_id: int
) -> None:
    """ARCHITECTURE.md §7.12: AWARD rows must have points > 0."""
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                "entered_by_staff_id, entered_by_system) VALUES "
                "(:u, :b, 'TASK', 'AWARD', -15, :s, false)"
            ),
            {"u": seeded_user_id, "b": seeded_booth_id, "s": seeded_staff_id},
        )
    assert _constraint_name(exc) == "ck_points_log_entry_type_consistency"


def test_award_cannot_reference_a_reversal_target(
    db: Session, seeded_user_id: int, seeded_booth_id: int, seeded_staff_id: int
) -> None:
    """A row claiming entry_type='AWARD' cannot ALSO set
    reverses_points_log_id — that combination is a REVERSAL pretending
    to be an ordinary award (the "malformed reversal structure" case)."""
    original_id = _insert_task_award(
        db, user_id=seeded_user_id, booth_id=seeded_booth_id, staff_id=seeded_staff_id
    )
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                "entered_by_staff_id, entered_by_system, reverses_points_log_id) VALUES "
                "(:u, :b, 'TASK', 'AWARD', 15, :s, false, :orig)"
            ),
            {"u": seeded_user_id, "b": seeded_booth_id, "s": seeded_staff_id, "orig": original_id},
        )
    assert _constraint_name(exc) == "ck_points_log_entry_type_consistency"


def test_reversal_without_target_rejected(
    db: Session, seeded_user_id: int, seeded_booth_id: int, seeded_staff_id: int
) -> None:
    """A REVERSAL must reference the row it reverses — a "floating"
    reversal is structurally invalid."""
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                "entered_by_staff_id, entered_by_system) VALUES "
                "(:u, :b, 'TASK', 'REVERSAL', -15, :s, false)"
            ),
            {"u": seeded_user_id, "b": seeded_booth_id, "s": seeded_staff_id},
        )
    assert _constraint_name(exc) == "ck_points_log_entry_type_consistency"


def test_reversal_with_positive_points_rejected(
    db: Session, seeded_user_id: int, seeded_booth_id: int, seeded_staff_id: int
) -> None:
    original_id = _insert_task_award(
        db, user_id=seeded_user_id, booth_id=seeded_booth_id, staff_id=seeded_staff_id
    )
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                "entered_by_staff_id, entered_by_system, reverses_points_log_id) VALUES "
                "(:u, :b, 'TASK', 'REVERSAL', 15, :s, false, :orig)"
            ),
            {"u": seeded_user_id, "b": seeded_booth_id, "s": seeded_staff_id, "orig": original_id},
        )
    assert _constraint_name(exc) == "ck_points_log_entry_type_consistency"


def test_a_row_can_be_reversed_at_most_once(
    db: Session, seeded_user_id: int, seeded_booth_id: int, seeded_staff_id: int
) -> None:
    """ARCHITECTURE.md §7.12 / §7.14: UNIQUE(reverses_points_log_id) —
    a row cannot be reversed twice."""
    original_id = _insert_task_award(
        db, user_id=seeded_user_id, booth_id=seeded_booth_id, staff_id=seeded_staff_id
    )
    db.execute(
        text(
            "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
            "entered_by_staff_id, entered_by_system, reverses_points_log_id) VALUES "
            "(:u, :b, 'TASK', 'REVERSAL', -15, :s, false, :orig)"
        ),
        {"u": seeded_user_id, "b": seeded_booth_id, "s": seeded_staff_id, "orig": original_id},
    )
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                "entered_by_staff_id, entered_by_system, reverses_points_log_id) VALUES "
                "(:u, :b, 'TASK', 'REVERSAL', -15, :s, false, :orig)"
            ),
            {"u": seeded_user_id, "b": seeded_booth_id, "s": seeded_staff_id, "orig": original_id},
        )
    assert _constraint_name(exc) == "uq_points_reverses"


def test_duplicate_task_dedupe_key_rejected(
    db: Session, seeded_user_id: int, seeded_booth_id: int, seeded_staff_id: int
) -> None:
    """ARCHITECTURE.md §14.5: the TASK repeat-policy mechanism —
    DUPLICATE_TASK_REWARD when repeats are disallowed."""
    key = f"TASK:{seeded_user_id}:{seeded_booth_id}"
    _insert_task_award(
        db,
        user_id=seeded_user_id,
        booth_id=seeded_booth_id,
        staff_id=seeded_staff_id,
        dedupe_key=key,
    )
    with pytest.raises(IntegrityError) as exc:
        _insert_task_award(
            db,
            user_id=seeded_user_id,
            booth_id=seeded_booth_id,
            staff_id=seeded_staff_id,
            dedupe_key=key,
        )
    assert _constraint_name(exc) == "uq_points_dedupe_key"


def test_null_dedupe_key_allows_unlimited_awards(
    db: Session, seeded_user_id: int, seeded_booth_id: int, seeded_staff_id: int
) -> None:
    """ARCHITECTURE.md §14.5: NULL dedupe_key (repeats allowed) must
    NOT collide — "NULLs do not collide" is standard SQL, verified
    here directly against real PostgreSQL rather than assumed."""
    _insert_task_award(
        db,
        user_id=seeded_user_id,
        booth_id=seeded_booth_id,
        staff_id=seeded_staff_id,
        dedupe_key=None,
    )
    _insert_task_award(
        db,
        user_id=seeded_user_id,
        booth_id=seeded_booth_id,
        staff_id=seeded_staff_id,
        dedupe_key=None,
    )  # must not raise


def test_duplicate_feedback_reward_rejected(
    db: Session, seeded_user_id: int, seeded_booth_id: int
) -> None:
    """ARCHITECTURE.md §7.12 / §14 invariant #6: structural, not
    string-based — no dedupe_key at all is used for this one."""
    db.execute(
        text(
            "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
            "entered_by_system) VALUES (:u, :b, 'FEEDBACK', 'AWARD', 5, true)"
        ),
        {"u": seeded_user_id, "b": seeded_booth_id},
    )
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                "entered_by_system) VALUES (:u, :b, 'FEEDBACK', 'AWARD', 5, true)"
            ),
            {"u": seeded_user_id, "b": seeded_booth_id},
        )
    assert _constraint_name(exc) == "uq_points_feedback_user_booth"


def test_reversing_a_feedback_award_does_not_free_the_uniqueness_index(
    db: Session, seeded_user_id: int, seeded_booth_id: int
) -> None:
    """Confirms the architecture rule (ARCHITECTURE.md §7.12, corrected
    in Phase 4.1): feedback-reward eligibility is historical/lifetime
    once-per-`(user, booth)` within the current dataset generation, and
    a `REVERSAL` does not restore it.

    The partial unique index is `UNIQUE (user_id, booth_id) WHERE
    source='FEEDBACK' AND entry_type='AWARD'`. Reversing an award does
    not delete or alter the ORIGINAL award row — points_log is
    append-only by design — so that original row keeps
    entry_type='AWARD' forever and keeps occupying the (user_id,
    booth_id) slot in the index regardless of any REVERSAL rows
    referencing it. A PostgreSQL partial index predicate can only
    reference the row's OWN column values (plus immutable
    functions/constants) — it cannot express "AWARD rows that don't
    currently have a REVERSAL referencing them", because that depends
    on the EXISTENCE of a different row, not on this row's own data.
    This is why "reversal frees the slot" was never implementable
    under §7.14's constraint-only enforcement rule (no SELECT-based
    eligibility check before the INSERT), and why the product decision
    is instead: a reversed feedback reward is permanently spent for
    that `(user, booth)` pair until a full demo reset replaces the
    dataset.
    """
    row = db.execute(
        text(
            "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
            "entered_by_system) VALUES (:u, :b, 'FEEDBACK', 'AWARD', 5, true) RETURNING id"
        ),
        {"u": seeded_user_id, "b": seeded_booth_id},
    ).one()
    db.execute(
        text(
            "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
            "entered_by_system, reverses_points_log_id) VALUES "
            "(:u, :b, 'FEEDBACK', 'REVERSAL', -5, true, :orig)"
        ),
        {"u": seeded_user_id, "b": seeded_booth_id, "orig": int(row[0])},
    )
    # The original AWARD row is untouched by the reversal, so it still
    # occupies the partial index slot — a second AWARD attempt still
    # collides, per the corrected §7.12 rule.
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                "entered_by_system) VALUES (:u, :b, 'FEEDBACK', 'AWARD', 5, true)"
            ),
            {"u": seeded_user_id, "b": seeded_booth_id},
        )
    assert _constraint_name(exc) == "uq_points_feedback_user_booth"


def test_task_from_wrong_booth_rejected(
    db: Session,
    seeded_user_id: int,
    seeded_booth_id: int,
    seeded_second_booth_id: int,
    seeded_staff_id: int,
) -> None:
    """ARCHITECTURE.md §7.12: composite FK (task_id, booth_id) ->
    tasks(id, booth_id) — "a row could reference a task belonging to a
    DIFFERENT booth" must be structurally impossible."""
    task_row = db.execute(
        text("INSERT INTO tasks (booth_id, title) VALUES (:b, 'Task at booth 2') RETURNING id"),
        {"b": seeded_second_booth_id},
    ).one()
    task_id = int(task_row[0])

    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
                "entered_by_staff_id, entered_by_system, task_id) VALUES "
                "(:u, :b, 'TASK', 'AWARD', 15, :s, false, :t)"
            ),
            {
                "u": seeded_user_id,
                "b": seeded_booth_id,  # booth 1
                "s": seeded_staff_id,
                "t": task_id,  # task belongs to booth 2
            },
        )
    assert _constraint_name(exc) == "fk_points_task_booth"


def test_task_from_correct_booth_is_allowed(
    db: Session,
    seeded_user_id: int,
    seeded_booth_id: int,
    seeded_staff_id: int,
    seeded_task_id: int,
) -> None:
    db.execute(
        text(
            "INSERT INTO points_log (user_id, booth_id, source, entry_type, points, "
            "entered_by_staff_id, entered_by_system, task_id) VALUES "
            "(:u, :b, 'TASK', 'AWARD', 15, :s, false, :t)"
        ),
        {"u": seeded_user_id, "b": seeded_booth_id, "s": seeded_staff_id, "t": seeded_task_id},
    )  # must not raise


def test_null_task_id_skips_the_composite_fk_entirely(
    db: Session, seeded_user_id: int, seeded_booth_id: int, seeded_staff_id: int
) -> None:
    """ARCHITECTURE.md §13.1: the staff flow awards without selecting a
    task — task_id NULL must be allowed regardless of booth_id."""
    _insert_task_award(
        db, user_id=seeded_user_id, booth_id=seeded_booth_id, staff_id=seeded_staff_id
    )


# --------------------------------------------------------------------------
# idempotency_records — staff-scoped ownership
# --------------------------------------------------------------------------


def test_duplicate_idempotency_key_for_same_staff_rejected(
    db: Session, seeded_staff_id: int
) -> None:
    key = uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO idempotency_records "
            "(staff_account_id, idempotency_key, request_fingerprint) "
            "VALUES (:s, :k, 'fp-1')"
        ),
        {"s": seeded_staff_id, "k": key},
    )
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO idempotency_records "
                "(staff_account_id, idempotency_key, request_fingerprint) "
                "VALUES (:s, :k, 'fp-2')"
            ),
            {"s": seeded_staff_id, "k": key},
        )
    assert _constraint_name(exc) == "pk_idempotency_records"


def test_same_idempotency_key_allowed_for_different_staff(
    db: Session, seeded_staff_id: int, seeded_booth_id: int
) -> None:
    """ARCHITECTURE.md §7.12.1: "the key is scoped to its issuer" — a
    UUID collision across two DIFFERENT staff accounts is not blocked,
    because the primary key is the (staff_account_id, idempotency_key)
    pair, not the key alone."""
    other_staff_row = db.execute(
        text(
            "INSERT INTO staff_accounts (email, password_hash, full_name, role, booth_id) "
            "VALUES ('staff2@example.com', 'x', 'Staff Two', 'BOOTH_STAFF', :b) RETURNING id"
        ),
        {"b": seeded_booth_id},
    ).one()
    other_staff_id = int(other_staff_row[0])

    key = uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO idempotency_records "
            "(staff_account_id, idempotency_key, request_fingerprint) "
            "VALUES (:s, :k, 'fp-1')"
        ),
        {"s": seeded_staff_id, "k": key},
    )
    db.execute(
        text(
            "INSERT INTO idempotency_records "
            "(staff_account_id, idempotency_key, request_fingerprint) "
            "VALUES (:s, :k, 'fp-1')"
        ),
        {"s": other_staff_id, "k": key},
    )  # must not raise — different staff, same key value


# --------------------------------------------------------------------------
# event_config — the guarded singleton
# --------------------------------------------------------------------------


def test_event_config_second_row_rejected(db: Session) -> None:
    db.execute(text("INSERT INTO event_config (id) VALUES (1)"))
    with pytest.raises(IntegrityError) as exc:
        db.execute(text("INSERT INTO event_config (id) VALUES (2)"))
    assert _constraint_name(exc) == "ck_event_config_singleton"


def test_event_config_feedback_points_out_of_bounds_rejected(db: Session) -> None:
    with pytest.raises(IntegrityError) as exc:
        db.execute(text("INSERT INTO event_config (id, feedback_points) VALUES (1, 0)"))
    assert _constraint_name(exc) == "ck_event_config_feedback_points_range"


def test_event_config_leaderboard_top_n_out_of_bounds_rejected(db: Session) -> None:
    with pytest.raises(IntegrityError) as exc:
        db.execute(text("INSERT INTO event_config (id, leaderboard_top_n) VALUES (1, 0)"))
    assert _constraint_name(exc) == "ck_event_config_leaderboard_top_n_range"


# --------------------------------------------------------------------------
# otp_challenges — at most one live challenge per (email, purpose)
# --------------------------------------------------------------------------


def test_duplicate_live_otp_challenge_rejected(db: Session) -> None:
    db.execute(
        text(
            "INSERT INTO otp_challenges (email, purpose, code_hash, expires_at) VALUES "
            "('otp@example.com', 'ACTIVATION', 'h1', now() + interval '10 minutes')"
        )
    )
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO otp_challenges (email, purpose, code_hash, expires_at) VALUES "
                "('otp@example.com', 'ACTIVATION', 'h2', now() + interval '10 minutes')"
            )
        )
    assert _constraint_name(exc) == "uq_otp_challenges_email_purpose_live"


def test_different_purpose_does_not_collide(db: Session) -> None:
    """ARCHITECTURE.md §8.2: "purpose exists so an activation code
    cannot be replayed as a login code" — the flip side is that a live
    ACTIVATION and a live LOGIN challenge for the same email may
    coexist."""
    db.execute(
        text(
            "INSERT INTO otp_challenges (email, purpose, code_hash, expires_at) VALUES "
            "('otp2@example.com', 'ACTIVATION', 'h1', now() + interval '10 minutes')"
        )
    )
    db.execute(
        text(
            "INSERT INTO otp_challenges (email, purpose, code_hash, expires_at) VALUES "
            "('otp2@example.com', 'LOGIN', 'h2', now() + interval '10 minutes')"
        )
    )  # must not raise


def test_consumed_challenge_does_not_block_a_new_one(db: Session) -> None:
    db.execute(
        text(
            "INSERT INTO otp_challenges "
            "(email, purpose, code_hash, expires_at, consumed_at) VALUES "
            "('otp3@example.com', 'ACTIVATION', 'h1', now() + interval '10 minutes', now())"
        )
    )
    db.execute(
        text(
            "INSERT INTO otp_challenges (email, purpose, code_hash, expires_at) VALUES "
            "('otp3@example.com', 'ACTIVATION', 'h2', now() + interval '10 minutes')"
        )
    )  # must not raise — the first one is already consumed


# --------------------------------------------------------------------------
# FK protection — referenced rows cannot vanish out from under history
# --------------------------------------------------------------------------


def test_deleting_a_referenced_booth_is_rejected(
    db: Session, seeded_booth_id: int, seeded_task_id: int
) -> None:
    """No ON DELETE CASCADE is declared for booths -> tasks
    (deliberately — see app/modules/booths/models.py docstring intent
    of not cascading away ledger-adjacent history). The default
    RESTRICT/NO ACTION must reject this."""
    with pytest.raises(IntegrityError) as exc:
        db.execute(text("DELETE FROM booths WHERE id = :b"), {"b": seeded_booth_id})
    assert _constraint_name(exc) == "fk_tasks_booth_id_booths"


def test_deleting_a_user_cascades_their_interests(
    db: Session, seeded_user_id: int, seeded_interest_id: int
) -> None:
    """The ONE explicit ON DELETE CASCADE in the schema
    (user_interests.user_id) works as declared."""
    db.execute(
        text("INSERT INTO user_interests (user_id, interest_id) VALUES (:u, :i)"),
        {"u": seeded_user_id, "i": seeded_interest_id},
    )
    db.execute(text("DELETE FROM users WHERE id = :u"), {"u": seeded_user_id})
    remaining = db.execute(
        text("SELECT count(*) FROM user_interests WHERE user_id = :u"), {"u": seeded_user_id}
    ).scalar_one()
    assert remaining == 0
