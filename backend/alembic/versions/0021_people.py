"""People: compatibility meetings, Match Corner tables, blocks, connections

Revision ID: 0021_people
Revises: 0020_competition
Create Date: 2026-10-17

Additive: seven new tables, no existing row or constraint touched, nothing
inserted (no table, meeting or connection is invented). Database invariants:
one open request per pair (`uq_meeting_requests_open_pair`), a table never
double-booked (`uq_meetings_table_slot`), a person never in two confirmed
meetings at once (`uq_meeting_participants_person_slot`), a mutual scan once per
direction and a connection once per pair. Downgrade drops exactly these tables.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0021_people"
down_revision: str | None = "0020_competition"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _id() -> sa.Column[Any]:
    return sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False)


def _user_fk(column: str, name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], ["users.id"], name=name)


def upgrade() -> None:
    op.create_table(
        "user_blocks",
        sa.Column("blocker_id", sa.BigInteger(), nullable=False),
        sa.Column("blocked_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _id(),
        sa.CheckConstraint("blocker_id <> blocked_id", name=op.f("ck_user_blocks_not_self")),
        _user_fk("blocker_id", "fk_user_blocks_blocker_id"),
        _user_fk("blocked_id", "fk_user_blocks_blocked_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_blocks")),
        sa.UniqueConstraint("blocker_id", "blocked_id", name="uq_user_blocks_pair"),
    )
    op.create_table(
        "meeting_requests",
        sa.Column("requester_id", sa.BigInteger(), nullable=False),
        sa.Column("recipient_id", sa.BigInteger(), nullable=False),
        sa.Column("pair_low_id", sa.BigInteger(), nullable=False),
        sa.Column("pair_high_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), server_default="PENDING", nullable=False),
        sa.Column("command_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        _id(),
        sa.CheckConstraint(
            "requester_id <> recipient_id", name=op.f("ck_meeting_requests_not_self")
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'ACCEPTED', 'DECLINED', 'CANCELLED')",
            name=op.f("ck_meeting_requests_status_known"),
        ),
        sa.CheckConstraint(
            "(status = 'PENDING') = (responded_at IS NULL)",
            name=op.f("ck_meeting_requests_responded_unless_pending"),
        ),
        sa.CheckConstraint(
            "pair_low_id = LEAST(requester_id, recipient_id) "
            "AND pair_high_id = GREATEST(requester_id, recipient_id)",
            name=op.f("ck_meeting_requests_pair_matches"),
        ),
        _user_fk("requester_id", "fk_meeting_requests_requester_id"),
        _user_fk("recipient_id", "fk_meeting_requests_recipient_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_meeting_requests")),
        sa.UniqueConstraint("command_id", name=op.f("uq_meeting_requests_command_id")),
    )
    op.create_index(
        "uq_meeting_requests_open_pair",
        "meeting_requests",
        ["pair_low_id", "pair_high_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'ACCEPTED')"),
    )
    op.create_index(
        "ix_meeting_requests_requester_created", "meeting_requests", ["requester_id", "created_at"]
    )
    op.create_index(
        "ix_meeting_requests_recipient_status", "meeting_requests", ["recipient_id", "status"]
    )
    op.create_table(
        "match_tables",
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        _id(),
        sa.CheckConstraint(
            "char_length(label) BETWEEN 1 AND 40", name=op.f("ck_match_tables_label_length")
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["masterclass_events.id"], name="fk_match_tables_event_id"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_match_tables")),
        sa.UniqueConstraint("event_id", "label", name="uq_match_tables_event_label"),
    )
    op.create_table(
        "meetings",
        sa.Column("request_id", sa.BigInteger(), nullable=False),
        sa.Column("table_id", sa.BigInteger(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prompts", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        _id(),
        sa.CheckConstraint(
            "ends_at = starts_at + interval '15 minutes'", name=op.f("ck_meetings_fifteen_minutes")
        ),
        sa.CheckConstraint(
            "cardinality(prompts) BETWEEN 2 AND 3", name=op.f("ck_meetings_two_or_three_prompts")
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["meeting_requests.id"], name="fk_meetings_request_id"
        ),
        sa.ForeignKeyConstraint(["table_id"], ["match_tables.id"], name="fk_meetings_table_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_meetings")),
        sa.UniqueConstraint("request_id", name=op.f("uq_meetings_request_id")),
    )
    op.create_index(
        "uq_meetings_table_slot",
        "meetings",
        ["table_id", "starts_at"],
        unique=True,
        postgresql_where=sa.text("cancelled_at IS NULL"),
    )
    op.create_table(
        "meeting_participants",
        sa.Column("meeting_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        _id(),
        sa.ForeignKeyConstraint(
            ["meeting_id"], ["meetings.id"], name="fk_meeting_participants_meeting_id"
        ),
        _user_fk("user_id", "fk_meeting_participants_user_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_meeting_participants")),
        sa.UniqueConstraint("meeting_id", "user_id", name="uq_meeting_participants_member"),
    )
    op.create_index(
        "uq_meeting_participants_person_slot",
        "meeting_participants",
        ["user_id", "starts_at"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_table(
        "connection_scans",
        sa.Column("scanner_id", sa.BigInteger(), nullable=False),
        sa.Column("scanned_id", sa.BigInteger(), nullable=False),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=False),
        _id(),
        sa.CheckConstraint("scanner_id <> scanned_id", name=op.f("ck_connection_scans_not_self")),
        _user_fk("scanner_id", "fk_connection_scans_scanner_id"),
        _user_fk("scanned_id", "fk_connection_scans_scanned_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connection_scans")),
        sa.UniqueConstraint("scanner_id", "scanned_id", name="uq_connection_scans_direction"),
    )
    op.create_table(
        "connections",
        sa.Column("user_low_id", sa.BigInteger(), nullable=False),
        sa.Column("user_high_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("same_company", sa.Boolean(), nullable=False),
        sa.Column("established_at", sa.DateTime(timezone=True), nullable=False),
        _id(),
        sa.CheckConstraint("user_low_id < user_high_id", name=op.f("ck_connections_ordered_pair")),
        sa.CheckConstraint("kind IN ('MATCH', 'OTHER')", name=op.f("ck_connections_kind_known")),
        _user_fk("user_low_id", "fk_connections_user_low_id"),
        _user_fk("user_high_id", "fk_connections_user_high_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connections")),
        sa.UniqueConstraint("user_low_id", "user_high_id", name="uq_connections_pair"),
    )


def downgrade() -> None:
    op.drop_table("connections")
    op.drop_table("connection_scans")
    op.drop_index("uq_meeting_participants_person_slot", table_name="meeting_participants")
    op.drop_table("meeting_participants")
    op.drop_index("uq_meetings_table_slot", table_name="meetings")
    op.drop_table("meetings")
    op.drop_table("match_tables")
    op.drop_index("ix_meeting_requests_recipient_status", table_name="meeting_requests")
    op.drop_index("ix_meeting_requests_requester_created", table_name="meeting_requests")
    op.drop_index("uq_meeting_requests_open_pair", table_name="meeting_requests")
    op.drop_table("meeting_requests")
    op.drop_table("user_blocks")
