"""weekday-specific story publication schedules"""

import sqlalchemy as sa

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade():
    columns = _columns("story_rules")
    with op.batch_alter_table("story_rules") as batch:
        if "timing_mode" not in columns:
            batch.add_column(
                sa.Column(
                    "timing_mode",
                    sa.String(20),
                    nullable=False,
                    server_default="relative",
                )
            )
        if "weekday_times" not in columns:
            batch.add_column(
                sa.Column(
                    "weekday_times",
                    sa.JSON(),
                    nullable=False,
                    server_default="{}",
                )
            )


def downgrade():
    columns = _columns("story_rules")
    with op.batch_alter_table("story_rules") as batch:
        if "weekday_times" in columns:
            batch.drop_column("weekday_times")
        if "timing_mode" in columns:
            batch.drop_column("timing_mode")
