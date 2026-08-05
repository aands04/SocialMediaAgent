"""Protected prompt metadata and tenant-scoped uniqueness.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(bind).get_columns(table)}


def _unique_names(bind, table: str) -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(bind).get_unique_constraints(table)
        if item.get("name")
    }


def _foreign_key_names(bind, table: str) -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(bind).get_foreign_keys(table)
        if item.get("name")
    }


def upgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind, "prompt_templates")
    if "status" not in columns:
        prompt_status = sa.Enum("DRAFT", "ACTIVE", "ARCHIVED", name="promptstatus")
        with op.batch_alter_table("prompt_templates") as batch:
            batch.add_column(
                sa.Column("status", prompt_status, nullable=False, server_default="DRAFT")
            )
            batch.add_column(sa.Column("checksum", sa.String(64), nullable=True))
            batch.add_column(sa.Column("allowed_variables", sa.JSON(), nullable=True))
            batch.add_column(sa.Column("validation_rules", sa.JSON(), nullable=True))
            batch.add_column(sa.Column("created_by", sa.String(36), nullable=True))
            batch.add_column(sa.Column("change_description", sa.String(500), nullable=True))
            batch.add_column(sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True))
            batch.create_foreign_key(
                "fk_prompt_templates_created_by", "users", ["created_by"], ["id"]
            )
            batch.create_index("ix_prompt_templates_status", ["status"])
            batch.create_index("ix_prompt_templates_checksum", ["checksum"])
        rows = bind.execute(
            sa.text("SELECT id,prompt_body,active,archived_at FROM prompt_templates")
        ).mappings()
        for row in rows:
            body = str(row["prompt_body"])
            status = "ARCHIVED" if row["archived_at"] else ("ACTIVE" if row["active"] else "DRAFT")
            bind.execute(
                sa.text(
                    "UPDATE prompt_templates SET status=:status, checksum=:checksum, "
                    "allowed_variables=:variables, validation_rules=:rules "
                    "WHERE id=:id"
                ),
                {
                    "id": row["id"],
                    "status": status,
                    "checksum": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    "variables": "[]",
                    "rules": "{}",
                },
            )
        with op.batch_alter_table("prompt_templates") as batch:
            batch.alter_column("checksum", nullable=False)
            batch.alter_column("allowed_variables", nullable=False)
            batch.alter_column("validation_rules", nullable=False)


def downgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind, "prompt_templates")
    if "status" not in columns:
        return
    foreign_keys = _foreign_key_names(bind, "prompt_templates")
    with op.batch_alter_table("prompt_templates") as batch:
        batch.drop_index("ix_prompt_templates_checksum")
        batch.drop_index("ix_prompt_templates_status")
        if "fk_prompt_templates_created_by" in foreign_keys:
            batch.drop_constraint("fk_prompt_templates_created_by", type_="foreignkey")
        for column in (
            "activated_at",
            "change_description",
            "created_by",
            "validation_rules",
            "allowed_variables",
            "checksum",
            "status",
        ):
            batch.drop_column(column)
