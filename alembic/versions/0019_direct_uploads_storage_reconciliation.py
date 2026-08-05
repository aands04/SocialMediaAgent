"""Direct upload sessions and object-storage reconciliation runs.

Revision ID: 0019
Revises: 0018
"""

from alembic import op
from app.db import Base

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

TABLES = ("direct_upload_sessions", "storage_reconciliation_runs")


def upgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        Base.metadata.tables[table].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(TABLES):
        Base.metadata.tables[table].drop(bind=bind, checkfirst=True)
