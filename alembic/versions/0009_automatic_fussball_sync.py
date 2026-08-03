"""persistent automatic FUSSBALL.DE synchronization state"""

import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade():
    # Migration 0001 builds current metadata for new installations. Existing
    # installations receive the new table here.
    if "fussball_sync_states" in _tables():
        return
    op.create_table(
        "fussball_sync_states",
        sa.Column(
            "team_id",
            sa.String(length=36),
            sa.ForeignKey("teams.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=160)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_started_at", sa.DateTime(timezone=True)),
        sa.Column("last_completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column(
            "last_snapshot_id",
            sa.String(length=36),
            sa.ForeignKey("provider_snapshots.id", ondelete="SET NULL"),
        ),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_result_scan_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_fussball_sync_states_status", "fussball_sync_states", ["status"]
    )
    op.create_index(
        "ix_fussball_sync_states_next_poll_at",
        "fussball_sync_states",
        ["next_poll_at"],
    )
    op.create_index(
        "ix_fussball_sync_states_lease_expires_at",
        "fussball_sync_states",
        ["lease_expires_at"],
    )


def downgrade():
    if "fussball_sync_states" in _tables():
        op.drop_table("fussball_sync_states")
