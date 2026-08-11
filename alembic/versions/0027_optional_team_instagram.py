"""allow teams without a legacy Instagram page

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.alter_column(
            "instagram_page_id",
            existing_type=sa.String(36),
            nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    missing = bind.execute(
        sa.text("SELECT COUNT(*) FROM teams WHERE instagram_page_id IS NULL")
    ).scalar_one()
    if missing:
        raise RuntimeError(
            "Downgrade blockiert: Mannschaften ohne Instagram-Seite müssen zuerst "
            "einer Legacy-Instagram-Seite zugeordnet werden."
        )
    with op.batch_alter_table("teams") as batch:
        batch.alter_column(
            "instagram_page_id",
            existing_type=sa.String(36),
            nullable=False,
        )
