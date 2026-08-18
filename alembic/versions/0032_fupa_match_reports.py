"""add tenant-isolated FuPa match report workflow

Revision ID: 0032
Revises: 0031
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    ]


def upgrade() -> None:
    op.add_column("teams", sa.Column("fupa_url", sa.String(length=1000), nullable=True))
    op.add_column("games", sa.Column("fupa_url", sa.String(length=1000), nullable=True))

    op.create_table(
        "fupa_match_snapshots",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("club_id", sa.String(length=36), nullable=False),
        sa.Column("game_id", sa.String(length=36), nullable=False),
        sa.Column("source_url", sa.String(length=1000), nullable=False),
        sa.Column("fetch_status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("structured_data", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("ticker_data", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("source_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_error_category", sa.String(length=80), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["game_id", "club_id"], ["games.id", "games.club_id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("club_id", "game_id", "content_digest", name="uq_fupa_snapshot_digest"),
        sa.CheckConstraint(
            "fetch_status IN ('pending','success','not_found','incomplete','failed')",
            name="ck_fupa_snapshot_fetch_status",
        ),
    )
    op.create_index("ix_fupa_match_snapshots_club_id", "fupa_match_snapshots", ["club_id"])
    op.create_index("ix_fupa_match_snapshots_game_id", "fupa_match_snapshots", ["game_id"])
    op.create_index(
        "ix_fupa_match_snapshots_content_digest", "fupa_match_snapshots", ["content_digest"]
    )
    op.create_index("ix_fupa_match_snapshots_fetched_at", "fupa_match_snapshots", ["fetched_at"])
    op.create_index(
        "ix_fupa_match_snapshots_next_check_at", "fupa_match_snapshots", ["next_check_at"]
    )

    op.create_table(
        "match_feedback_contacts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("club_id", sa.String(length=36), nullable=False),
        sa.Column("team_id", sa.String(length=36), nullable=False),
        sa.Column("recipient_id", sa.String(length=36), nullable=False),
        sa.Column("normalized_phone", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("role_label", sa.String(length=120), nullable=True),
        sa.Column("request_match_reports", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        *_timestamps(),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["team_id", "club_id"], ["teams.id", "teams.club_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id", "club_id"],
            ["whatsapp_recipients.id", "whatsapp_recipients.club_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "club_id", name="uq_match_feedback_contacts_id_club"),
        sa.UniqueConstraint(
            "club_id", "team_id", "normalized_phone", name="uq_match_feedback_contact_phone"
        ),
    )
    op.create_index("ix_match_feedback_contacts_club_id", "match_feedback_contacts", ["club_id"])
    op.create_index("ix_match_feedback_contacts_team_id", "match_feedback_contacts", ["team_id"])
    op.create_index(
        "ix_match_feedback_contacts_recipient_id", "match_feedback_contacts", ["recipient_id"]
    )
    op.create_index("ix_match_feedback_contacts_active", "match_feedback_contacts", ["active"])

    op.create_table(
        "match_feedback_requests",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("club_id", sa.String(length=36), nullable=False),
        sa.Column("game_id", sa.String(length=36), nullable=False),
        sa.Column("team_id", sa.String(length=36), nullable=False),
        sa.Column("contact_id", sa.String(length=36), nullable=False),
        sa.Column("channel_connection_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("template_id", sa.String(length=36), nullable=True),
        sa.Column("provider_message_id", sa.String(length=200), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["game_id", "club_id"], ["games.id", "games.club_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "club_id"], ["teams.id", "teams.club_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["contact_id", "club_id"],
            ["match_feedback_contacts.id", "match_feedback_contacts.club_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["template_id", "club_id"],
            ["whatsapp_message_templates.id", "whatsapp_message_templates.club_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "club_id", name="uq_match_feedback_requests_id_club"),
        sa.UniqueConstraint("club_id", "idempotency_key", name="uq_match_feedback_request_key"),
        sa.CheckConstraint(
            "status IN ('pending','sent','answered','expired','failed','cancelled')",
            name="ck_match_feedback_request_status",
        ),
    )
    for column in ("club_id", "game_id", "team_id", "contact_id", "channel_connection_id", "status", "provider_message_id", "deadline_at"):
        op.create_index(f"ix_match_feedback_requests_{column}", "match_feedback_requests", [column])

    op.create_table(
        "match_feedback_responses",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("club_id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("provider_message_id", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["request_id", "club_id"],
            ["match_feedback_requests.id", "match_feedback_requests.club_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "club_id", "provider_message_id", name="uq_match_feedback_response_msg"
        ),
    )
    op.create_index("ix_match_feedback_responses_club_id", "match_feedback_responses", ["club_id"])
    op.create_index("ix_match_feedback_responses_request_id", "match_feedback_responses", ["request_id"])

    op.create_table(
        "club_writing_examples",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("club_id", sa.String(length=36), nullable=False),
        sa.Column("team_id", sa.String(length=36), nullable=True),
        sa.Column("category", sa.String(length=30), nullable=False, server_default="general"),
        sa.Column("title", sa.String(length=240), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["team_id", "club_id"], ["teams.id", "teams.club_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("id", "club_id", name="uq_club_writing_examples_id_club"),
        sa.CheckConstraint(
            "category IN ('general','win','loss','draw','derby','cup','friendly')",
            name="ck_club_writing_example_category",
        ),
    )
    for column in ("club_id", "team_id", "category", "active"):
        op.create_index(f"ix_club_writing_examples_{column}", "club_writing_examples", [column])

    op.create_table(
        "match_manual_notes",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("club_id", sa.String(length=36), nullable=False),
        sa.Column("game_id", sa.String(length=36), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("confirmed_facts", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["game_id", "club_id"], ["games.id", "games.club_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_match_manual_notes_club_id", "match_manual_notes", ["club_id"])
    op.create_index("ix_match_manual_notes_game_id", "match_manual_notes", ["game_id"])

    op.create_table(
        "match_reports",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("club_id", sa.String(length=36), nullable=False),
        sa.Column("team_id", sa.String(length=36), nullable=False),
        sa.Column("game_id", sa.String(length=36), nullable=False),
        sa.Column("report_type", sa.String(length=30), nullable=False, server_default="match_report"),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="waiting_for_sources"),
        sa.Column("desired_length", sa.String(length=20), nullable=False, server_default="medium"),
        sa.Column("source_summary", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("source_conflicts", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("generation_settings", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("generation_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_version_number", sa.Integer(), nullable=True),
        sa.Column("automatic_publish_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("approved_by", sa.String(length=36), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_category", sa.String(length=80), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["game_id", "club_id"], ["games.id", "games.club_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "club_id"], ["teams.id", "teams.club_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("id", "club_id", name="uq_match_reports_id_club"),
        sa.UniqueConstraint("club_id", "game_id", "report_type", name="uq_match_report_game_type"),
        sa.CheckConstraint(
            "status IN ('waiting_for_sources','waiting_for_feedback','ready_to_generate','conflict_requires_review','generating','draft','review_required','approved','publishing','published','failed','cancelled')",
            name="ck_match_report_status",
        ),
    )
    for column in ("club_id", "team_id", "game_id", "status", "generation_due_at"):
        op.create_index(f"ix_match_reports_{column}", "match_reports", [column])

    op.create_table(
        "match_report_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("club_id", sa.String(length=36), nullable=False),
        sa.Column("report_id", sa.String(length=36), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(length=30), nullable=False, server_default="generated"),
        sa.Column("headline", sa.String(length=300), nullable=False),
        sa.Column("teaser", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("used_sources", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("omitted_sources", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("source_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("prompt_template_id", sa.String(length=100), nullable=True),
        sa.Column("prompt_version", sa.Integer(), nullable=True),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["report_id", "club_id"], ["match_reports.id", "match_reports.club_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("id", "club_id", name="uq_match_report_versions_id_club"),
        sa.UniqueConstraint("club_id", "report_id", "version_number", name="uq_match_report_version"),
    )
    op.create_index("ix_match_report_versions_club_id", "match_report_versions", ["club_id"])
    op.create_index("ix_match_report_versions_report_id", "match_report_versions", ["report_id"])

    op.create_table(
        "match_report_publications",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("club_id", sa.String(length=36), nullable=False),
        sa.Column("report_id", sa.String(length=36), nullable=False),
        sa.Column("version_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False, server_default="fupa"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=True),
        sa.Column("external_url", sa.String(length=1000), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_category", sa.String(length=80), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["report_id", "club_id"], ["match_reports.id", "match_reports.club_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["version_id", "club_id"],
            ["match_report_versions.id", "match_report_versions.club_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("club_id", "idempotency_key", name="uq_match_report_publication_key"),
        sa.CheckConstraint(
            "status IN ('pending','publishing','published','manual_required','retry','failed','cancelled')",
            name="ck_match_report_publication_status",
        ),
    )
    for column in ("club_id", "report_id", "version_id", "status", "next_attempt_at"):
        op.create_index(f"ix_match_report_publications_{column}", "match_report_publications", [column])


def downgrade() -> None:
    op.drop_table("match_report_publications")
    op.drop_table("match_report_versions")
    op.drop_table("match_reports")
    op.drop_table("match_manual_notes")
    op.drop_table("club_writing_examples")
    op.drop_table("match_feedback_responses")
    op.drop_table("match_feedback_requests")
    op.drop_table("match_feedback_contacts")
    op.drop_table("fupa_match_snapshots")
    op.drop_column("games", "fupa_url")
    op.drop_column("teams", "fupa_url")
