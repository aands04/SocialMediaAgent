"""story target weekdays and explicit media slots"""

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
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
        if "weekday_targets" not in columns:
            batch.add_column(
                sa.Column(
                    "weekday_targets",
                    sa.JSON(),
                    nullable=False,
                    server_default="{}",
                )
            )
        if "media_slot" not in columns:
            batch.add_column(
                sa.Column(
                    "media_slot",
                    sa.Integer(),
                    nullable=False,
                    server_default="1",
                )
            )

    # Preserve the existing visual order: two existing Story rules become
    # Story output 1 and 2 instead of unexpectedly sharing one generated file.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, team_id, post_type FROM story_rules "
            "WHERE active = true ORDER BY team_id, post_type, sort_order, created_at, id"
        )
    ).mappings()
    positions: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row["team_id"], row["post_type"])
        positions[key] = positions.get(key, 0) + 1
        bind.execute(
            sa.text("UPDATE story_rules SET media_slot = :slot WHERE id = :id"),
            {"slot": positions[key], "id": row["id"]},
        )


def downgrade():
    columns = _columns("story_rules")
    with op.batch_alter_table("story_rules") as batch:
        if "media_slot" in columns:
            batch.drop_column("media_slot")
        if "weekday_targets" in columns:
            batch.drop_column("weekday_targets")
