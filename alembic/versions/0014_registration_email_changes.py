"""self-registration approvals and confirmed email changes"""

import sqlalchemy as sa

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    if table not in _tables():
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    columns = _columns("users")
    with op.batch_alter_table("users") as batch:
        if "registration_status" not in columns:
            batch.add_column(
                sa.Column(
                    "registration_status",
                    sa.String(20),
                    nullable=False,
                    server_default="approved",
                )
            )
        if "registration_requested_at" not in columns:
            batch.add_column(sa.Column("registration_requested_at", sa.DateTime(timezone=True)))
        if "registration_reviewed_at" not in columns:
            batch.add_column(sa.Column("registration_reviewed_at", sa.DateTime(timezone=True)))
        if "registration_reviewed_by" not in columns:
            batch.add_column(
                sa.Column(
                    "registration_reviewed_by",
                    sa.String(36),
                    sa.ForeignKey(
                        "users.id",
                        name="fk_users_registration_reviewed_by",
                        ondelete="SET NULL",
                    ),
                )
            )
    if "ix_users_registration_status" not in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes("users")
    }:
        op.create_index("ix_users_registration_status", "users", ["registration_status"])

    if "email_change_tokens" not in _tables():
        op.create_table(
            "email_change_tokens",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("old_email", sa.String(255), nullable=False),
            sa.Column("new_email", sa.String(255), nullable=False),
            sa.Column("auth_version", sa.Integer(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True)),
            sa.Column("requested_ip", sa.String(80)),
            sa.Column("delivery_status", sa.String(20), nullable=False),
            sa.Column("delivery_error", sa.String(160)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_email_change_tokens_user_id", "email_change_tokens", ["user_id"]
        )
        op.create_index(
            "ix_email_change_tokens_token_hash",
            "email_change_tokens",
            ["token_hash"],
            unique=True,
        )
        op.create_index(
            "ix_email_change_tokens_new_email", "email_change_tokens", ["new_email"]
        )
        op.create_index(
            "ix_email_change_tokens_expires_at", "email_change_tokens", ["expires_at"]
        )
        op.create_index(
            "ix_email_change_tokens_created_at", "email_change_tokens", ["created_at"]
        )


def downgrade():
    if "email_change_tokens" in _tables():
        op.drop_table("email_change_tokens")
    if "ix_users_registration_status" in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes("users")
    }:
        op.drop_index("ix_users_registration_status", table_name="users")
    columns = _columns("users")
    with op.batch_alter_table("users") as batch:
        if "registration_reviewed_by" in columns:
            batch.drop_column("registration_reviewed_by")
        if "registration_reviewed_at" in columns:
            batch.drop_column("registration_reviewed_at")
        if "registration_requested_at" in columns:
            batch.drop_column("registration_requested_at")
        if "registration_status" in columns:
            batch.drop_column("registration_status")
