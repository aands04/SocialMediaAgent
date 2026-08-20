"""encrypted tenant-bound FuPa browser sessions

Revision ID: 0034
Revises: 0033
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fupa_browser_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False),
        sa.Column("encrypted_storage_state", sa.Text()),
        sa.Column("key_version", sa.String(40), nullable=False, server_default="v1"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_category", sa.String(80)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_by", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("club_id", name="uq_fupa_browser_sessions_club"),
        sa.CheckConstraint(
            "status IN ('active','expired','revoked','error')",
            name="ck_fupa_browser_session_status",
        ),
    )
    op.create_index(
        "ix_fupa_browser_sessions_club_id",
        "fupa_browser_sessions",
        ["club_id"],
    )
    op.create_index(
        "ix_fupa_browser_sessions_status",
        "fupa_browser_sessions",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_fupa_browser_sessions_status", table_name="fupa_browser_sessions")
    op.drop_index("ix_fupa_browser_sessions_club_id", table_name="fupa_browser_sessions")
    op.drop_table("fupa_browser_sessions")
