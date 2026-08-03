"""ordered manual carousel media and persistent Meta child containers"""

import sqlalchemy as sa

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    if table not in _tables():
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    if table not in _tables():
        return set()
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table)
        if index.get("name")
    }


def upgrade():
    if "publication_media_items" not in _tables():
        op.create_table(
            "publication_media_items",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "publication_job_id",
                sa.String(36),
                sa.ForeignKey("publication_jobs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("media_path", sa.String(800), nullable=False),
            sa.Column("checksum", sa.String(64), nullable=False),
            sa.Column("mime_type", sa.String(80), nullable=False),
            sa.Column("file_size", sa.Integer(), nullable=False),
            sa.Column("width", sa.Integer(), nullable=False),
            sa.Column("height", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.UniqueConstraint(
                "publication_job_id", "position", name="uq_publication_media_position"
            ),
        )
        op.create_index(
            "ix_publication_media_items_publication_job_id",
            "publication_media_items",
            ["publication_job_id"],
        )
    if "publication_media_item_id" not in _columns("public_media_grants"):
        with op.batch_alter_table("public_media_grants") as batch:
            batch.add_column(sa.Column("publication_media_item_id", sa.String(36)))
            batch.create_foreign_key(
                "fk_public_media_grants_media_item",
                "publication_media_items",
                ["publication_media_item_id"],
                ["id"],
                ondelete="CASCADE",
            )
            batch.create_index(
                "ix_public_media_grants_publication_media_item_id",
                ["publication_media_item_id"],
            )
    if "meta_carousel_items" not in _tables():
        op.create_table(
            "meta_carousel_items",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "attempt_id",
                sa.String(36),
                sa.ForeignKey("meta_publishing_attempts.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "publication_media_item_id",
                sa.String(36),
                sa.ForeignKey("publication_media_items.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "public_media_grant_id",
                sa.String(36),
                sa.ForeignKey("public_media_grants.id", ondelete="SET NULL"),
            ),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("meta_container_id", sa.String(120)),
            sa.Column("container_status", sa.String(80)),
            sa.Column("sanitized_response", sa.JSON(), nullable=False),
            sa.Column("error_category", sa.String(80)),
            sa.Column("error_message", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.UniqueConstraint(
                "attempt_id", "position", name="uq_meta_carousel_position"
            ),
            sa.UniqueConstraint(
                "attempt_id",
                "publication_media_item_id",
                name="uq_meta_carousel_media_item",
            ),
        )
        op.create_index(
            "ix_meta_carousel_items_attempt_id", "meta_carousel_items", ["attempt_id"]
        )
        op.create_index(
            "ix_meta_carousel_items_publication_media_item_id",
            "meta_carousel_items",
            ["publication_media_item_id"],
        )
        op.create_index(
            "ix_meta_carousel_items_meta_container_id",
            "meta_carousel_items",
            ["meta_container_id"],
        )


def downgrade():
    if "meta_carousel_items" in _tables():
        op.drop_table("meta_carousel_items")
    if "publication_media_item_id" in _columns("public_media_grants"):
        with op.batch_alter_table("public_media_grants") as batch:
            if "ix_public_media_grants_publication_media_item_id" in _indexes(
                "public_media_grants"
            ):
                batch.drop_index("ix_public_media_grants_publication_media_item_id")
            batch.drop_column("publication_media_item_id")
    if "publication_media_items" in _tables():
        op.drop_table("publication_media_items")
