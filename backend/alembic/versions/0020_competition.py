"""Beyond Now competition foundation: verified participation evidence and scopes

Revision ID: 0020_competition
Revises: 0019_playground
Create Date: 2026-10-16

Additive. No row is rewritten and no point is created for anyone:

* `points_source` gains the eight competition categories and
  `ck_points_log_source_attribution` gains one more allowed form for them (every
  older row keeps its meaning). Points stay in `points_log` only.
* `staff_role` gains VOLUNTEER (no booth, no powers of its own) and
  `ck_staff_role_booth` allows it.
* `event_config.competition_mode` (default false: the ranking is unchanged until
  an administrator activates the competition after the dry-run report).
* `users.competition_excluded_reason` (NULL = eligible; administrator-only).
* `masterclass_events.competition_closes_at` (NULL until scheduled).
* `masterclasses.standing_capacity` (default 0: no standing).
* `masterclass_reservations.cancel_reason` may also be NO_SHOW.
* New tables: `staff_scopes`, `panels`, `panel_scans`, `masterclass_attendance`.

Downgrade drops exactly these tables and columns and restores the previous
CHECKs. It REFUSES (the CHECK restore fails) while competition rows or VOLUNTEER
accounts exist - deliberately, so no history is silently lost. PostgreSQL cannot
remove enum values; the added values stay harmlessly unused after a downgrade.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "0020_competition"
down_revision: str | None = "0019_playground"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_SOURCES = (
    "PROFILE",
    "PANEL",
    "QMETER",
    "MASTERCLASS",
    "PLAYGROUND",
    "CONNECTION",
    "MENTOR",
    "FOUNDER",
)
_OLD_ATTRIBUTION = (
    "(source = 'TASK' AND booth_id IS NOT NULL "
    "AND entered_by_staff_id IS NOT NULL AND entered_by_system = false) OR "
    "(source = 'FEEDBACK' AND booth_id IS NOT NULL "
    "AND entered_by_staff_id IS NULL AND entered_by_system = true) OR "
    "(source = 'FEEDBACK' AND entry_type = 'REVERSAL' AND booth_id IS NOT NULL "
    "AND entered_by_staff_id IS NOT NULL AND entered_by_system = false)"
)
_NEW_ATTRIBUTION = (
    _OLD_ATTRIBUTION + " OR (source IN ('PROFILE', 'PANEL', 'QMETER', 'MASTERCLASS', 'PLAYGROUND', "
    "'CONNECTION', 'MENTOR', 'FOUNDER') "
    "AND (entered_by_staff_id IS NULL) = entered_by_system)"
)
_OLD_ROLE = (
    "(role = 'BOOTH_STAFF' AND booth_id IS NOT NULL) OR (role = 'ADMIN' AND booth_id IS NULL)"
)
_NEW_ROLE = _OLD_ROLE + " OR (role = 'VOLUNTEER' AND booth_id IS NULL)"
_OLD_REASON = "cancel_reason IS NULL OR cancel_reason IN ('CANCELLED_BY_ATTENDEE', 'REPLACED')"
_NEW_REASON = (
    "cancel_reason IS NULL OR cancel_reason IN ('CANCELLED_BY_ATTENDEE', 'REPLACED', 'NO_SHOW')"
)


def _id() -> sa.Column[Any]:
    return sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False)


def upgrade() -> None:
    # A new enum value cannot be used in the transaction that adds it, and the
    # CHECK below names them: add them in their own committed block first.
    with op.get_context().autocommit_block():
        for value in _SOURCES:
            op.execute(f"ALTER TYPE points_source ADD VALUE IF NOT EXISTS '{value}'")
        op.execute("ALTER TYPE staff_role ADD VALUE IF NOT EXISTS 'VOLUNTEER'")

    op.drop_constraint(op.f("ck_points_log_source_attribution"), "points_log", type_="check")
    op.create_check_constraint(
        op.f("ck_points_log_source_attribution"), "points_log", _NEW_ATTRIBUTION
    )
    op.drop_constraint(op.f("ck_staff_role_booth"), "staff_accounts", type_="check")
    op.create_check_constraint(op.f("ck_staff_role_booth"), "staff_accounts", _NEW_ROLE)
    op.drop_constraint(
        op.f("ck_masterclass_reservations_cancel_reason_known"),
        "masterclass_reservations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_masterclass_reservations_cancel_reason_known"),
        "masterclass_reservations",
        _NEW_REASON,
    )

    op.add_column(
        "event_config",
        sa.Column("competition_mode", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column("users", sa.Column("competition_excluded_reason", sa.Text(), nullable=True))
    op.add_column(
        "masterclass_events",
        sa.Column("competition_closes_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "masterclasses",
        sa.Column("standing_capacity", sa.Integer(), server_default="0", nullable=False),
    )

    op.create_table(
        "staff_scopes",
        sa.Column("staff_account_id", sa.BigInteger(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("granted_by_staff_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        _id(),
        sa.CheckConstraint(
            "scope IN ('MASTERCLASS_DOOR', 'GAME_MASTER', 'MENTOR_HOST')",
            name=op.f("ck_staff_scopes_scope_known"),
        ),
        sa.CheckConstraint(
            "(scope = 'GAME_MASTER') = (target_id IS NULL)",
            name=op.f("ck_staff_scopes_target_matches_scope"),
        ),
        sa.ForeignKeyConstraint(
            ["staff_account_id"], ["staff_accounts.id"], name="fk_staff_scopes_staff_account_id"
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_staff_id"], ["staff_accounts.id"], name="fk_staff_scopes_granted_by"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_scopes")),
    )
    op.create_index(
        "uq_staff_scopes_live_target",
        "staff_scopes",
        ["staff_account_id", "scope", "target_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL AND target_id IS NOT NULL"),
    )
    op.create_index(
        "uq_staff_scopes_live_event",
        "staff_scopes",
        ["staff_account_id", "scope"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL AND target_id IS NULL"),
    )

    op.create_table(
        "panels",
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("qr_version", sa.Integer(), server_default="1", nullable=False),
        _id(),
        sa.CheckConstraint("ends_at > starts_at", name=op.f("ck_panels_ends_after_start")),
        sa.CheckConstraint("qr_version >= 1", name=op.f("ck_panels_qr_version_positive")),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 200", name=op.f("ck_panels_title_length")
        ),
        sa.ForeignKeyConstraint(["event_id"], ["masterclass_events.id"], name="fk_panels_event_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_panels")),
        sa.UniqueConstraint("event_id", "code", name="uq_panels_event_code"),
    )
    op.create_table(
        "panel_scans",
        sa.Column("panel_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=False),
        _id(),
        sa.CheckConstraint("phase IN ('ENTRY', 'EXIT')", name=op.f("ck_panel_scans_phase_known")),
        sa.ForeignKeyConstraint(["panel_id"], ["panels.id"], name="fk_panel_scans_panel_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_panel_scans_user_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_panel_scans")),
        sa.UniqueConstraint("panel_id", "user_id", "phase", name="uq_panel_scans_once"),
    )
    op.create_table(
        "masterclass_attendance",
        sa.Column("masterclass_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("standing", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("entry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_by_staff_id", sa.BigInteger(), nullable=False),
        sa.Column("exit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_by_staff_id", sa.BigInteger(), nullable=True),
        _id(),
        sa.CheckConstraint(
            "(exit_at IS NULL) = (exit_by_staff_id IS NULL)",
            name=op.f("ck_masterclass_attendance_exit_complete"),
        ),
        sa.CheckConstraint(
            "exit_at IS NULL OR exit_at >= entry_at",
            name=op.f("ck_masterclass_attendance_exit_after_entry"),
        ),
        sa.ForeignKeyConstraint(
            ["masterclass_id"],
            ["masterclasses.id"],
            name="fk_masterclass_attendance_masterclass_id",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_masterclass_attendance_user_id"
        ),
        sa.ForeignKeyConstraint(
            ["entry_by_staff_id"], ["staff_accounts.id"], name="fk_masterclass_attendance_entry_by"
        ),
        sa.ForeignKeyConstraint(
            ["exit_by_staff_id"], ["staff_accounts.id"], name="fk_masterclass_attendance_exit_by"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_masterclass_attendance")),
        sa.UniqueConstraint("masterclass_id", "user_id", name="uq_masterclass_attendance_once"),
    )


def downgrade() -> None:
    op.drop_table("masterclass_attendance")
    op.drop_table("panel_scans")
    op.drop_table("panels")
    op.drop_index("uq_staff_scopes_live_event", table_name="staff_scopes")
    op.drop_index("uq_staff_scopes_live_target", table_name="staff_scopes")
    op.drop_table("staff_scopes")
    op.drop_column("masterclasses", "standing_capacity")
    op.drop_column("masterclass_events", "competition_closes_at")
    op.drop_column("users", "competition_excluded_reason")
    op.drop_column("event_config", "competition_mode")
    op.drop_constraint(
        op.f("ck_masterclass_reservations_cancel_reason_known"),
        "masterclass_reservations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_masterclass_reservations_cancel_reason_known"),
        "masterclass_reservations",
        _OLD_REASON,
    )
    op.drop_constraint(op.f("ck_staff_role_booth"), "staff_accounts", type_="check")
    op.create_check_constraint(op.f("ck_staff_role_booth"), "staff_accounts", _OLD_ROLE)
    op.drop_constraint(op.f("ck_points_log_source_attribution"), "points_log", type_="check")
    op.create_check_constraint(
        op.f("ck_points_log_source_attribution"), "points_log", _OLD_ATTRIBUTION
    )
