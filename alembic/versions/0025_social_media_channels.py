"""social media channels, Facebook Pages and WhatsApp messaging foundation

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _channel_id(page_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"vereinszentrale:instagram-page:{page_id}"))


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _unique_names(table: str) -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_unique_constraints(table)
        if item.get("name")
    }


def _foreign_key_names(table: str) -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_foreign_keys(table)
        if item.get("name")
    }


def _create_channel_tables() -> None:
    op.create_table(
        "social_channel_connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("channel_type", sa.String(20), nullable=False, index=True),
        sa.Column("internal_name", sa.String(120), nullable=False),
        sa.Column("display_name", sa.String(160), nullable=False),
        sa.Column("username", sa.String(120), nullable=True),
        sa.Column("external_account_id", sa.String(160), nullable=True, index=True),
        sa.Column("parent_business_id", sa.String(160), nullable=True),
        sa.Column("phone_number_id", sa.String(160), nullable=True, index=True),
        sa.Column("display_phone_number", sa.String(40), nullable=True),
        sa.Column("legacy_instagram_page_id", sa.String(36), nullable=True, index=True),
        sa.Column(
            "status", sa.String(40), nullable=False, server_default="setup_required", index=True
        ),
        sa.Column("capabilities", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("scopes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("settings", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("encrypted_token", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("token_key_version", sa.String(40), nullable=True),
        sa.Column("api_version", sa.String(20), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false(), index=True),
        sa.Column("publishing_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "automatic_delivery_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "channel_type IN ('instagram', 'facebook', 'whatsapp')",
            name="ck_social_channel_type",
        ),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["legacy_instagram_page_id", "club_id"],
            ["instagram_pages.id", "instagram_pages.club_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "club_id", name="uq_social_channel_connections_id_club"),
        sa.UniqueConstraint(
            "club_id",
            "channel_type",
            "external_account_id",
            name="uq_social_channel_external_account",
        ),
        sa.UniqueConstraint("legacy_instagram_page_id"),
    )
    op.create_table(
        "social_channel_oauth_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("channel_type", sa.String(20), nullable=False, index=True),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("redirect_uri", sa.String(1000), nullable=False),
        sa.Column("encrypted_selection_payload", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "channel_type IN ('facebook', 'whatsapp')", name="ck_social_channel_oauth_type"
        ),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["user_id", "club_id"],
            ["users.id", "users.club_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "team_channel_assignments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("team_id", sa.String(36), nullable=False, index=True),
        sa.Column("channel_connection_id", sa.String(36), nullable=False, index=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("announcement_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("result_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("story_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(
            ["team_id", "club_id"], ["teams.id", "teams.club_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("team_id", "channel_connection_id", name="uq_team_channel_assignment"),
    )


def _create_whatsapp_tables() -> None:
    op.create_table(
        "whatsapp_recipients",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("channel_connection_id", sa.String(36), nullable=False, index=True),
        sa.Column("normalized_phone", sa.String(32), nullable=False, index=True),
        sa.Column("display_name", sa.String(160), nullable=True),
        sa.Column(
            "opt_in_status", sa.String(20), nullable=False, server_default="pending", index=True
        ),
        sa.Column("opt_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opt_in_source", sa.String(160), nullable=True),
        sa.Column("opt_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false(), index=True),
        sa.Column("preferred_message_types", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("provider_recipient_id", sa.String(160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "opt_in_status IN ('pending', 'confirmed', 'revoked')",
            name="ck_whatsapp_recipient_opt_in",
        ),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "club_id",
            "channel_connection_id",
            "normalized_phone",
            name="uq_whatsapp_recipient_phone",
        ),
        sa.UniqueConstraint("id", "club_id", name="uq_whatsapp_recipients_id_club"),
    )
    op.create_table(
        "whatsapp_message_templates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("channel_connection_id", sa.String(36), nullable=False, index=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("provider_template_id", sa.String(160), nullable=False),
        sa.Column("language", sa.String(20), nullable=False, server_default="de"),
        sa.Column("category", sa.String(40), nullable=False, server_default="utility"),
        sa.Column("message_type", sa.String(40), nullable=False, index=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="draft", index=True),
        sa.Column("components", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("id", "club_id", name="uq_whatsapp_templates_id_club"),
        sa.UniqueConstraint(
            "club_id",
            "channel_connection_id",
            "provider_template_id",
            name="uq_whatsapp_provider_template",
        ),
    )


def _create_post_channel_contents() -> None:
    op.create_table(
        "post_channel_contents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("post_id", sa.String(36), nullable=False, index=True),
        sa.Column("channel_connection_id", sa.String(36), nullable=False, index=True),
        sa.Column("channel_type", sa.String(20), nullable=False, index=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="derived"),
        sa.Column("updated_by", sa.String(36), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["post_id", "club_id"], ["posts.id", "posts.club_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "channel_type IN ('facebook', 'whatsapp')",
            name="ck_post_channel_content_type",
        ),
        sa.CheckConstraint(
            "source IN ('derived', 'manual', 'ai')",
            name="ck_post_channel_content_source",
        ),
        sa.UniqueConstraint(
            "club_id",
            "post_id",
            "channel_connection_id",
            name="uq_post_channel_content_target",
        ),
    )


def _extend_publication_jobs() -> None:
    with op.batch_alter_table("publication_jobs") as batch:
        batch.alter_column("instagram_page_id", existing_type=sa.String(36), nullable=True)
        batch.add_column(
            sa.Column("channel_type", sa.String(20), nullable=False, server_default="instagram")
        )
        batch.add_column(sa.Column("channel_connection_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("content_type", sa.String(40), nullable=True))
        batch.add_column(sa.Column("target", sa.String(200), nullable=True))
        batch.add_column(
            sa.Column("delivery_action", sa.String(20), nullable=False, server_default="publish")
        )
        batch.create_foreign_key(
            "fk_publication_jobs_channel_connection_club",
            "social_channel_connections",
            ["channel_connection_id", "club_id"],
            ["id", "club_id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_publication_jobs_channel_type",
            "channel_type IN ('instagram', 'facebook', 'whatsapp')",
        )
        batch.create_check_constraint(
            "ck_publication_jobs_delivery_action",
            "delivery_action IN ('publish', 'send')",
        )
        batch.create_index("ix_publication_jobs_channel_type", ["channel_type"])
        batch.create_index("ix_publication_jobs_channel_connection_id", ["channel_connection_id"])


def _create_delivery_tables() -> None:
    op.create_table(
        "channel_delivery_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("publication_job_id", sa.String(36), nullable=False, index=True),
        sa.Column("channel_connection_id", sa.String(36), nullable=False, index=True),
        sa.Column("recipient_id", sa.String(36), nullable=True, index=True),
        sa.Column("template_id", sa.String(36), nullable=True, index=True),
        sa.Column("action", sa.String(20), nullable=False, server_default="publish"),
        sa.Column("status", sa.String(40), nullable=False, server_default="queued", index=True),
        sa.Column("platform_id", sa.String(200), nullable=True, index=True),
        sa.Column("cost_amount", sa.Numeric(12, 6), nullable=True),
        sa.Column("cost_currency", sa.String(8), nullable=True),
        sa.Column("sanitized_response", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error_category", sa.String(80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(180), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["publication_job_id"], ["publication_jobs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="RESTRICT",
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
        sa.UniqueConstraint("club_id", "idempotency_key", name="uq_channel_delivery_idempotency"),
    )
    op.create_table(
        "meta_webhook_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("channel_type", sa.String(20), nullable=False, index=True),
        sa.Column("channel_connection_id", sa.String(36), nullable=False, index=True),
        sa.Column("provider_event_key", sa.String(200), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False, index=True),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="received", index=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "club_id",
            "channel_type",
            "provider_event_key",
            name="uq_meta_webhook_event_key",
        ),
    )


def _backfill_instagram_channels() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    pages = sa.Table("instagram_pages", metadata, autoload_with=bind)
    connections = sa.Table("instagram_connections", metadata, autoload_with=bind)
    channels = sa.Table("social_channel_connections", metadata, autoload_with=bind)
    teams = sa.Table("teams", metadata, autoload_with=bind)
    assignments = sa.Table("team_channel_assignments", metadata, autoload_with=bind)
    jobs = sa.Table("publication_jobs", metadata, autoload_with=bind)
    current = _now()

    rows = list(
        bind.execute(
            sa.select(pages, connections).select_from(
                pages.outerjoin(connections, connections.c.instagram_page_id == pages.c.id)
            )
        ).mappings()
    )
    page_channels: dict[str, str] = {}
    for row in rows:
        page_id = row[pages.c.id]
        channel_id = _channel_id(page_id)
        page_channels[page_id] = channel_id
        allowed = row[pages.c.allowed_types] or {}
        capabilities = ["feed_image", "carousel", "caption"]
        if allowed.get("story", True):
            capabilities.append("story")
        status = (
            row[connections.c.status] if row[connections.c.id] else row[pages.c.connection_status]
        )
        bind.execute(
            channels.insert().values(
                id=channel_id,
                club_id=row[pages.c.club_id],
                channel_type="instagram",
                internal_name=row[pages.c.internal_name],
                display_name=row[pages.c.display_name],
                username=row[pages.c.username],
                external_account_id=(
                    row[connections.c.instagram_user_id]
                    if row[connections.c.id]
                    else row[pages.c.account_id]
                ),
                legacy_instagram_page_id=page_id,
                status=status or "setup_required",
                capabilities=capabilities,
                scopes=row[connections.c.scopes] if row[connections.c.id] else [],
                settings={
                    "feed_enabled": bool(allowed.get("feed", True)),
                    "story_enabled": bool(allowed.get("story", True)),
                },
                encrypted_token=None,
                token_expires_at=(
                    row[connections.c.token_expires_at] if row[connections.c.id] else None
                ),
                token_key_version=(
                    row[connections.c.token_key_version] if row[connections.c.id] else None
                ),
                api_version=(row[connections.c.api_version] if row[connections.c.id] else None),
                active=bool(row[pages.c.active]),
                publishing_enabled=bool(row[pages.c.publishing_enabled]),
                automatic_delivery_enabled=bool(row[pages.c.automatic_publishing_enabled]),
                last_check_at=(
                    row[connections.c.last_check_at]
                    if row[connections.c.id]
                    else row[pages.c.last_check_at]
                ),
                last_success_at=(
                    row[connections.c.last_success_at] if row[connections.c.id] else None
                ),
                last_error=(
                    row[connections.c.last_error]
                    if row[connections.c.id]
                    else row[pages.c.last_error]
                ),
                disconnected_at=(
                    row[connections.c.disconnected_at] if row[connections.c.id] else None
                ),
                created_at=current,
                updated_at=current,
                version=1,
            )
        )

    for row in bind.execute(sa.select(teams)).mappings():
        channel_id = page_channels.get(row[teams.c.instagram_page_id])
        if not channel_id:
            continue
        bind.execute(
            assignments.insert().values(
                id=str(
                    uuid5(
                        NAMESPACE_URL,
                        f"vereinszentrale:team-channel:{row[teams.c.id]}:{channel_id}",
                    )
                ),
                club_id=row[teams.c.club_id],
                team_id=row[teams.c.id],
                channel_connection_id=channel_id,
                enabled=True,
                announcement_enabled=True,
                result_enabled=True,
                story_enabled=True,
                created_at=current,
                updated_at=current,
                version=1,
            )
        )

    for page_id, channel_id in page_channels.items():
        bind.execute(
            jobs.update()
            .where(jobs.c.instagram_page_id == page_id)
            .values(
                channel_type="instagram",
                channel_connection_id=channel_id,
                content_type=jobs.c.kind,
                delivery_action="publish",
            )
        )


def upgrade() -> None:
    existing = _tables()
    channels_were_missing = "social_channel_connections" not in existing
    if "uq_posts_id_club" not in _unique_names("posts"):
        with op.batch_alter_table("posts") as batch:
            batch.create_unique_constraint("uq_posts_id_club", ["id", "club_id"])
    if channels_were_missing:
        _create_channel_tables()
    existing = _tables()
    if "whatsapp_recipients" not in existing:
        _create_whatsapp_tables()
    if "channel_type" not in _columns("publication_jobs"):
        _extend_publication_jobs()
    existing = _tables()
    if "channel_delivery_attempts" not in existing:
        _create_delivery_tables()
    if "post_channel_contents" not in existing:
        _create_post_channel_contents()
    if channels_were_missing:
        _backfill_instagram_channels()


def downgrade() -> None:
    if "post_channel_contents" in _tables():
        op.drop_table("post_channel_contents")
    op.drop_table("meta_webhook_events")
    op.drop_table("channel_delivery_attempts")
    publication_foreign_keys = _foreign_key_names("publication_jobs")
    with op.batch_alter_table("publication_jobs") as batch:
        batch.drop_index("ix_publication_jobs_channel_connection_id")
        batch.drop_index("ix_publication_jobs_channel_type")
        batch.drop_constraint("ck_publication_jobs_delivery_action", type_="check")
        batch.drop_constraint("ck_publication_jobs_channel_type", type_="check")
        if "fk_publication_jobs_channel_connection_club" in publication_foreign_keys:
            batch.drop_constraint("fk_publication_jobs_channel_connection_club", type_="foreignkey")
        batch.drop_column("delivery_action")
        batch.drop_column("target")
        batch.drop_column("content_type")
        batch.drop_column("channel_connection_id")
        batch.drop_column("channel_type")
        batch.alter_column("instagram_page_id", existing_type=sa.String(36), nullable=False)
    op.drop_table("whatsapp_message_templates")
    op.drop_table("whatsapp_recipients")
    op.drop_table("team_channel_assignments")
    op.drop_table("social_channel_oauth_states")
    op.drop_table("social_channel_connections")
    if "uq_posts_id_club" in _unique_names("posts"):
        with op.batch_alter_table("posts") as batch:
            batch.drop_constraint("uq_posts_id_club", type_="unique")
