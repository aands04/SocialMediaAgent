"""store exact AI provider prompts for PlatformAdmin observability

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade():
    # Migration 0001 historically builds the current SQLAlchemy metadata on a
    # completely fresh database.  In that case this table already exists by
    # the time Alembic reaches 0022.  Existing installations at 0021 do not
    # have it yet and still take the normal create path below.
    if "ai_prompt_dispatches" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "ai_prompt_dispatches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("club_id", sa.String(length=36), nullable=False),
        sa.Column("generation_job_id", sa.String(length=36), nullable=True),
        sa.Column("post_id", sa.String(length=36), nullable=True),
        sa.Column("team_id", sa.String(length=36), nullable=True),
        sa.Column("game_id", sa.String(length=36), nullable=True),
        sa.Column("prompt_kind", sa.String(length=20), nullable=False),
        sa.Column("post_type", sa.String(length=30), nullable=False),
        sa.Column("media_kind", sa.String(length=10), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("prompt_template_id", sa.String(length=36), nullable=True),
        sa.Column("prompt_name", sa.String(length=160), nullable=True),
        sa.Column("prompt_version", sa.Integer(), nullable=True),
        sa.Column("prompt_checksum", sa.String(length=64), nullable=False),
        sa.Column("rendered_prompt", sa.Text(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("call_index", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("error_summary", sa.String(length=500), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["generation_job_id"], ["generation_jobs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["prompt_template_id"], ["prompt_templates.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "club_id", "idempotency_key", name="uq_ai_prompt_dispatch_idempotency"
        ),
    )
    for column in (
        "club_id",
        "generation_job_id",
        "post_id",
        "team_id",
        "game_id",
        "prompt_kind",
        "post_type",
        "media_kind",
        "prompt_template_id",
        "prompt_checksum",
        "status",
        "dispatched_at",
    ):
        op.create_index(
            f"ix_ai_prompt_dispatches_{column}",
            "ai_prompt_dispatches",
            [column],
        )


def downgrade():
    op.drop_table("ai_prompt_dispatches")
