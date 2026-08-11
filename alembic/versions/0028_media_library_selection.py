"""visual media library, usage policies and game media preferences

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


CONTRIBUTION_TYPES = ("announcement", "reminder", "result", "live")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    media_columns = (
        {item["name"] for item in inspector.get_columns("media_assets")}
        if "media_assets" in tables
        else set()
    )
    schema_preexisting = {
        "media_usage_history",
        "club_media_usage_policies",
        "game_media_preferences",
    }.issubset(tables) and "media_category" in media_columns
    if schema_preexisting:
        # Revision 0001 deliberately builds current metadata on a brand-new
        # database. Do not try to add the same structures again.
        _insert_default_policies(bind)
        if "tenant_migration_reports" in tables:
            report = {
                "revision": revision,
                "mode": "fresh_schema_created_from_current_metadata",
                "note": "No legacy media rows required structural migration.",
            }
            serialized = json.dumps(report, sort_keys=True, separators=(",", ":"))
            exists = bind.execute(
                sa.text(
                    "SELECT COUNT(*) FROM tenant_migration_reports "
                    "WHERE migration_revision=:revision"
                ),
                {"revision": revision},
            ).scalar_one()
            if not exists:
                bind.execute(
                    sa.text(
                        "INSERT INTO tenant_migration_reports "
                        "(id,migration_revision,club_id,status,report,checksum,created_at) "
                        "VALUES (:id,:revision,NULL,'fresh_schema_preexisting',"
                        ":report,:checksum,:now)"
                    ),
                    {
                        "id": str(uuid4()),
                        "revision": revision,
                        "report": serialized,
                        "checksum": hashlib.sha256(serialized.encode()).hexdigest(),
                        "now": _now(),
                    },
                )
        return
    if bind.dialect.name != "sqlite":
        reserved_unique = next(
            (
                item.get("name")
                for item in sa.inspect(bind).get_unique_constraints("media_assets")
                if item.get("column_names") == ["reserved_game_id"] and item.get("name")
            ),
            None,
        )
        if reserved_unique:
            op.drop_constraint(reserved_unique, "media_assets", type_="unique")
    with op.batch_alter_table("media_assets") as batch:
        batch.add_column(
            sa.Column(
                "media_category",
                sa.String(30),
                nullable=False,
                server_default="match_photo",
            )
        )
        batch.add_column(sa.Column("game_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("description", sa.String(500), nullable=True))
        batch.add_column(sa.Column("width", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("height", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("photographer", sa.String(160), nullable=True))
        batch.add_column(sa.Column("uploaded_by", sa.String(36), nullable=True))
        batch.add_column(
            sa.Column(
                "automatic_usage_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_unique_constraint("uq_media_assets_id_club", ["id", "club_id"])
        batch.create_check_constraint(
            "ck_media_assets_category",
            "media_category IN ('match_photo', 'player_portrait', 'team_photo')",
        )
        batch.create_foreign_key(
            "fk_media_assets_game", "games", ["game_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_foreign_key(
            "fk_media_assets_team_club",
            "teams",
            ["team_id", "club_id"],
            ["id", "club_id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_media_assets_uploaded_by",
            "users",
            ["uploaded_by"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_media_assets_media_category", ["media_category"])
        batch.create_index("ix_media_assets_game_id", ["game_id"])
        batch.create_index("ix_media_assets_captured_at", ["captured_at"])
        batch.create_index("ix_media_assets_uploaded_by", ["uploaded_by"])
        batch.create_index("ix_media_assets_automatic_usage_enabled", ["automatic_usage_enabled"])
        batch.create_index("ix_media_assets_deleted_at", ["deleted_at"])
        batch.create_index("ix_media_assets_reserved_game_id", ["reserved_game_id"])

    # Existing assets were all uploaded through the former player/match image
    # workflow.  No file is moved or deleted during this safe classification.
    op.execute(
        sa.text(
            "UPDATE media_assets SET media_category = 'match_photo', "
            "automatic_usage_enabled = CASE WHEN uses > 0 THEN false ELSE true END"
        )
    )

    op.create_table(
        "media_usage_history",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("media_asset_id", sa.String(36), nullable=False, index=True),
        sa.Column("team_id", sa.String(36), nullable=False, index=True),
        sa.Column("game_id", sa.String(36), nullable=True, index=True),
        sa.Column("post_id", sa.String(36), nullable=True, index=True),
        sa.Column("contribution_type", sa.String(30), nullable=True, index=True),
        sa.Column("action", sa.String(30), nullable=False, index=True),
        sa.Column("actor_user_id", sa.String(36), nullable=True, index=True),
        sa.Column("details", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.CheckConstraint(
            "action IN ('reserved', 'reservation_released', 'used', 'released', 'manual_reuse', "
            "'automatic_excluded', 'automatic_enabled', 'soft_deleted')",
            name="ck_media_usage_history_action",
        ),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["media_asset_id", "club_id"],
            ["media_assets.id", "media_assets.club_id"],
            ondelete="RESTRICT",
            name="fk_media_usage_history_asset_club",
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "club_id"],
            ["teams.id", "teams.club_id"],
            ondelete="RESTRICT",
            name="fk_media_usage_history_team_club",
        ),
        sa.ForeignKeyConstraint(
            ["game_id", "club_id"],
            ["games.id", "games.club_id"],
            ondelete="RESTRICT",
            name="fk_media_usage_history_game_club",
        ),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_table(
        "club_media_usage_policies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("contribution_type", sa.String(30), nullable=False, index=True),
        sa.Column("allowed_media_categories", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("category_priority", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "contribution_type IN ('announcement', 'reminder', 'result', 'live')",
            name="ck_club_media_policy_contribution_type",
        ),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "club_id", "contribution_type", name="uq_club_media_policy_contribution"
        ),
    )
    op.create_table(
        "game_media_preferences",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("game_id", sa.String(36), nullable=False, index=True),
        sa.Column("team_id", sa.String(36), nullable=False, index=True),
        sa.Column("contribution_type", sa.String(30), nullable=False, index=True),
        sa.Column("selection_mode", sa.String(20), nullable=False, server_default="automatic"),
        sa.Column("selected_media_asset_id", sa.String(36), nullable=True, index=True),
        sa.Column("allow_used_once", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("selected_by", sa.String(36), nullable=True),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "selection_mode IN ('automatic', 'manual')",
            name="ck_game_media_preference_mode",
        ),
        sa.CheckConstraint(
            "contribution_type IN ('announcement', 'reminder', 'result', 'live')",
            name="ck_game_media_preference_contribution_type",
        ),
        sa.CheckConstraint(
            "selection_mode = 'automatic' OR selected_media_asset_id IS NOT NULL",
            name="ck_game_media_preference_manual_asset",
        ),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["game_id", "club_id"],
            ["games.id", "games.club_id"],
            ondelete="CASCADE",
            name="fk_game_media_preference_game_club",
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "club_id"],
            ["teams.id", "teams.club_id"],
            ondelete="RESTRICT",
            name="fk_game_media_preference_team_club",
        ),
        sa.ForeignKeyConstraint(
            ["selected_media_asset_id", "club_id"],
            ["media_assets.id", "media_assets.club_id"],
            ondelete="RESTRICT",
            name="fk_game_media_preference_asset_club",
        ),
        sa.ForeignKeyConstraint(["selected_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "club_id",
            "game_id",
            "team_id",
            "contribution_type",
            name="uq_game_media_preference_scope",
        ),
    )

    _insert_default_policies(bind)


def _insert_default_policies(bind) -> None:
    clubs = bind.execute(sa.text("SELECT id FROM clubs")).scalars().all()
    timestamp = _now()
    policy_table = sa.table(
        "club_media_usage_policies",
        sa.column("id", sa.String),
        sa.column("club_id", sa.String),
        sa.column("contribution_type", sa.String),
        sa.column("allowed_media_categories", sa.JSON),
        sa.column("category_priority", sa.JSON),
        sa.column("active", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("version", sa.Integer),
    )
    rows = []
    for club_id in clubs:
        for contribution_type in CONTRIBUTION_TYPES:
            categories = (
                ["player_portrait", "match_photo"]
                if contribution_type == "live"
                else ["match_photo"]
            )
            rows.append(
                {
                    "id": str(uuid4()),
                    "club_id": club_id,
                    "contribution_type": contribution_type,
                    "allowed_media_categories": categories,
                    "category_priority": categories,
                    "active": True,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "version": 1,
                }
            )
    if rows:
        existing = {
            (row[0], row[1])
            for row in bind.execute(
                sa.text("SELECT club_id, contribution_type FROM club_media_usage_policies")
            )
        }
        missing = [
            row for row in rows if (row["club_id"], row["contribution_type"]) not in existing
        ]
        if missing:
            op.bulk_insert(policy_table, missing)


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "tenant_migration_reports" in tables:
        mode = bind.execute(
            sa.text(
                "SELECT status FROM tenant_migration_reports WHERE migration_revision=:revision"
            ),
            {"revision": revision},
        ).scalar_one_or_none()
        if mode == "fresh_schema_preexisting":
            return
    op.drop_table("game_media_preferences")
    op.drop_table("club_media_usage_policies")
    op.drop_table("media_usage_history")
    with op.batch_alter_table("media_assets") as batch:
        batch.drop_index("ix_media_assets_reserved_game_id")
        batch.drop_index("ix_media_assets_deleted_at")
        batch.drop_index("ix_media_assets_automatic_usage_enabled")
        batch.drop_index("ix_media_assets_uploaded_by")
        batch.drop_index("ix_media_assets_captured_at")
        batch.drop_index("ix_media_assets_game_id")
        batch.drop_index("ix_media_assets_media_category")
        batch.drop_constraint("fk_media_assets_uploaded_by", type_="foreignkey")
        batch.drop_constraint("fk_media_assets_team_club", type_="foreignkey")
        batch.drop_constraint("fk_media_assets_game", type_="foreignkey")
        batch.drop_constraint("ck_media_assets_category", type_="check")
        batch.drop_constraint("uq_media_assets_id_club", type_="unique")
        batch.drop_column("deleted_at")
        batch.drop_column("automatic_usage_enabled")
        batch.drop_column("uploaded_by")
        batch.drop_column("photographer")
        batch.drop_column("captured_at")
        batch.drop_column("description")
        batch.drop_column("height")
        batch.drop_column("width")
        batch.drop_column("game_id")
        batch.drop_column("media_category")
        batch.create_unique_constraint("uq_media_assets_reserved_game_id", ["reserved_game_id"])
