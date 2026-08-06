"""add platform-wide verified opponent logo catalog

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade():
    if "shared_opponent_logos" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "shared_opponent_logos",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("normalized_name", sa.String(length=200), nullable=False),
        sa.Column("original_path", sa.String(length=800), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=80), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("catalog_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_club_id", sa.String(length=36), nullable=True),
        sa.Column("uploaded_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["source_club_id"], ["clubs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "normalized_name", "checksum", name="uq_shared_opponent_logo_name_checksum"
        ),
    )
    op.create_index(
        "ix_shared_opponent_logos_normalized_name",
        "shared_opponent_logos",
        ["normalized_name"],
    )
    op.create_index(
        "ix_shared_opponent_logos_checksum", "shared_opponent_logos", ["checksum"]
    )
    op.create_index(
        "ix_shared_opponent_logos_source_club_id",
        "shared_opponent_logos",
        ["source_club_id"],
    )
    op.create_index(
        "ix_shared_opponent_logo_name_active",
        "shared_opponent_logos",
        ["normalized_name", "active"],
    )


def downgrade():
    if "shared_opponent_logos" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("shared_opponent_logos")
