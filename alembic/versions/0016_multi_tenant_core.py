"""multi-tenant club, account and plan foundation

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

import sqlalchemy as sa

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


TENANT_TABLES = (
    "instagram_pages",
    "teams",
    "games",
    "logo_assets",
    "media_assets",
    "story_rules",
    "posts",
    "publication_jobs",
    "publication_media_items",
    "generation_jobs",
    "instagram_connections",
    "instagram_oauth_states",
    "public_media_grants",
    "meta_publishing_attempts",
    "meta_carousel_items",
    "meta_publish_confirmations",
    "notifications",
    "font_assets",
    "design_templates",
    "provider_snapshots",
    "fussball_sync_states",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return normalized[:100] or "initialer-verein"


def _table_names(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _count(bind, table: str) -> int:
    return int(bind.execute(sa.text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one())


def _existing_club_names(bind, tables: set[str]) -> list[str]:
    names: dict[str, str] = {}
    for table in ("teams", "instagram_pages"):
        if table not in tables or "club" not in _columns(bind, table):
            continue
        rows = bind.execute(
            sa.text(f'SELECT DISTINCT club FROM "{table}" WHERE club IS NOT NULL')
        ).scalars()
        for raw in rows:
            value = str(raw).strip()
            if value:
                names.setdefault(value.casefold(), value)
    return sorted(names.values(), key=str.casefold)


def _initial_identity(bind, tables: set[str]) -> dict[str, str] | None:
    counts = {
        table: _count(bind, table)
        for table in tables
        if table in TENANT_TABLES or table in {"users", "audit_logs", "prompt_templates"}
    }
    if not any(counts.values()):
        return None

    found_names = _existing_club_names(bind, tables)
    configured_name = (os.getenv("INITIAL_CLUB_NAME") or "").strip()
    if not found_names and not configured_name:
        raise RuntimeError(
            "SaaS-Migration abgebrochen: vorhandene Daten besitzen keine eindeutig "
            "ableitbare Vereinszuordnung. INITIAL_CLUB_NAME, INITIAL_CLUB_SHORT_NAME "
            "und INITIAL_CLUB_SLUG sind erforderlich."
        )
    if len(found_names) > 1 and not configured_name:
        raise RuntimeError(
            "SaaS-Migration abgebrochen: vorhandene Daten enthalten mehrere "
            f"Vereinsnamen ({', '.join(found_names)}). INITIAL_CLUB_NAME, "
            "INITIAL_CLUB_SLUG und eine geprüfte Preflight-Zuordnung sind erforderlich."
        )
    if configured_name and found_names:
        unmatched = [name for name in found_names if name.casefold() != configured_name.casefold()]
        if unmatched:
            raise RuntimeError(
                "SaaS-Migration abgebrochen: INITIAL_CLUB_NAME widerspricht vorhandenen "
                f"Vereinsnamen ({', '.join(found_names)})."
            )

    name = configured_name or (found_names[0] if found_names else "Initialer Verein")
    slug = (os.getenv("INITIAL_CLUB_SLUG") or _slug(name)).strip()
    short_name = (os.getenv("INITIAL_CLUB_SHORT_NAME") or name[:60]).strip()
    configured_id = (os.getenv("INITIAL_CLUB_ID") or "").strip()
    try:
        club_id = (
            str(UUID(configured_id))
            if configured_id
            else str(uuid5(NAMESPACE_URL, f"social-media-agent:club:{slug}"))
        )
    except ValueError as exc:
        raise RuntimeError("INITIAL_CLUB_ID ist keine gültige UUID") from exc
    return {
        "id": club_id,
        "name": name,
        "short_name": short_name,
        "slug": slug,
        "counts": counts,
        "found_names": found_names,
    }


def _create_core_tables() -> None:
    op.create_table(
        "plan_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("max_teams", sa.Integer(), nullable=False),
        sa.Column("max_storage_bytes", sa.BigInteger(), nullable=False),
        sa.Column("monthly_ai_texts", sa.Integer(), nullable=False),
        sa.Column("monthly_ai_images", sa.Integer(), nullable=False),
        sa.Column("max_fonts", sa.Integer(), nullable=False),
        sa.Column("max_instagram_pages", sa.Integer(), nullable=False),
        sa.Column("trial_days", sa.Integer()),
        sa.Column("feature_flags", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("name", "version", name="uq_plan_profile_version"),
    )
    op.create_index("ix_plan_profiles_name", "plan_profiles", ["name"])
    op.create_index("ix_plan_profiles_active", "plan_profiles", ["active"])

    club_status = sa.Enum(
        "SETUP_PENDING",
        "TRIAL",
        "ACTIVE",
        "SUSPENDED",
        "CANCELLED",
        "ARCHIVED",
        name="clubstatus",
    )
    op.create_table(
        "clubs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(180), nullable=False),
        sa.Column("short_name", sa.String(60), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("logo_asset_id", sa.String(36)),
        sa.Column("status", club_status, nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("contact_name", sa.String(180)),
        sa.Column("contact_email", sa.String(255)),
        sa.Column("billing_details", sa.JSON(), nullable=False),
        sa.Column("contract_details", sa.JSON(), nullable=False),
        sa.Column("technical_settings", sa.JSON(), nullable=False),
        sa.Column("branding_settings", sa.JSON(), nullable=False),
        sa.Column(
            "plan_profile_id", sa.String(36), sa.ForeignKey("plan_profiles.id"), nullable=False
        ),
        sa.Column("limit_overrides", sa.JSON(), nullable=False),
        sa.Column("usage_snapshot", sa.JSON(), nullable=False),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("slug <> ''", name="ck_clubs_slug_not_empty"),
        sa.CheckConstraint("version > 0", name="ck_clubs_version_positive"),
    )
    op.create_index("ix_clubs_slug", "clubs", ["slug"])
    op.create_index("ix_clubs_status", "clubs", ["status"])
    op.create_index("ix_clubs_plan_profile_id", "clubs", ["plan_profile_id"])

    op.create_table(
        "club_additional_allowances",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "club_id", sa.String(36), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("limit_key", sa.String(60), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(240)),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_club_allowances_positive"),
        sa.UniqueConstraint(
            "club_id", "limit_key", "starts_at", "ends_at", name="uq_club_allowance_period"
        ),
    )
    op.create_index(
        "ix_club_additional_allowances_club_id", "club_additional_allowances", ["club_id"]
    )
    op.create_index(
        "ix_club_additional_allowances_limit_key", "club_additional_allowances", ["limit_key"]
    )

    op.create_table(
        "club_branding_configurations",
        sa.Column(
            "club_id",
            sa.String(36),
            sa.ForeignKey("clubs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("image_settings", sa.JSON(), nullable=False),
        sa.Column("text_settings", sa.JSON(), nullable=False),
        sa.Column("primary_font_id", sa.String(36)),
        sa.Column("secondary_font_id", sa.String(36)),
        sa.Column("updated_by", sa.String(36), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_table(
        "feature_flags",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), sa.ForeignKey("clubs.id", ondelete="CASCADE")),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.String(36), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("club_id", "key", name="uq_feature_flag_scope"),
    )
    op.create_index("ix_feature_flags_club_id", "feature_flags", ["club_id"])
    op.create_index("ix_feature_flags_key", "feature_flags", ["key"])

    op.create_table(
        "tenant_migration_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("migration_revision", sa.String(32), nullable=False, unique=True),
        sa.Column("club_id", sa.String(36), sa.ForeignKey("clubs.id")),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tenant_migration_reports_status", "tenant_migration_reports", ["status"])


def upgrade() -> None:
    bind = op.get_bind()
    tables = _table_names(bind)
    # Migration 0001 historically calls ``Base.metadata.create_all``.  On a
    # brand-new installation it therefore creates the *current* metadata,
    # including the tables and columns introduced by this revision.  Existing
    # installations at 0015 do not have them.  Do not modify 0001 retroactively;
    # record this case and make this revision a deliberate no-op instead.
    schema_preexisting = {
        "plan_profiles",
        "clubs",
        "tenant_migration_reports",
    }.issubset(tables) and "club_id" in _columns(bind, "users")
    if schema_preexisting:
        report = {
            "revision": revision,
            "mode": "fresh_schema_created_from_current_metadata",
            "note": "No legacy rows required tenant backfill.",
        }
        serialized = json.dumps(report, sort_keys=True, separators=(",", ":"))
        exists = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM tenant_migration_reports WHERE migration_revision=:revision"
            ),
            {"revision": revision},
        ).scalar_one()
        if not exists:
            bind.execute(
                sa.text(
                    "INSERT INTO tenant_migration_reports "
                    "(id,migration_revision,club_id,status,report,checksum,created_at) "
                    "VALUES (:id,:revision,NULL,'fresh_schema_preexisting',:report,:checksum,:now)"
                ),
                {
                    "id": str(uuid5(NAMESPACE_URL, "social-media-agent:migration:0016:fresh")),
                    "revision": revision,
                    "report": serialized,
                    "checksum": hashlib.sha256(serialized.encode()).hexdigest(),
                    "now": _now(),
                },
            )
        return

    identity = _initial_identity(bind, tables)
    _create_core_tables()

    now = _now()
    plan_id = str(uuid5(NAMESPACE_URL, "social-media-agent:plan:legacy-standard"))
    bind.execute(
        sa.text(
            "INSERT INTO plan_profiles "
            "(id,name,description,active,max_teams,max_storage_bytes,monthly_ai_texts,"
            "monthly_ai_images,max_fonts,max_instagram_pages,feature_flags,created_at,updated_at,version) "
            "VALUES (:id,:name,:description,:active,:max_teams,:storage,:texts,:images,:fonts,:pages,:flags,:now,:now,1)"
        ),
        {
            "id": plan_id,
            "name": "Legacy Standard",
            "description": "Automatisch für die bestehende Installation angelegtes Limitprofil",
            "active": True,
            "max_teams": 100,
            "storage": 1_099_511_627_776,
            "texts": 1_000_000,
            "images": 1_000_000,
            "fonts": 100,
            "pages": 100,
            "flags": json.dumps({"legacy_installation": True}),
            "now": now,
        },
    )

    if identity:
        bind.execute(
            sa.text(
                "INSERT INTO clubs "
                "(id,name,short_name,slug,status,activated_at,timezone,billing_details,"
                "contract_details,technical_settings,branding_settings,plan_profile_id,"
                "limit_overrides,usage_snapshot,created_at,updated_at,version) "
                "VALUES (:id,:name,:short_name,:slug,:status,:now,'Europe/Berlin',:empty,:empty,"
                ":technical,:empty,:plan,:empty,:empty,:now,:now,1)"
            ),
            {
                **identity,
                "status": "ACTIVE",
                "now": now,
                "empty": json.dumps({}),
                "technical": json.dumps({"migrated_from_single_tenant": True}),
                "plan": plan_id,
            },
        )

    account_type = sa.Enum("CLUB_USER", "PLATFORM_ADMIN", name="accounttype")
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("club_id", sa.String(36), nullable=True))
        batch.add_column(
            sa.Column("account_type", account_type, nullable=False, server_default="CLUB_USER")
        )
        batch.create_foreign_key(
            "fk_users_club_id", "clubs", ["club_id"], ["id"], ondelete="RESTRICT"
        )
        batch.create_index("ix_users_club_id", ["club_id"])
        batch.create_index("ix_users_account_type", ["account_type"])

    for table in TENANT_TABLES:
        if table not in tables:
            continue
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("club_id", sa.String(36), nullable=True))
            batch.create_foreign_key(
                f"fk_{table}_club_id", "clubs", ["club_id"], ["id"], ondelete="RESTRICT"
            )
            batch.create_index(f"ix_{table}_club_id", ["club_id"])

    with op.batch_alter_table("user_teams") as batch:
        batch.add_column(sa.Column("club_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_user_teams_club_id", "clubs", ["club_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_index("ix_user_teams_club_id", ["club_id"])
    with op.batch_alter_table("audit_logs") as batch:
        batch.add_column(sa.Column("club_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("scope", sa.String(20), nullable=False, server_default="club"))
        batch.create_foreign_key(
            "fk_audit_logs_club_id", "clubs", ["club_id"], ["id"], ondelete="RESTRICT"
        )
        batch.create_index("ix_audit_logs_club_id", ["club_id"])
        batch.create_index("ix_audit_logs_scope", ["scope"])
    with op.batch_alter_table("prompt_templates") as batch:
        batch.add_column(sa.Column("source_club_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_prompt_templates_source_club", "clubs", ["source_club_id"], ["id"]
        )
        batch.create_index("ix_prompt_templates_source_club_id", ["source_club_id"])

    if identity:
        club_id = identity["id"]
        bind.execute(
            sa.text("UPDATE users SET club_id=:club, account_type='CLUB_USER'"), {"club": club_id}
        )
        for table in TENANT_TABLES:
            if table in tables:
                bind.execute(sa.text(f'UPDATE "{table}" SET club_id=:club'), {"club": club_id})
        bind.execute(sa.text("UPDATE user_teams SET club_id=:club"), {"club": club_id})
        bind.execute(
            sa.text("UPDATE audit_logs SET club_id=:club, scope='club'"), {"club": club_id}
        )
        bind.execute(sa.text("UPDATE prompt_templates SET source_club_id=:club"), {"club": club_id})

        report = {
            "revision": revision,
            "club": {key: identity[key] for key in ("id", "name", "short_name", "slug")},
            "found_club_names": identity["found_names"],
            "counts": identity["counts"],
            "classification": {
                "prompt_templates": "central_with_source_club_reference",
                "system_settings": "platform_safety_settings",
                "audit_logs": "initial_club",
            },
        }
        serialized = json.dumps(report, sort_keys=True, separators=(",", ":"))
        bind.execute(
            sa.text(
                "INSERT INTO tenant_migration_reports "
                "(id,migration_revision,club_id,status,report,checksum,created_at) "
                "VALUES (:id,:revision,:club,'completed',:report,:checksum,:now)"
            ),
            {
                "id": str(uuid5(NAMESPACE_URL, "social-media-agent:migration:0016")),
                "revision": revision,
                "club": club_id,
                "report": serialized,
                "checksum": hashlib.sha256(serialized.encode()).hexdigest(),
                "now": now,
            },
        )

    # club_id intentionally remains nullable on users: PlatformAdmin accounts
    # have no club.  The following check constraint enforces the exact XOR.
    required_tables = (*TENANT_TABLES, "user_teams")
    for table in required_tables:
        if table not in _table_names(bind):
            continue
        if (
            _count(bind, table)
            and bind.execute(
                sa.text(f'SELECT COUNT(*) FROM "{table}" WHERE club_id IS NULL')
            ).scalar_one()
        ):
            raise RuntimeError(
                f"SaaS-Migration abgebrochen: {table} enthält Datensätze ohne Verein"
            )
        with op.batch_alter_table(table) as batch:
            batch.alter_column("club_id", existing_type=sa.String(36), nullable=False)

    with op.batch_alter_table("users") as batch:
        batch.create_check_constraint(
            "ck_users_account_tenant",
            "(account_type = 'CLUB_USER' AND club_id IS NOT NULL) OR "
            "(account_type = 'PLATFORM_ADMIN' AND club_id IS NULL)",
        )
        batch.create_unique_constraint("uq_users_id_club", ["id", "club_id"])
    with op.batch_alter_table("teams") as batch:
        batch.create_unique_constraint("uq_teams_id_club", ["id", "club_id"])
    with op.batch_alter_table("instagram_pages") as batch:
        batch.create_unique_constraint("uq_instagram_pages_id_club", ["id", "club_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = _table_names(bind)
    if "tenant_migration_reports" in tables:
        mode = bind.execute(
            sa.text(
                "SELECT status FROM tenant_migration_reports WHERE migration_revision=:revision"
            ),
            {"revision": revision},
        ).scalar_one_or_none()
        if mode == "fresh_schema_preexisting":
            # See upgrade(): on fresh databases these objects belong to the
            # schema created by 0001/current metadata, not to this revision.
            return

    with op.batch_alter_table("prompt_templates") as batch:
        batch.drop_index("ix_prompt_templates_source_club_id")
        batch.drop_constraint("fk_prompt_templates_source_club", type_="foreignkey")
        batch.drop_column("source_club_id")
    with op.batch_alter_table("audit_logs") as batch:
        batch.drop_index("ix_audit_logs_scope")
        batch.drop_index("ix_audit_logs_club_id")
        batch.drop_constraint("fk_audit_logs_club_id", type_="foreignkey")
        batch.drop_column("scope")
        batch.drop_column("club_id")
    with op.batch_alter_table("user_teams") as batch:
        batch.drop_index("ix_user_teams_club_id")
        batch.drop_constraint("fk_user_teams_club_id", type_="foreignkey")
        batch.drop_column("club_id")
    for table in reversed(TENANT_TABLES):
        if table not in tables:
            continue
        with op.batch_alter_table(table) as batch:
            batch.drop_index(f"ix_{table}_club_id")
            batch.drop_constraint(f"fk_{table}_club_id", type_="foreignkey")
            batch.drop_column("club_id")
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("uq_users_id_club", type_="unique")
        batch.drop_constraint("ck_users_account_tenant", type_="check")
        batch.drop_index("ix_users_account_type")
        batch.drop_index("ix_users_club_id")
        batch.drop_constraint("fk_users_club_id", type_="foreignkey")
        batch.drop_column("account_type")
        batch.drop_column("club_id")
    op.drop_table("tenant_migration_reports")
    op.drop_table("feature_flags")
    op.drop_table("club_branding_configurations")
    op.drop_table("club_additional_allowances")
    op.drop_table("clubs")
    op.drop_table("plan_profiles")
