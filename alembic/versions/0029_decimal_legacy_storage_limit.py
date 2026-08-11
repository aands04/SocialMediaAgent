"""normalize the legacy storage quota to decimal gigabytes

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


LEGACY_BINARY_LIMIT = 1_099_511_627_776
DECIMAL_LIMIT = 1_000_000_000_000


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE plan_profiles "
            "SET max_storage_bytes=:decimal_limit, version=version+1 "
            "WHERE name='Legacy Standard' AND max_storage_bytes=:binary_limit"
        ),
        {
            "decimal_limit": DECIMAL_LIMIT,
            "binary_limit": LEGACY_BINARY_LIMIT,
        },
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE plan_profiles "
            "SET max_storage_bytes=:binary_limit, version=version+1 "
            "WHERE name='Legacy Standard' AND max_storage_bytes=:decimal_limit"
        ),
        {
            "decimal_limit": DECIMAL_LIMIT,
            "binary_limit": LEGACY_BINARY_LIMIT,
        },
    )
