"""tenant-safe live center and match event workflow

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    ]


def upgrade() -> None:
    expected_tables = {
        "live_reporters",
        "live_reporter_teams",
        "live_game_states",
        "match_events",
        "live_event_rules",
        "live_event_deliveries",
        "live_delivery_attempts",
        "whatsapp_audiences",
        "whatsapp_audience_recipients",
    }
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    already_present = expected_tables & existing_tables
    if already_present == expected_tables:
        # Migration 0001 intentionally creates the current canonical metadata on a
        # completely fresh installation. Later additive migrations therefore have
        # to tolerate their complete target schema already being present.
        return
    if already_present:
        missing = ", ".join(sorted(expected_tables - existing_tables))
        present = ", ".join(sorted(already_present))
        raise RuntimeError(
            "Unvollständiges Live-Center-Schema; Migration wird sicher abgebrochen. "
            f"Vorhanden: {present}. Fehlend: {missing}."
        )
    op.create_table(
        "whatsapp_audiences",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("channel_connection_id", sa.String(36), nullable=False, index=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("audience_type", sa.String(30), nullable=False, index=True),
        sa.Column("external_group_id", sa.String(200), nullable=True, index=True),
        sa.Column("eligibility_status", sa.String(30), nullable=False, server_default="unknown"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "audience_type IN ('group','recipient_list')",
            name="ck_whatsapp_audience_type",
        ),
        sa.CheckConstraint(
            "eligibility_status IN ('available','not_available','unknown','connection_error')",
            name="ck_whatsapp_audience_eligibility",
        ),
        sa.UniqueConstraint("id", "club_id", name="uq_whatsapp_audiences_id_club"),
        sa.UniqueConstraint(
            "club_id",
            "channel_connection_id",
            "name",
            name="uq_whatsapp_audience_name",
        ),
    )
    op.create_table(
        "whatsapp_audience_recipients",
        sa.Column("audience_id", sa.String(36), primary_key=True),
        sa.Column("recipient_id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["audience_id", "club_id"],
            ["whatsapp_audiences.id", "whatsapp_audiences.club_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id", "club_id"],
            ["whatsapp_recipients.id", "whatsapp_recipients.club_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "live_reporters",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("user_id", sa.String(36), nullable=True, index=True),
        sa.Column("channel_connection_id", sa.String(36), nullable=True, index=True),
        sa.Column("normalized_phone", sa.String(32), nullable=True, index=True),
        sa.Column("display_name", sa.String(160), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("all_teams", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("trusted_auto_confirm", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("may_correct", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("allowed_event_types", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("active_game_id", sa.String(36), nullable=True, index=True),
        sa.Column("active_game_expires_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["user_id", "club_id"], ["users.id", "users.club_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["active_game_id", "club_id"],
            ["games.id", "games.club_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "user_id IS NOT NULL OR normalized_phone IS NOT NULL",
            name="ck_live_reporter_identity",
        ),
        sa.UniqueConstraint("id", "club_id", name="uq_live_reporters_id_club"),
        sa.UniqueConstraint(
            "club_id",
            "channel_connection_id",
            "normalized_phone",
            name="uq_live_reporter_phone",
        ),
    )
    op.create_table(
        "live_reporter_teams",
        sa.Column("reporter_id", sa.String(36), primary_key=True),
        sa.Column("team_id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["reporter_id", "club_id"],
            ["live_reporters.id", "live_reporters.club_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "club_id"], ["teams.id", "teams.club_id"], ondelete="CASCADE"
        ),
    )
    op.create_table(
        "live_game_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("game_id", sa.String(36), nullable=False, index=True),
        sa.Column("team_id", sa.String(36), nullable=False, index=True),
        sa.Column("phase", sa.String(30), nullable=False, server_default="scheduled", index=True),
        sa.Column("home_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("away_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("minute", sa.Integer(), nullable=True),
        sa.Column("stoppage_minute", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(40), nullable=False, server_default="dashboard"),
        sa.Column("last_event_id", sa.String(36), nullable=True, index=True),
        sa.Column("last_event_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "live_publishing_paused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            index=True,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["game_id", "club_id"], ["games.id", "games.club_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "club_id"], ["teams.id", "teams.club_id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("home_score >= 0 AND away_score >= 0", name="ck_live_score_nonnegative"),
        sa.CheckConstraint(
            "phase IN ('scheduled','first_half','halftime','second_half','interrupted','finished','abandoned')",
            name="ck_live_game_phase",
        ),
        sa.UniqueConstraint("game_id", name="uq_live_game_state_game"),
        sa.UniqueConstraint("id", "club_id", name="uq_live_game_states_id_club"),
    )
    op.create_table(
        "match_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("game_id", sa.String(36), nullable=False, index=True),
        sa.Column("team_id", sa.String(36), nullable=False, index=True),
        sa.Column("reporter_id", sa.String(36), nullable=True, index=True),
        sa.Column("channel_connection_id", sa.String(36), nullable=True, index=True),
        sa.Column(
            "provider", sa.String(40), nullable=False, server_default="dashboard", index=True
        ),
        sa.Column("provider_event_id", sa.String(200), nullable=True, index=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False, index=True),
        sa.Column("team_side", sa.String(20), nullable=True, index=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending", index=True),
        sa.Column("minute", sa.Integer(), nullable=True),
        sa.Column("stoppage_minute", sa.Integer(), nullable=True),
        sa.Column("home_score_after", sa.Integer(), nullable=True),
        sa.Column("away_score_after", sa.Integer(), nullable=True),
        sa.Column("own_score_after", sa.Integer(), nullable=True),
        sa.Column("opponent_score_after", sa.Integer(), nullable=True),
        sa.Column("player_name", sa.String(160), nullable=True),
        sa.Column("player_id", sa.String(36), nullable=True),
        sa.Column("assist_name", sa.String(160), nullable=True),
        sa.Column("assist_player_id", sa.String(36), nullable=True),
        sa.Column("related_player_name", sa.String(160), nullable=True),
        sa.Column("card_color", sa.String(20), nullable=True),
        sa.Column("reason", sa.String(250), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False, server_default="1"),
        sa.Column(
            "needs_confirmation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            index=True,
        ),
        sa.Column("supersedes_event_id", sa.String(36), nullable=True, index=True),
        sa.Column("raw_text_digest", sa.String(64), nullable=True),
        sa.Column("source_sender_digest", sa.String(64), nullable=True),
        sa.Column("sanitized_input", sa.String(500), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_by", sa.String(36), nullable=True),
        sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("corrected_by", sa.String(36), nullable=True),
        sa.Column("created_by", sa.String(36), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["game_id", "club_id"], ["games.id", "games.club_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "club_id"], ["teams.id", "teams.club_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["reporter_id", "club_id"],
            ["live_reporters.id", "live_reporters.club_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_event_id", "club_id"],
            ["match_events.id", "match_events.club_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["corrected_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "event_type IN ('kickoff','goal','opponent_goal','own_goal','penalty_scored','penalty_missed','yellow_card','second_yellow_card','red_card','substitution','halftime','second_half','fulltime','interruption','resume','abandoned','comment','score_correction','event_correction')",
            name="ck_match_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending','confirmed','rejected','superseded')",
            name="ck_match_event_status",
        ),
        sa.CheckConstraint(
            "minute IS NULL OR minute BETWEEN 0 AND 150", name="ck_match_event_minute"
        ),
        sa.CheckConstraint(
            "home_score_after IS NULL OR home_score_after >= 0", name="ck_match_event_home_score"
        ),
        sa.CheckConstraint(
            "away_score_after IS NULL OR away_score_after >= 0", name="ck_match_event_away_score"
        ),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_match_event_confidence"),
        sa.CheckConstraint("event_sequence >= 1", name="ck_match_event_sequence"),
        sa.CheckConstraint(
            "team_side IS NULL OR team_side IN ('own','opponent','neutral')",
            name="ck_match_event_team_side",
        ),
        sa.UniqueConstraint("id", "club_id", name="uq_match_events_id_club"),
        sa.UniqueConstraint("club_id", "idempotency_key", name="uq_match_events_idempotency"),
        sa.UniqueConstraint("club_id", "game_id", "event_sequence", name="uq_match_event_sequence"),
    )
    op.create_table(
        "live_event_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("team_id", sa.String(36), nullable=False, index=True),
        sa.Column("event_type", sa.String(40), nullable=False, index=True),
        sa.Column("delivery_mode", sa.String(20), nullable=False, server_default="off"),
        sa.Column("audience_type", sa.String(30), nullable=False, server_default="dashboard"),
        sa.Column("whatsapp_audience_id", sa.String(36), nullable=True, index=True),
        sa.Column("channel_types", sa.JSON(), nullable=False, server_default='["dashboard"]'),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false(), index=True),
        sa.Column("require_confirmation", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("settings", sa.JSON(), nullable=False, server_default="{}"),
        *_timestamps(),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["team_id", "club_id"], ["teams.id", "teams.club_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["whatsapp_audience_id", "club_id"],
            ["whatsapp_audiences.id", "whatsapp_audiences.club_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "delivery_mode IN ('off','manual','automatic')", name="ck_live_event_rule_delivery_mode"
        ),
        sa.CheckConstraint(
            "audience_type IN ('dashboard','opt_in_recipients','eligible_group')",
            name="ck_live_event_rule_audience",
        ),
        sa.UniqueConstraint(
            "club_id", "team_id", "event_type", name="uq_live_event_rule_team_type"
        ),
    )
    op.create_table(
        "live_event_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("event_id", sa.String(36), nullable=False, index=True),
        sa.Column("rule_id", sa.String(36), nullable=True, index=True),
        sa.Column("channel_type", sa.String(20), nullable=False, index=True),
        sa.Column("channel_connection_id", sa.String(36), nullable=True, index=True),
        sa.Column("whatsapp_audience_id", sa.String(36), nullable=True, index=True),
        sa.Column("publication_job_id", sa.String(36), nullable=True, index=True),
        sa.Column(
            "status", sa.String(30), nullable=False, server_default="awaiting_approval", index=True
        ),
        sa.Column("target", sa.String(200), nullable=True),
        sa.Column("message_snapshot", sa.Text(), nullable=True),
        sa.Column("platform_id", sa.String(200), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(220), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["event_id", "club_id"], ["match_events.id", "match_events.club_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["rule_id"], ["live_event_rules.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["whatsapp_audience_id", "club_id"],
            ["whatsapp_audiences.id", "whatsapp_audiences.club_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["publication_job_id"], ["publication_jobs.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint(
            "channel_type IN ('dashboard','instagram','facebook','whatsapp')",
            name="ck_live_delivery_channel",
        ),
        sa.CheckConstraint(
            "status IN ('awaiting_approval','queued','processing','sent','delivered','failed','cancelled','blocked')",
            name="ck_live_delivery_status",
        ),
        sa.UniqueConstraint("id", "club_id", name="uq_live_event_deliveries_id_club"),
        sa.UniqueConstraint("club_id", "idempotency_key", name="uq_live_delivery_idempotency"),
    )
    op.create_table(
        "live_delivery_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("delivery_id", sa.String(36), nullable=False, index=True),
        sa.Column("recipient_id", sa.String(36), nullable=True, index=True),
        sa.Column("template_id", sa.String(36), nullable=True, index=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="queued", index=True),
        sa.Column("platform_id", sa.String(200), nullable=True, index=True),
        sa.Column("error_category", sa.String(80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("sanitized_response", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("idempotency_key", sa.String(240), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["delivery_id", "club_id"],
            ["live_event_deliveries.id", "live_event_deliveries.club_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id", "club_id"],
            ["whatsapp_recipients.id", "whatsapp_recipients.club_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["template_id", "club_id"],
            ["whatsapp_message_templates.id", "whatsapp_message_templates.club_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('queued','processing','sent','delivered','read','failed','uncertain','cancelled')",
            name="ck_live_delivery_attempt_status",
        ),
        sa.UniqueConstraint(
            "club_id", "idempotency_key", name="uq_live_delivery_attempt_idempotency"
        ),
    )


def downgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table in (
        "live_delivery_attempts",
        "live_event_deliveries",
        "live_event_rules",
        "match_events",
        "live_game_states",
        "live_reporter_teams",
        "live_reporters",
        "whatsapp_audience_recipients",
        "whatsapp_audiences",
    ):
        if table in existing_tables:
            op.drop_table(table)
