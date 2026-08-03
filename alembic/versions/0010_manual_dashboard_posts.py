"""spielunabhängige, manuell hochgeladene Dashboard-Beiträge"""

import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def _columns(table: str) -> dict[str, dict]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return {}
    return {column["name"]: column for column in inspector.get_columns(table)}


def _unique_constraints(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(table)
        if constraint.get("name")
    }


def upgrade():
    post_columns = _columns("posts")
    with op.batch_alter_table("posts") as batch:
        if "manual_submission_id" not in post_columns:
            batch.add_column(sa.Column("manual_submission_id", sa.String(120)))
        if not post_columns.get("game_id", {}).get("nullable", True):
            batch.alter_column(
                "game_id",
                existing_type=sa.String(36),
                nullable=True,
            )
    if "uq_posts_manual_submission_id" not in _unique_constraints("posts"):
        with op.batch_alter_table("posts") as batch:
            batch.create_unique_constraint(
                "uq_posts_manual_submission_id", ["manual_submission_id"]
            )

    job_columns = _columns("publication_jobs")
    if not job_columns.get("game_id", {}).get("nullable", True):
        with op.batch_alter_table("publication_jobs") as batch:
            batch.alter_column(
                "game_id",
                existing_type=sa.String(36),
                nullable=True,
            )


def downgrade():
    if "uq_posts_manual_submission_id" in _unique_constraints("posts"):
        with op.batch_alter_table("posts") as batch:
            batch.drop_constraint("uq_posts_manual_submission_id", type_="unique")
    post_columns = _columns("posts")
    with op.batch_alter_table("posts") as batch:
        if "manual_submission_id" in post_columns:
            batch.drop_column("manual_submission_id")
        if post_columns.get("game_id", {}).get("nullable", False):
            batch.alter_column(
                "game_id",
                existing_type=sa.String(36),
                nullable=False,
            )
    job_columns = _columns("publication_jobs")
    if job_columns.get("game_id", {}).get("nullable", False):
        with op.batch_alter_table("publication_jobs") as batch:
            batch.alter_column(
                "game_id",
                existing_type=sa.String(36),
                nullable=False,
            )
