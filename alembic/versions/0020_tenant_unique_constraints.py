"""Replace legacy global uniqueness with tenant-scoped constraints.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}

REPLACEMENTS = (
    ("teams", ("slug",), ("club_id", "slug"), "uq_teams_club_slug"),
    (
        "instagram_pages",
        (),
        ("club_id", "username"),
        "uq_instagram_pages_club_username",
    ),
    (
        "games",
        ("team_id", "provider", "external_id"),
        ("club_id", "team_id", "provider", "external_id"),
        "uq_games_club_provider_external",
    ),
    (
        "logo_assets",
        ("logo_type", "team_id", "normalized_name", "version"),
        ("club_id", "logo_type", "team_id", "normalized_name", "version"),
        "uq_logo_assets_club_version",
    ),
    (
        "media_assets",
        ("team_id", "relative_path"),
        ("club_id", "team_id", "relative_path"),
        "uq_media_assets_club_team_path",
    ),
    (
        "story_rules",
        ("team_id", "name"),
        ("club_id", "team_id", "name"),
        "uq_story_rules_club_team_name",
    ),
    (
        "posts",
        ("game_id", "post_type", "active_key"),
        ("club_id", "game_id", "post_type", "active_key"),
        "uq_posts_club_game_type_active",
    ),
    (
        "posts",
        ("manual_submission_id",),
        ("club_id", "manual_submission_id"),
        "uq_posts_club_manual_submission_id",
    ),
    (
        "publication_jobs",
        ("idempotency_key",),
        ("club_id", "idempotency_key"),
        "uq_publication_jobs_club_idempotency",
    ),
    (
        "generation_jobs",
        ("active_key",),
        ("club_id", "active_key"),
        "uq_generation_jobs_club_active_key",
    ),
    (
        "generation_jobs",
        ("idempotency_key",),
        ("club_id", "idempotency_key"),
        "uq_generation_jobs_club_idempotency",
    ),
    (
        "public_media_grants",
        ("active_key",),
        ("club_id", "active_key"),
        "uq_public_media_grants_club_active",
    ),
    (
        "meta_publishing_attempts",
        ("active_key",),
        ("club_id", "active_key"),
        "uq_meta_attempts_club_active",
    ),
    (
        "font_assets",
        ("name",),
        ("club_id", "name"),
        "uq_font_assets_club_name",
    ),
    (
        "font_assets",
        ("relative_path",),
        ("club_id", "relative_path"),
        "uq_font_assets_club_path",
    ),
    (
        "design_templates",
        ("name", "version"),
        ("club_id", "name", "version"),
        "uq_design_templates_club_version",
    ),
    (
        "provider_snapshots",
        ("relative_path",),
        ("club_id", "relative_path"),
        "uq_provider_snapshots_club_path",
    ),
)


def _unique_constraints(bind, table: str) -> list[dict]:
    return list(sa.inspect(bind).get_unique_constraints(table))


def _drop_unique(bind, table: str, columns: tuple[str, ...]) -> None:
    if not columns:
        return
    for constraint in _unique_constraints(bind, table):
        if tuple(constraint.get("column_names") or ()) != columns:
            continue
        name = constraint.get("name")
        with op.batch_alter_table(table, naming_convention=NAMING_CONVENTION) as batch:
            batch.drop_constraint(name or f"uq_{table}_{columns[0]}", type_="unique")
        return


def _ensure_unique(bind, table: str, columns: tuple[str, ...], name: str) -> None:
    if any(
        tuple(item.get("column_names") or ()) == columns
        for item in _unique_constraints(bind, table)
    ):
        return
    with op.batch_alter_table(table, naming_convention=NAMING_CONVENTION) as batch:
        batch.create_unique_constraint(name, list(columns))


def _replace_logo_checksum_indexes(bind) -> None:
    indexes = {item["name"]: item for item in sa.inspect(bind).get_indexes("logo_assets")}
    for name in ("uq_logo_assets_team_checksum", "uq_logo_assets_opponent_checksum"):
        if name in indexes:
            op.drop_index(name, table_name="logo_assets")
    op.create_index(
        "uq_logo_assets_team_checksum",
        "logo_assets",
        ["team_id", "club_id", "checksum"],
        unique=True,
        postgresql_where=sa.text("logo_type = 'team'"),
        sqlite_where=sa.text("logo_type = 'team'"),
    )
    op.create_index(
        "uq_logo_assets_opponent_checksum",
        "logo_assets",
        ["club_id", "checksum"],
        unique=True,
        postgresql_where=sa.text("logo_type = 'opponent'"),
        sqlite_where=sa.text("logo_type = 'opponent'"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table, legacy, tenant, name in REPLACEMENTS:
        if table not in tables:
            continue
        if legacy and legacy != tenant:
            _drop_unique(bind, table, legacy)
        _ensure_unique(bind, table, tenant, name)
    if "logo_assets" in tables:
        _replace_logo_checksum_indexes(bind)
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_feature_flags_platform_key "
            "ON feature_flags (key) WHERE club_id IS NULL"
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS uq_feature_flags_platform_key")
    for table, legacy, tenant, _name in reversed(REPLACEMENTS):
        if table not in tables:
            continue
        _drop_unique(bind, table, tenant)
        if legacy:
            _ensure_unique(bind, table, legacy, f"uq_{table}_{legacy[0]}")
