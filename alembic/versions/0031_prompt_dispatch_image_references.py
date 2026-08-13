"""record image references sent with protected AI prompts

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("ai_prompt_dispatches")}
    if "reference_images" not in columns:
        op.add_column(
            "ai_prompt_dispatches",
            sa.Column(
                "reference_images",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
        )


def downgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("ai_prompt_dispatches")}
    if "reference_images" in columns:
        op.drop_column("ai_prompt_dispatches", "reference_images")
