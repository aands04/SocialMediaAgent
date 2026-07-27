"""dashboard managed assets and provider snapshots"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "font_assets" not in existing:
        op.create_table("font_assets", sa.Column("id", sa.String(36), primary_key=True), sa.Column("name", sa.String(160), nullable=False, unique=True), sa.Column("family", sa.String(160), nullable=False), sa.Column("relative_path", sa.String(800), nullable=False, unique=True), sa.Column("mime_type", sa.String(80), nullable=False), sa.Column("size", sa.Integer(), nullable=False), sa.Column("active", sa.Boolean(), nullable=False), sa.Column("archived_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.Column("version", sa.Integer(), nullable=False))
    if "design_templates" not in existing:
        op.create_table("design_templates", sa.Column("id", sa.String(36), primary_key=True), sa.Column("name", sa.String(160), nullable=False), sa.Column("post_type", sa.String(30), nullable=False), sa.Column("media_kind", sa.String(10), nullable=False), sa.Column("width", sa.Integer(), nullable=False), sa.Column("height", sa.Integer(), nullable=False), sa.Column("html_template", sa.Text(), nullable=False), sa.Column("css", sa.Text(), nullable=False), sa.Column("defaults", sa.JSON(), nullable=False), sa.Column("required_assets", sa.JSON(), nullable=False), sa.Column("version", sa.Integer(), nullable=False), sa.Column("active", sa.Boolean(), nullable=False), sa.Column("archived_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("name", "version"))
    if "provider_snapshots" not in existing:
        op.create_table("provider_snapshots", sa.Column("id", sa.String(36), primary_key=True), sa.Column("team_id", sa.String(36), sa.ForeignKey("teams.id")), sa.Column("source_url", sa.String(1000), nullable=False), sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False), sa.Column("status_code", sa.Integer(), nullable=False), sa.Column("checksum", sa.String(64), nullable=False), sa.Column("relative_path", sa.String(800), nullable=False, unique=True), sa.Column("parser_result", sa.JSON(), nullable=False), sa.Column("error", sa.Text()))


def downgrade():
    op.drop_table("provider_snapshots")
    op.drop_table("design_templates")
    op.drop_table("font_assets")
