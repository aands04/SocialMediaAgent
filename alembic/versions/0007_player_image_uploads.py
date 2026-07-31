"""store dashboard player-image uploads separately from external media"""

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if "media_assets" not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns("media_assets")}


def upgrade():
    # Migration 0001 creates current metadata on fresh installations. Existing
    # databases reaching 0007 still need the discriminator added explicitly.
    if "storage_kind" not in _columns():
        with op.batch_alter_table("media_assets") as batch:
            batch.add_column(
                sa.Column(
                    "storage_kind",
                    sa.String(20),
                    nullable=False,
                    server_default="external",
                )
            )


def downgrade():
    if "storage_kind" in _columns():
        with op.batch_alter_table("media_assets") as batch:
            batch.drop_column("storage_kind")
