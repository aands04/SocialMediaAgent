"""controlled automatic Instagram publishing through the scheduler"""

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _indexes(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes(table)}


def upgrade():
    # Migration 0001 creates the current metadata on fresh databases. Existing
    # installations at 0007 receive only the missing columns and indexes here.
    page_columns = _columns("instagram_pages")
    with op.batch_alter_table("instagram_pages") as batch:
        if "automatic_publishing_enabled" not in page_columns:
            batch.add_column(
                sa.Column(
                    "automatic_publishing_enabled",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )
        if "automatic_publishing_confirmed_by" not in page_columns:
            batch.add_column(
                sa.Column(
                    "automatic_publishing_confirmed_by",
                    sa.String(36),
                    sa.ForeignKey(
                        "users.id",
                        name=(
                            "fk_instagram_pages_automatic_publishing_"
                            "confirmed_by_users"
                        ),
                    ),
                )
            )
        if "automatic_publishing_confirmed_at" not in page_columns:
            batch.add_column(
                sa.Column(
                    "automatic_publishing_confirmed_at",
                    sa.DateTime(timezone=True),
                )
            )

    job_columns = _columns("publication_jobs")
    if "next_attempt_at" not in job_columns:
        with op.batch_alter_table("publication_jobs") as batch:
            batch.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True)))
    if "ix_publication_jobs_next_attempt_at" not in _indexes("publication_jobs"):
        op.create_index(
            "ix_publication_jobs_next_attempt_at",
            "publication_jobs",
            ["next_attempt_at"],
        )

    attempt_columns = _columns("meta_publishing_attempts")
    with op.batch_alter_table("meta_publishing_attempts") as batch:
        if "trigger_mode" not in attempt_columns:
            batch.add_column(
                sa.Column(
                    "trigger_mode",
                    sa.String(20),
                    nullable=False,
                    server_default="manual",
                )
            )
        if "next_action_at" not in attempt_columns:
            batch.add_column(sa.Column("next_action_at", sa.DateTime(timezone=True)))
    attempt_indexes = _indexes("meta_publishing_attempts")
    if "ix_meta_publishing_attempts_trigger_mode" not in attempt_indexes:
        op.create_index(
            "ix_meta_publishing_attempts_trigger_mode",
            "meta_publishing_attempts",
            ["trigger_mode"],
        )
    if "ix_meta_publishing_attempts_next_action_at" not in attempt_indexes:
        op.create_index(
            "ix_meta_publishing_attempts_next_action_at",
            "meta_publishing_attempts",
            ["next_action_at"],
        )


def downgrade():
    if "ix_meta_publishing_attempts_next_action_at" in _indexes(
        "meta_publishing_attempts"
    ):
        op.drop_index(
            "ix_meta_publishing_attempts_next_action_at",
            table_name="meta_publishing_attempts",
        )
    if "ix_meta_publishing_attempts_trigger_mode" in _indexes(
        "meta_publishing_attempts"
    ):
        op.drop_index(
            "ix_meta_publishing_attempts_trigger_mode",
            table_name="meta_publishing_attempts",
        )
    attempt_columns = _columns("meta_publishing_attempts")
    with op.batch_alter_table("meta_publishing_attempts") as batch:
        if "next_action_at" in attempt_columns:
            batch.drop_column("next_action_at")
        if "trigger_mode" in attempt_columns:
            batch.drop_column("trigger_mode")

    if "ix_publication_jobs_next_attempt_at" in _indexes("publication_jobs"):
        op.drop_index(
            "ix_publication_jobs_next_attempt_at", table_name="publication_jobs"
        )
    if "next_attempt_at" in _columns("publication_jobs"):
        with op.batch_alter_table("publication_jobs") as batch:
            batch.drop_column("next_attempt_at")

    page_columns = _columns("instagram_pages")
    with op.batch_alter_table("instagram_pages") as batch:
        if "automatic_publishing_confirmed_at" in page_columns:
            batch.drop_column("automatic_publishing_confirmed_at")
        if "automatic_publishing_confirmed_by" in page_columns:
            batch.drop_column("automatic_publishing_confirmed_by")
        if "automatic_publishing_enabled" in page_columns:
            batch.drop_column("automatic_publishing_enabled")
