"""versioned AI prompt templates"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "prompt_templates" not in existing:
        op.create_table(
            "prompt_templates",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("prompt_kind", sa.String(20), nullable=False),
            sa.Column("post_type", sa.String(30), nullable=False),
            sa.Column("media_kind", sa.String(10), nullable=False),
            sa.Column("prompt_body", sa.Text(), nullable=False),
            sa.Column("style_direction", sa.Text()),
            sa.Column("model", sa.String(100), nullable=False),
            sa.Column("quality", sa.String(20), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "name", "prompt_kind", "post_type", "media_kind", "version"
            ),
        )
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("story_rules")
    }
    if "prompt_template" not in columns:
        with op.batch_alter_table("story_rules") as batch:
            batch.add_column(
                sa.Column(
                    "prompt_template",
                    sa.String(160),
                    nullable=False,
                    server_default="default-image-story",
                )
            )


def downgrade():
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("story_rules")
    }
    if "prompt_template" in columns:
        with op.batch_alter_table("story_rules") as batch:
            batch.drop_column("prompt_template")
    if "prompt_templates" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("prompt_templates")
