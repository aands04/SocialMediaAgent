"""persistent generation jobs"""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "generation_jobs" in existing:
        return
    status = sa.Enum(
        "QUEUED",
        "RUNNING",
        "RETRY_WAIT",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "MANUAL_REVIEW_REQUIRED",
        name="generationjobstatus",
    )
    job_type = sa.Enum(
        "CREATE_POST",
        "RERENDER_POST",
        name="generationjobtype",
    )
    op.create_table(
        "generation_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_type", job_type, nullable=False),
        sa.Column("game_id", sa.String(36), sa.ForeignKey("games.id"), nullable=False),
        sa.Column("team_id", sa.String(36), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("post_id", sa.String(36), sa.ForeignKey("posts.id")),
        sa.Column("result_post_id", sa.String(36), sa.ForeignKey("posts.id")),
        sa.Column("post_type", sa.String(30), nullable=False),
        sa.Column("requested_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", status, nullable=False),
        sa.Column("phase", sa.String(40), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("planned_outputs", sa.Integer(), nullable=False),
        sa.Column("completed_outputs", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String(160)),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("error_category", sa.String(80)),
        sa.Column("error_message", sa.Text()),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("active_key", sa.String(255)),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("active_key"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_generation_jobs_job_type", "generation_jobs", ["job_type"])
    op.create_index("ix_generation_jobs_game_id", "generation_jobs", ["game_id"])
    op.create_index("ix_generation_jobs_team_id", "generation_jobs", ["team_id"])
    op.create_index("ix_generation_jobs_post_id", "generation_jobs", ["post_id"])
    op.create_index("ix_generation_jobs_status", "generation_jobs", ["status"])
    op.create_index("ix_generation_jobs_available_at", "generation_jobs", ["available_at"])
    op.create_index("ix_generation_jobs_lease_expires_at", "generation_jobs", ["lease_expires_at"])


def downgrade():
    if "generation_jobs" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("generation_jobs")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="generationjobstatus").drop(bind, checkfirst=True)
        sa.Enum(name="generationjobtype").drop(bind, checkfirst=True)
