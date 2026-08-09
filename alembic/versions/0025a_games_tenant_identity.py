"""Add the composite game identity required by tenant-safe foreign keys.

Revision ID: 0025a
Revises: 0025
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0025a"
down_revision = "0025"
branch_labels = None
depends_on = None

CONSTRAINT_NAME = "uq_games_id_club"
CONSTRAINT_COLUMNS = ("id", "club_id")
NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _matching_constraint(bind: sa.engine.Connection) -> dict | None:
    inspector = sa.inspect(bind)
    if "games" not in set(inspector.get_table_names()):
        return None
    return next(
        (
            constraint
            for constraint in inspector.get_unique_constraints("games")
            if tuple(constraint.get("column_names") or ()) == CONSTRAINT_COLUMNS
        ),
        None,
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "games" not in set(inspector.get_table_names()):
        return
    if _matching_constraint(bind) is not None:
        return
    with op.batch_alter_table("games", naming_convention=NAMING_CONVENTION) as batch:
        batch.create_unique_constraint(CONSTRAINT_NAME, list(CONSTRAINT_COLUMNS))


def downgrade() -> None:
    bind = op.get_bind()
    constraint = _matching_constraint(bind)
    if constraint is None:
        return
    with op.batch_alter_table("games", naming_convention=NAMING_CONVENTION) as batch:
        batch.drop_constraint(
            constraint.get("name") or CONSTRAINT_NAME,
            type_="unique",
        )
