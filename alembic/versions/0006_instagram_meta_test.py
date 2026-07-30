"""isolated Instagram Login meta-test integration"""

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    # Migration 0001 in this legacy repository creates the then-current
    # SQLAlchemy metadata. A completely fresh database therefore already
    # contains new tables before later migrations run. Existing production
    # databases at 0005 do not. Preserve both upgrade paths without changing
    # the historical migration.
    required_tables = {
        "instagram_connections",
        "instagram_oauth_states",
        "public_media_grants",
        "meta_publishing_attempts",
        "meta_publish_confirmations",
    }
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if required_tables.issubset(existing_tables):
        return
    partially_existing = required_tables.intersection(existing_tables)
    if partially_existing:
        raise RuntimeError(
            "Unvollständiges Meta-Test-Schema erkannt: "
            + ", ".join(sorted(partially_existing))
        )

    op.create_table(
        "instagram_connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "instagram_page_id",
            sa.String(36),
            sa.ForeignKey("instagram_pages.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("instagram_user_id", sa.String(100)),
        sa.Column("confirmed_username", sa.String(100)),
        sa.Column("account_type", sa.String(40)),
        sa.Column("login_variant", sa.String(40), nullable=False),
        sa.Column("api_version", sa.String(20), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("encrypted_token", sa.Text()),
        sa.Column("token_expires_at", sa.DateTime(timezone=True)),
        sa.Column("token_key_version", sa.String(40)),
        sa.Column("test_account", sa.Boolean(), nullable=False),
        sa.Column("last_check_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("disconnected_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_instagram_connections_instagram_page_id",
        "instagram_connections",
        ["instagram_page_id"],
    )
    op.create_index(
        "ix_instagram_connections_instagram_user_id",
        "instagram_connections",
        ["instagram_user_id"],
    )
    op.create_index("ix_instagram_connections_status", "instagram_connections", ["status"])
    op.create_index(
        "ix_instagram_connections_token_expires_at",
        "instagram_connections",
        ["token_expires_at"],
    )

    op.create_table(
        "instagram_oauth_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "instagram_page_id",
            sa.String(36),
            sa.ForeignKey("instagram_pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("redirect_uri", sa.String(1000), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_instagram_oauth_states_state_hash", "instagram_oauth_states", ["state_hash"])
    op.create_index(
        "ix_instagram_oauth_states_instagram_page_id",
        "instagram_oauth_states",
        ["instagram_page_id"],
    )
    op.create_index("ix_instagram_oauth_states_user_id", "instagram_oauth_states", ["user_id"])
    op.create_index(
        "ix_instagram_oauth_states_expires_at", "instagram_oauth_states", ["expires_at"]
    )

    op.create_table(
        "public_media_grants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "publication_job_id",
            sa.String(36),
            sa.ForeignKey("publication_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("active_key", sa.String(120), unique=True),
        sa.Column("media_path", sa.String(800), nullable=False),
        sa.Column("file_checksum", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(80), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("fetch_count", sa.Integer(), nullable=False),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_public_media_grants_publication_job_id",
        "public_media_grants",
        ["publication_job_id"],
    )
    op.create_index("ix_public_media_grants_token_hash", "public_media_grants", ["token_hash"])
    op.create_index(
        "ix_public_media_grants_expires_at", "public_media_grants", ["expires_at"]
    )

    op.create_table(
        "meta_publishing_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "publication_job_id",
            sa.String(36),
            sa.ForeignKey("publication_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            sa.String(36),
            sa.ForeignKey("instagram_connections.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "public_media_grant_id",
            sa.String(36),
            sa.ForeignKey("public_media_grants.id", ondelete="SET NULL"),
        ),
        sa.Column("active_key", sa.String(120), unique=True),
        sa.Column("target_account_id", sa.String(100), nullable=False),
        sa.Column("media_kind", sa.String(20), nullable=False),
        sa.Column("local_media_version", sa.Integer(), nullable=False),
        sa.Column("media_path", sa.String(800), nullable=False),
        sa.Column("file_checksum", sa.String(64), nullable=False),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("phase", sa.String(40), nullable=False),
        sa.Column("meta_container_id", sa.String(120)),
        sa.Column("container_status", sa.String(80)),
        sa.Column("meta_media_id", sa.String(120)),
        sa.Column("permalink", sa.String(1000)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("sanitized_response", sa.JSON(), nullable=False),
        sa.Column("error_category", sa.String(80)),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_meta_publishing_attempts_publication_job_id",
        "meta_publishing_attempts",
        ["publication_job_id"],
    )
    op.create_index(
        "ix_meta_publishing_attempts_connection_id",
        "meta_publishing_attempts",
        ["connection_id"],
    )
    op.create_index(
        "ix_meta_publishing_attempts_phase", "meta_publishing_attempts", ["phase"]
    )
    op.create_index(
        "ix_meta_publishing_attempts_meta_container_id",
        "meta_publishing_attempts",
        ["meta_container_id"],
    )
    op.create_index(
        "ix_meta_publishing_attempts_meta_media_id",
        "meta_publishing_attempts",
        ["meta_media_id"],
    )

    op.create_table(
        "meta_publish_confirmations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "attempt_id",
            sa.String(36),
            sa.ForeignKey("meta_publishing_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_meta_publish_confirmations_attempt_id",
        "meta_publish_confirmations",
        ["attempt_id"],
    )
    op.create_index(
        "ix_meta_publish_confirmations_expires_at",
        "meta_publish_confirmations",
        ["expires_at"],
    )


def downgrade():
    op.drop_table("meta_publish_confirmations")
    op.drop_table("meta_publishing_attempts")
    op.drop_table("public_media_grants")
    op.drop_table("instagram_oauth_states")
    op.drop_table("instagram_connections")
