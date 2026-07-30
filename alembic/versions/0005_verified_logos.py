"""verified and versioned club logos"""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _foreign_key_name(table: str, column: str) -> str | None:
    for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys(table):
        if column in foreign_key.get("constrained_columns", []):
            return foreign_key.get("name")
    return None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "logo_assets" not in inspector.get_table_names():
        op.create_table(
            "logo_assets",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("logo_type", sa.String(20), nullable=False),
            sa.Column("team_id", sa.String(36), sa.ForeignKey("teams.id")),
            sa.Column("display_name", sa.String(200), nullable=False),
            sa.Column("normalized_name", sa.String(200), nullable=False),
            sa.Column("original_path", sa.String(800), nullable=False, unique=True),
            sa.Column("render_path", sa.String(800), unique=True),
            sa.Column("original_filename", sa.String(255), nullable=False),
            sa.Column("mime_type", sa.String(80), nullable=False),
            sa.Column("size", sa.Integer(), nullable=False),
            sa.Column("width", sa.Integer(), nullable=False),
            sa.Column("height", sa.Integer(), nullable=False),
            sa.Column("checksum", sa.String(64), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
            sa.Column("uploaded_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.UniqueConstraint("logo_type", "team_id", "normalized_name", "version"),
        )
        op.create_index("ix_logo_assets_logo_type", "logo_assets", ["logo_type"])
        op.create_index("ix_logo_assets_team_id", "logo_assets", ["team_id"])
        op.create_index("ix_logo_assets_normalized_name", "logo_assets", ["normalized_name"])
        op.create_index("ix_logo_assets_checksum", "logo_assets", ["checksum"])
        op.create_index(
            "uq_logo_assets_team_checksum",
            "logo_assets",
            ["team_id", "checksum"],
            unique=True,
            postgresql_where=sa.text("logo_type = 'team'"),
            sqlite_where=sa.text("logo_type = 'team'"),
        )
        op.create_index(
            "uq_logo_assets_opponent_checksum",
            "logo_assets",
            ["checksum"],
            unique=True,
            postgresql_where=sa.text("logo_type = 'opponent'"),
            sqlite_where=sa.text("logo_type = 'opponent'"),
        )
    if "logo_asset_id" not in _columns("teams"):
        with op.batch_alter_table("teams") as batch:
            batch.add_column(sa.Column("logo_asset_id", sa.String(36)))
            batch.create_foreign_key(
                "fk_teams_logo_asset_id_logo_assets", "logo_assets", ["logo_asset_id"], ["id"]
            )
    if "opponent_logo_id" not in _columns("games"):
        with op.batch_alter_table("games") as batch:
            batch.add_column(sa.Column("opponent_logo_id", sa.String(36)))
            batch.create_foreign_key(
                "fk_games_opponent_logo_id_logo_assets",
                "logo_assets",
                ["opponent_logo_id"],
                ["id"],
            )


def downgrade():
    if "opponent_logo_id" in _columns("games"):
        constraint = _foreign_key_name("games", "opponent_logo_id")
        with op.batch_alter_table("games") as batch:
            if constraint:
                batch.drop_constraint(constraint, type_="foreignkey")
            batch.drop_column("opponent_logo_id")
    if "logo_asset_id" in _columns("teams"):
        constraint = _foreign_key_name("teams", "logo_asset_id")
        with op.batch_alter_table("teams") as batch:
            if constraint:
                batch.drop_constraint(constraint, type_="foreignkey")
            batch.drop_column("logo_asset_id")
    if "logo_assets" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("logo_assets")
