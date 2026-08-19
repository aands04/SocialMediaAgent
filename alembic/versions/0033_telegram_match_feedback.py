"""provider-neutral match feedback and Telegram bot connections

Revision ID: 0033
Revises: 0032
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_social_channel_type", "social_channel_connections", type_="check")
    op.create_check_constraint(
        "ck_social_channel_type",
        "social_channel_connections",
        "channel_type IN ('instagram','facebook','whatsapp','telegram')",
    )
    op.create_index(
        "uq_social_channel_telegram_bot_global",
        "social_channel_connections",
        ["external_account_id"],
        unique=True,
        postgresql_where=sa.text("channel_type='telegram' AND external_account_id IS NOT NULL"),
        sqlite_where=sa.text("channel_type='telegram' AND external_account_id IS NOT NULL"),
    )
    op.create_index(
        "uq_social_channel_telegram_active_club",
        "social_channel_connections",
        ["club_id"],
        unique=True,
        postgresql_where=sa.text("channel_type='telegram' AND active IS TRUE"),
        sqlite_where=sa.text("channel_type='telegram' AND active = 1"),
    )
    op.alter_column(
        "match_feedback_contacts",
        "recipient_id",
        existing_type=sa.String(36),
        nullable=True,
    )
    op.alter_column(
        "match_feedback_contacts",
        "normalized_phone",
        existing_type=sa.String(32),
        nullable=True,
    )
    op.add_column(
        "match_feedback_contacts",
        sa.Column(
            "preferred_provider",
            sa.String(20),
            nullable=True,
            server_default="whatsapp",
        ),
    )
    # Existing contacts retain the prior WhatsApp routing preference. New
    # provider-neutral contacts must opt in explicitly and must not inherit a
    # database-level default silently.
    op.alter_column(
        "match_feedback_contacts",
        "preferred_provider",
        existing_type=sa.String(20),
        server_default=None,
    )
    op.add_column(
        "match_feedback_contacts", sa.Column("fallback_provider", sa.String(20), nullable=True)
    )
    op.create_index(
        "ix_match_feedback_contacts_preferred_provider",
        "match_feedback_contacts",
        ["preferred_provider"],
    )

    for name, column in (
        (
            "provider",
            sa.Column("provider", sa.String(20), nullable=False, server_default="whatsapp"),
        ),
        ("external_chat_id", sa.Column("external_chat_id", sa.String(200), nullable=True)),
        ("external_message_id", sa.Column("external_message_id", sa.String(200), nullable=True)),
        ("sent_at", sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True)),
        (
            "delivery_status",
            sa.Column("delivery_status", sa.String(30), nullable=False, server_default="queued"),
        ),
    ):
        op.add_column("match_feedback_requests", column)
        if name != "sent_at":
            op.create_index(
                f"ix_match_feedback_requests_{name}",
                "match_feedback_requests",
                [name],
            )
    op.create_check_constraint(
        "ck_match_feedback_request_provider",
        "match_feedback_requests",
        "provider IN ('whatsapp','telegram')",
    )
    op.execute(
        "UPDATE match_feedback_requests "
        "SET external_message_id=provider_message_id, sent_at=requested_at, "
        "delivery_status=CASE WHEN status='sent' THEN 'sent' ELSE status END"
    )

    for name, column in (
        (
            "provider",
            sa.Column("provider", sa.String(20), nullable=False, server_default="whatsapp"),
        ),
        ("external_chat_id", sa.Column("external_chat_id", sa.String(200), nullable=True)),
        ("external_sender_id", sa.Column("external_sender_id", sa.String(200), nullable=True)),
        (
            "payload_type",
            sa.Column("payload_type", sa.String(30), nullable=False, server_default="text"),
        ),
        (
            "payload_metadata",
            sa.Column(
                "payload_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
            ),
        ),
        ("source_role", sa.Column("source_role", sa.String(40), nullable=True)),
        (
            "no_additional_feedback",
            sa.Column(
                "no_additional_feedback", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
        ),
    ):
        op.add_column("match_feedback_responses", column)
        if name in {"provider", "external_chat_id", "external_sender_id"}:
            op.create_index(
                f"ix_match_feedback_responses_{name}",
                "match_feedback_responses",
                [name],
            )
    op.drop_constraint(
        "uq_match_feedback_response_msg",
        "match_feedback_responses",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_match_feedback_response_provider_msg",
        "match_feedback_responses",
        ["club_id", "provider", "provider_message_id"],
    )
    op.create_check_constraint(
        "ck_match_feedback_response_provider",
        "match_feedback_responses",
        "provider IN ('whatsapp','telegram')",
    )
    op.create_check_constraint(
        "ck_match_feedback_contact_preferred_provider",
        "match_feedback_contacts",
        "preferred_provider IS NULL OR preferred_provider IN ('whatsapp','telegram')",
    )
    op.create_check_constraint(
        "ck_match_feedback_contact_fallback_provider",
        "match_feedback_contacts",
        "fallback_provider IS NULL OR fallback_provider IN ('whatsapp','telegram')",
    )
    op.create_check_constraint(
        "ck_match_feedback_contact_distinct_providers",
        "match_feedback_contacts",
        "fallback_provider IS NULL OR fallback_provider <> preferred_provider",
    )

    op.create_table(
        "match_feedback_endpoints",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False),
        sa.Column("contact_id", sa.String(36), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("connection_id", sa.String(36), nullable=False),
        sa.Column("external_user_id", sa.String(200)),
        sa.Column("external_chat_id", sa.String(200)),
        sa.Column("external_username", sa.String(160)),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("linked_at", sa.DateTime(timezone=True)),
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["contact_id", "club_id"],
            ["match_feedback_contacts.id", "match_feedback_contacts.club_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "club_id", name="uq_match_feedback_endpoints_id_club"),
        sa.UniqueConstraint(
            "club_id", "provider", "external_chat_id", name="uq_match_feedback_endpoint_chat"
        ),
        sa.UniqueConstraint(
            "club_id", "contact_id", "provider", name="uq_match_feedback_endpoint_contact"
        ),
        sa.CheckConstraint(
            "provider IN ('whatsapp','telegram')", name="ck_match_feedback_endpoint_provider"
        ),
        sa.CheckConstraint(
            "status IN ('pending','connected','disabled','error')",
            name="ck_match_feedback_endpoint_status",
        ),
    )
    for col in (
        "club_id",
        "contact_id",
        "provider",
        "connection_id",
        "external_user_id",
        "external_chat_id",
        "status",
        "is_primary",
    ):
        op.create_index(
            f"ix_match_feedback_endpoints_{col}",
            "match_feedback_endpoints",
            [col],
        )

    op.create_table(
        "match_feedback_link_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False),
        sa.Column("contact_id", sa.String(36), nullable=False),
        sa.Column("connection_id", sa.String(36), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["contact_id", "club_id"],
            ["match_feedback_contacts.id", "match_feedback_contacts.club_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("token_digest", name="uq_match_feedback_link_token_digest"),
    )
    for col in ("club_id", "contact_id", "connection_id", "token_digest", "expires_at", "used_at"):
        op.create_index(
            f"ix_match_feedback_link_tokens_{col}",
            "match_feedback_link_tokens",
            [col],
        )

    op.create_table(
        "telegram_webhook_updates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False),
        sa.Column("connection_id", sa.String(36), nullable=False),
        sa.Column("update_id", sa.String(80), nullable=False),
        sa.Column("update_type", sa.String(40), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="received"),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("connection_id", "update_id", name="uq_telegram_webhook_update"),
    )
    for col in ("club_id", "connection_id", "update_id", "status"):
        op.create_index(
            f"ix_telegram_webhook_updates_{col}",
            "telegram_webhook_updates",
            [col],
        )

    # Existing clubs keep their prior WhatsApp route. No silent Telegram migration.
    op.execute("""INSERT INTO match_feedback_endpoints (id,club_id,contact_id,provider,connection_id,external_user_id,external_chat_id,status,is_primary,linked_at,created_at,updated_at,version)
        SELECT md5(c.id || '-whatsapp'), c.club_id, c.id, 'whatsapp', r.channel_connection_id, r.normalized_phone, r.normalized_phone, 'connected', true, c.created_at, c.created_at, c.updated_at, 1
        FROM match_feedback_contacts c JOIN whatsapp_recipients r ON r.id=c.recipient_id
        WHERE c.recipient_id IS NOT NULL""")


def downgrade() -> None:
    # Provider-neutral or Telegram records cannot be represented by the old
    # schema. Fail explicitly and portably instead of silently losing data.
    bind = op.get_bind()
    incompatible_queries = (
        "SELECT 1 FROM social_channel_connections WHERE channel_type='telegram' LIMIT 1",
        "SELECT 1 FROM match_feedback_contacts "
        "WHERE recipient_id IS NULL OR normalized_phone IS NULL LIMIT 1",
        "SELECT 1 FROM match_feedback_requests WHERE provider='telegram' LIMIT 1",
        "SELECT 1 FROM match_feedback_responses WHERE provider='telegram' LIMIT 1",
        "SELECT 1 FROM match_feedback_endpoints WHERE provider='telegram' LIMIT 1",
    )
    if any(bind.execute(sa.text(query)).first() for query in incompatible_queries):
        raise RuntimeError(
            "0033 downgrade blocked: Telegram- oder provider-neutrale Rückmeldedaten sind vorhanden"
        )
    for table in (
        "telegram_webhook_updates",
        "match_feedback_link_tokens",
        "match_feedback_endpoints",
    ):
        op.drop_table(table)
    op.drop_constraint(
        "ck_match_feedback_response_provider",
        "match_feedback_responses",
        type_="check",
    )
    op.drop_constraint(
        "uq_match_feedback_response_provider_msg",
        "match_feedback_responses",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_match_feedback_response_msg",
        "match_feedback_responses",
        ["club_id", "provider_message_id"],
    )
    for col in (
        "source_role",
        "no_additional_feedback",
        "payload_metadata",
        "payload_type",
        "external_sender_id",
        "external_chat_id",
        "provider",
    ):
        op.drop_column("match_feedback_responses", col)
    op.drop_constraint(
        "ck_match_feedback_request_provider",
        "match_feedback_requests",
        type_="check",
    )
    for col in (
        "delivery_status",
        "sent_at",
        "external_message_id",
        "external_chat_id",
        "provider",
    ):
        op.drop_column("match_feedback_requests", col)
    for constraint in (
        "ck_match_feedback_contact_distinct_providers",
        "ck_match_feedback_contact_fallback_provider",
        "ck_match_feedback_contact_preferred_provider",
    ):
        op.drop_constraint(constraint, "match_feedback_contacts", type_="check")
    op.drop_column("match_feedback_contacts", "fallback_provider")
    op.drop_column("match_feedback_contacts", "preferred_provider")
    op.alter_column(
        "match_feedback_contacts",
        "normalized_phone",
        existing_type=sa.String(32),
        nullable=False,
    )
    op.alter_column(
        "match_feedback_contacts",
        "recipient_id",
        existing_type=sa.String(36),
        nullable=False,
    )
    op.drop_constraint("ck_social_channel_type", "social_channel_connections", type_="check")
    op.drop_index(
        "uq_social_channel_telegram_active_club",
        table_name="social_channel_connections",
    )
    op.drop_index(
        "uq_social_channel_telegram_bot_global",
        table_name="social_channel_connections",
    )
    op.create_check_constraint(
        "ck_social_channel_type",
        "social_channel_connections",
        "channel_type IN ('instagram','facebook','whatsapp')",
    )
