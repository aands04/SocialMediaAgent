"""single-use password reset tokens and session invalidation version"""

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    if table not in _tables():
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    if "auth_version" not in _columns("users"):
        with op.batch_alter_table("users") as batch:
            batch.add_column(
                sa.Column(
                    "auth_version",
                    sa.Integer(),
                    nullable=False,
                    server_default="1",
                )
            )
    if "password_reset_tokens" not in _tables():
        op.create_table(
            "password_reset_tokens",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True)),
            sa.Column("requested_ip", sa.String(80)),
            sa.Column("delivery_status", sa.String(20), nullable=False),
            sa.Column("delivery_error", sa.String(160)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"]
        )
        op.create_index(
            "ix_password_reset_tokens_token_hash",
            "password_reset_tokens",
            ["token_hash"],
            unique=True,
        )
        op.create_index(
            "ix_password_reset_tokens_expires_at",
            "password_reset_tokens",
            ["expires_at"],
        )
        op.create_index(
            "ix_password_reset_tokens_created_at",
            "password_reset_tokens",
            ["created_at"],
        )


def downgrade():
    if "password_reset_tokens" in _tables():
        op.drop_table("password_reset_tokens")
    if "auth_version" in _columns("users"):
        with op.batch_alter_table("users") as batch:
            batch.drop_column("auth_version")
