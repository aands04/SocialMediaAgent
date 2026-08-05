"""add the dedicated club reviewer role

Revision ID: 0021
Revises: 0020
"""

from sqlalchemy import text

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE role ADD VALUE IF NOT EXISTS 'REVIEWER'")


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    reviewer_count = bind.execute(
        text("SELECT count(*) FROM users WHERE role::text = 'REVIEWER'")
    ).scalar_one()
    if reviewer_count:
        raise RuntimeError(
            "Downgrade blockiert: REVIEWER-Benutzer müssen zuvor einer anderen Rolle zugeordnet werden"
        )
    op.execute("ALTER TABLE users ALTER COLUMN role DROP DEFAULT")
    op.execute("ALTER TYPE role RENAME TO role_with_reviewer")
    op.execute("CREATE TYPE role AS ENUM ('ADMIN', 'EDITOR', 'APPROVER', 'VIEWER')")
    op.execute(
        "ALTER TABLE users ALTER COLUMN role TYPE role "
        "USING role::text::role"
    )
    op.execute("DROP TYPE role_with_reviewer")
