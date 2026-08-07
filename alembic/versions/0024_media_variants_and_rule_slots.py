"""immutable media variants, text versions and structured publication rules

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def _uid() -> str:
    return str(uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _file_metadata(path_value: str | None) -> dict:
    path_value = path_value or ""
    path = Path(path_value)
    try:
        payload = path.read_bytes()
    except OSError:
        payload = b""
    return {
        "checksum": hashlib.sha256(payload or path_value.encode("utf-8")).hexdigest(),
        "file_size": len(payload),
        "mime_type": "image/png",
        "width": 0,
        "height": 0,
        "validation_status": "valid" if payload else "legacy_unverified",
    }


def _stamp() -> dict:
    current = _now()
    return {"created_at": current, "updated_at": current, "version": 1}


def _create_tables() -> None:
    op.create_table(
        "content_rule_sets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("scope_type", sa.String(10), nullable=False),
        sa.Column("scope_key", sa.String(80), nullable=False, index=True),
        sa.Column("team_id", sa.String(36), nullable=True, index=True),
        sa.Column("game_id", sa.String(36), nullable=True, index=True),
        sa.Column("post_type", sa.String(30), nullable=False, index=True),
        sa.Column("rule_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("feed_generation_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("story_generation_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("feed_publish_variants", sa.JSON(), nullable=False, server_default="[1]"),
        sa.Column("story_publish_variants", sa.JSON(), nullable=False, server_default="[1]"),
        sa.Column("approval_policy", sa.String(30), nullable=False, server_default="manual"),
        sa.Column("inherited_from_id", sa.String(36), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("scope_type IN ('club', 'team', 'game')", name="ck_content_rule_scope"),
        sa.CheckConstraint(
            "feed_generation_count >= 0 AND feed_generation_count <= 10",
            name="ck_content_rule_feed_count",
        ),
        sa.CheckConstraint(
            "story_generation_count >= 0 AND story_generation_count <= 10",
            name="ck_content_rule_story_count",
        ),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["inherited_from_id"], ["content_rule_sets.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "club_id",
            "scope_key",
            "post_type",
            "rule_version",
            name="uq_content_rule_set_scope_version",
        ),
    )
    op.create_table(
        "publication_rule_slots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("rule_set_id", sa.String(36), nullable=False, index=True),
        sa.Column("slot_key", sa.String(100), nullable=False),
        sa.Column("label", sa.String(160), nullable=False),
        sa.Column("media_kind", sa.String(10), nullable=False),
        sa.Column("variant_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("timing_model", sa.String(30), nullable=False, server_default="manual"),
        sa.Column("reference", sa.String(40), nullable=True),
        sa.Column("direction", sa.String(10), nullable=True),
        sa.Column("offset_minutes", sa.Integer(), nullable=True),
        sa.Column("match_weekday", sa.Integer(), nullable=True, index=True),
        sa.Column("target_weekday", sa.Integer(), nullable=True),
        sa.Column("local_time", sa.String(5), nullable=True),
        sa.Column("timezone", sa.String(50), nullable=False, server_default="Europe/Berlin"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("instagram_page_id", sa.String(36), nullable=True, index=True),
        sa.Column("template", sa.String(100), nullable=True),
        sa.Column("reuse_media", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("legacy_story_rule_id", sa.String(36), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("media_kind IN ('feed', 'story')", name="ck_publication_rule_media_kind"),
        sa.CheckConstraint(
            "timing_model IN ('relative', 'weekday_fixed', 'result_detected', 'manual')",
            name="ck_publication_rule_timing_model",
        ),
        sa.CheckConstraint("variant_number > 0", name="ck_publication_rule_variant"),
        sa.CheckConstraint(
            "match_weekday IS NULL OR (match_weekday >= 0 AND match_weekday <= 6)",
            name="ck_publication_rule_match_weekday",
        ),
        sa.CheckConstraint(
            "target_weekday IS NULL OR (target_weekday >= 0 AND target_weekday <= 6)",
            name="ck_publication_rule_target_weekday",
        ),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["rule_set_id"], ["content_rule_sets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["instagram_page_id"], ["instagram_pages.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["legacy_story_rule_id"], ["story_rules.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("rule_set_id", "slot_key", name="uq_publication_rule_slot_key"),
    )
    op.create_table(
        "post_text_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("post_id", sa.String(36), nullable=False, index=True),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("generation_job_id", sa.String(36), nullable=True, index=True),
        sa.Column("prompt_template_id", sa.String(36), nullable=True),
        sa.Column("prompt_version", sa.Integer(), nullable=True),
        sa.Column("prompt_checksum", sa.String(64), nullable=True),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("source", sa.String(30), nullable=False, server_default="generation"),
        sa.Column("validation_status", sa.String(30), nullable=False, server_default="valid"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("version_number > 0", name="ck_post_text_version_number"),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["generation_job_id"], ["generation_jobs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["prompt_template_id"], ["prompt_templates.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("post_id", "version_number", name="uq_post_text_version"),
    )
    op.create_table(
        "generated_media_slots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("post_id", sa.String(36), nullable=False, index=True),
        sa.Column("game_id", sa.String(36), nullable=True, index=True),
        sa.Column("team_id", sa.String(36), nullable=False, index=True),
        sa.Column("story_rule_id", sa.String(36), nullable=True, index=True),
        sa.Column("slot_key", sa.String(120), nullable=False),
        sa.Column("media_kind", sa.String(10), nullable=False, index=True),
        sa.Column("output_position", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("variant_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("label", sa.String(180), nullable=False),
        sa.Column("selection_mode", sa.String(20), nullable=False, server_default="auto_latest"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("media_kind IN ('feed', 'story')", name="ck_generated_media_slot_kind"),
        sa.CheckConstraint("variant_number > 0", name="ck_generated_media_slot_variant"),
        sa.CheckConstraint("output_position > 0", name="ck_generated_media_slot_position"),
        sa.CheckConstraint(
            "selection_mode IN ('auto_latest', 'manual')",
            name="ck_generated_media_slot_selection_mode",
        ),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["story_rule_id"], ["story_rules.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("club_id", "post_id", "slot_key", name="uq_generated_media_slot_key"),
        sa.UniqueConstraint("id", "club_id", name="uq_generated_media_slots_id_club"),
    )
    op.create_table(
        "generated_media_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("club_id", sa.String(36), nullable=False, index=True),
        sa.Column("slot_id", sa.String(36), nullable=False, index=True),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("media_path", sa.String(800), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(80), nullable=False, server_default="image/png"),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("generation_status", sa.String(30), nullable=False, server_default="completed"),
        sa.Column("validation_status", sa.String(30), nullable=False, server_default="valid"),
        sa.Column("generation_job_id", sa.String(36), nullable=True, index=True),
        sa.Column("source_media_asset_id", sa.String(36), nullable=True),
        sa.Column("prompt_template_id", sa.String(36), nullable=True),
        sa.Column("prompt_version", sa.Integer(), nullable=True),
        sa.Column("prompt_checksum", sa.String(64), nullable=True),
        sa.Column("logo_references", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("design_metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("legacy_import", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("version_number > 0", name="ck_generated_media_version_number"),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["slot_id"], ["generated_media_slots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["generation_job_id"], ["generation_jobs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["source_media_asset_id"], ["media_assets.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["prompt_template_id"], ["prompt_templates.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("slot_id", "version_number", name="uq_generated_media_version"),
        sa.UniqueConstraint("id", "club_id", name="uq_generated_media_versions_id_club"),
        sa.UniqueConstraint("id", "slot_id", name="uq_generated_media_versions_id_slot"),
    )
    with op.batch_alter_table("generated_media_slots") as batch:
        batch.add_column(sa.Column("selected_version_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("latest_version_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_generated_media_slots_selected_version_same_slot",
            "generated_media_versions",
            ["selected_version_id", "id"],
            ["id", "slot_id"],
        )
        batch.create_foreign_key(
            "fk_generated_media_slots_latest_version_same_slot",
            "generated_media_versions",
            ["latest_version_id", "id"],
            ["id", "slot_id"],
        )


def _add_link_columns() -> None:
    with op.batch_alter_table("posts") as batch:
        batch.add_column(
            sa.Column(
                "text_selection_mode", sa.String(20), nullable=False, server_default="auto_latest"
            )
        )
        batch.add_column(sa.Column("selected_text_version_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("latest_text_version_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_posts_selected_text_version",
            "post_text_versions",
            ["selected_text_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_posts_latest_text_version",
            "post_text_versions",
            ["latest_text_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("publication_jobs") as batch:
        batch.add_column(sa.Column("text_version_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("media_version_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("publication_rule_slot_id", sa.String(36), nullable=True))
        batch.add_column(
            sa.Column("schedule_source", sa.String(30), nullable=False, server_default="legacy")
        )
        batch.create_foreign_key(
            "fk_publication_jobs_text_version",
            "post_text_versions",
            ["text_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_publication_jobs_media_version",
            "generated_media_versions",
            ["media_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_publication_jobs_rule_slot",
            "publication_rule_slots",
            ["publication_rule_slot_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_publication_jobs_text_version_id", "publication_jobs", ["text_version_id"])
    op.create_index("ix_publication_jobs_media_version_id", "publication_jobs", ["media_version_id"])
    op.create_index(
        "ix_publication_jobs_publication_rule_slot_id",
        "publication_jobs",
        ["publication_rule_slot_id"],
    )
    with op.batch_alter_table("publication_media_items") as batch:
        batch.add_column(sa.Column("media_version_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_publication_media_items_media_version",
            "generated_media_versions",
            ["media_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_publication_media_items_media_version_id",
        "publication_media_items",
        ["media_version_id"],
    )


def _backfill_versions() -> dict:
    bind = op.get_bind()
    posts = list(
        bind.execute(
            sa.text(
                "SELECT id, club_id, game_id, team_id, text, text_version, media_asset_id, "
                "design_snapshot FROM posts"
            )
        ).mappings()
    )
    if any(not row["club_id"] for row in posts):
        raise RuntimeError("0024 abgebrochen: Beitrag ohne eindeutige Vereinszuordnung")
    text_ids: dict[str, str] = {}
    for row in posts:
        version_id = _uid()
        text_ids[row["id"]] = version_id
        bind.execute(
            sa.text(
                "INSERT INTO post_text_versions "
                "(id, club_id, post_id, version_number, text, source, validation_status, "
                "metadata_json, created_at, updated_at, version) "
                "VALUES (:id, :club_id, :post_id, :version_number, :text, 'legacy_import', "
                "'valid', :metadata, :created_at, :updated_at, 1)"
            ),
            {
                "id": version_id,
                "club_id": row["club_id"],
                "post_id": row["id"],
                "version_number": max(1, int(row["text_version"] or 1)),
                "text": row["text"] or "",
                "metadata": json.dumps({"legacy_import": True}),
                **_stamp(),
            },
        )
        bind.execute(
            sa.text(
                "UPDATE posts SET selected_text_version_id=:version_id, "
                "latest_text_version_id=:version_id WHERE id=:post_id"
            ),
            {"version_id": version_id, "post_id": row["id"]},
        )

    post_map = {row["id"]: row for row in posts}
    story_slots = {
        row["id"]: max(1, int(row["media_slot"] or 1))
        for row in bind.execute(sa.text("SELECT id, media_slot FROM story_rules")).mappings()
    }
    slot_cache: dict[tuple[str, str], tuple[str, str]] = {}

    def version_for(job, slot_key, position, path_value, metadata, label):
        cache_key = (job["post_id"], slot_key)
        if cache_key in slot_cache:
            return slot_cache[cache_key]
        post = post_map[job["post_id"]]
        slot_id, version_id = _uid(), _uid()
        bind.execute(
            sa.text(
                "INSERT INTO generated_media_slots "
                "(id, club_id, post_id, game_id, team_id, story_rule_id, slot_key, media_kind, "
                "output_position, variant_number, label, selection_mode, created_at, updated_at, version) "
                "VALUES (:id, :club_id, :post_id, :game_id, :team_id, :story_rule_id, :slot_key, "
                ":media_kind, :position, 1, :label, 'auto_latest', :created_at, :updated_at, 1)"
            ),
            {
                "id": slot_id,
                "club_id": post["club_id"],
                "post_id": post["id"],
                "game_id": post["game_id"],
                "team_id": post["team_id"],
                "story_rule_id": job["story_rule_id"],
                "slot_key": slot_key,
                "media_kind": "story" if job["kind"] == "story" else "feed",
                "position": position,
                "label": label,
                **_stamp(),
            },
        )
        file_meta = {**_file_metadata(path_value), **(metadata or {})}
        bind.execute(
            sa.text(
                "INSERT INTO generated_media_versions "
                "(id, club_id, slot_id, version_number, media_path, checksum, mime_type, "
                "file_size, width, height, generation_status, validation_status, "
                "source_media_asset_id, logo_references, design_metadata, legacy_import, "
                "created_at, updated_at, version) "
                "VALUES (:id, :club_id, :slot_id, 1, :media_path, :checksum, :mime_type, "
                ":file_size, :width, :height, 'completed', :validation_status, :media_asset_id, "
                ":logos, :design, :legacy_import, :created_at, :updated_at, 1)"
            ),
            {
                "id": version_id,
                "club_id": post["club_id"],
                "slot_id": slot_id,
                "media_path": path_value,
                "media_asset_id": post["media_asset_id"],
                "logos": json.dumps(_json(post["design_snapshot"]).get("logos") or {}),
                "design": json.dumps({"legacy_import": True}),
                "legacy_import": True,
                **file_meta,
                **_stamp(),
            },
        )
        bind.execute(
            sa.text(
                "UPDATE generated_media_slots SET selected_version_id=:version_id, "
                "latest_version_id=:version_id WHERE id=:slot_id"
            ),
            {"version_id": version_id, "slot_id": slot_id},
        )
        slot_cache[cache_key] = (slot_id, version_id)
        return slot_id, version_id

    jobs = list(
        bind.execute(
            sa.text(
                "SELECT id, post_id, kind, media_path, story_rule_id FROM publication_jobs "
                "ORDER BY created_at, id"
            )
        ).mappings()
    )
    media_count = 0
    for job in jobs:
        if job["post_id"] not in post_map:
            raise RuntimeError("0024 abgebrochen: Veröffentlichungsauftrag ohne Beitrag")
        if job["kind"] == "carousel":
            items = list(
                bind.execute(
                    sa.text(
                        "SELECT id, position, media_path, checksum, mime_type, file_size, width, height "
                        "FROM publication_media_items WHERE publication_job_id=:job_id "
                        "ORDER BY position"
                    ),
                    {"job_id": job["id"]},
                ).mappings()
            )
            first_version = None
            for item in items:
                _slot_id, version_id = version_for(
                    job,
                    f"feed:{item['position']}:variant:1",
                    int(item["position"]),
                    item["media_path"],
                    {
                        "checksum": item["checksum"],
                        "mime_type": item["mime_type"],
                        "file_size": item["file_size"],
                        "width": item["width"],
                        "height": item["height"],
                        "validation_status": "valid",
                    },
                    f"Feed-Bild {item['position']}",
                )
                first_version = first_version or version_id
                media_count += 1
                bind.execute(
                    sa.text(
                        "UPDATE publication_media_items SET media_version_id=:version_id WHERE id=:id"
                    ),
                    {"version_id": version_id, "id": item["id"]},
                )
            if first_version:
                bind.execute(
                    sa.text(
                        "UPDATE publication_jobs SET media_version_id=:media_version, "
                        "text_version_id=:text_version WHERE id=:id"
                    ),
                    {
                        "media_version": first_version,
                        "text_version": text_ids[job["post_id"]],
                        "id": job["id"],
                    },
                )
        else:
            position = (
                story_slots.get(job["story_rule_id"], 1) if job["kind"] == "story" else 1
            )
            slot_key = (
                f"story:{position}:variant:1"
                if job["kind"] == "story"
                else "feed:1:variant:1"
            )
            _slot_id, version_id = version_for(
                job,
                slot_key,
                position,
                job["media_path"],
                None,
                f"Story-Ausgabe {position}" if job["kind"] == "story" else "Feed-Bild 1",
            )
            media_count += 1
            bind.execute(
                sa.text(
                    "UPDATE publication_jobs SET media_version_id=:media_version, "
                    "text_version_id=:text_version WHERE id=:id"
                ),
                {
                    "media_version": version_id,
                    "text_version": text_ids[job["post_id"]],
                    "id": job["id"],
                },
            )
    return {
        "posts": len(posts),
        "text_versions": len(text_ids),
        "media_slots": len(slot_cache),
        "publication_media_bindings": media_count,
    }


def _backfill_rules() -> dict:
    bind = op.get_bind()
    teams = list(bind.execute(sa.text("SELECT id, club_id, rules, timezone FROM teams")).mappings())
    if any(not row["club_id"] for row in teams):
        raise RuntimeError("0024 abgebrochen: Mannschaft ohne eindeutige Vereinszuordnung")
    story_rows = list(
        bind.execute(
            sa.text(
                "SELECT id, club_id, team_id, name, post_type, reference, direction, "
                "offset_minutes, timing_mode, weekday_times, weekday_targets, media_slot, "
                "sort_order FROM story_rules WHERE active = true ORDER BY team_id, sort_order, id"
            )
        ).mappings()
    )
    story_by_team: dict[str, list] = {}
    for row in story_rows:
        story_by_team.setdefault(row["team_id"], []).append(row)
    set_count = slot_count = 0
    for team in teams:
        rules = _json(team["rules"])
        for post_type in ("announcement", "reminder", "result"):
            rule_set_id = _uid()
            feed_count = max(0, min(10, int(rules.get(f"{post_type}_feed_output_count", 1))))
            story_count = max(0, min(10, int(rules.get(f"{post_type}_story_output_count", 1))))
            bind.execute(
                sa.text(
                    "INSERT INTO content_rule_sets "
                    "(id, club_id, scope_type, scope_key, team_id, post_type, rule_version, active, "
                    "feed_generation_count, story_generation_count, feed_publish_variants, "
                    "story_publish_variants, approval_policy, created_at, updated_at, version) "
                    "VALUES (:id, :club_id, 'team', :scope_key, :team_id, :post_type, 1, :active, "
                    ":feed_count, :story_count, :feed_variants, :story_variants, :approval, "
                    ":created_at, :updated_at, 1)"
                ),
                {
                    "id": rule_set_id,
                    "club_id": team["club_id"],
                    "scope_key": f"team:{team['id']}",
                    "team_id": team["id"],
                    "post_type": post_type,
                    "active": bool(rules.get(f"{post_type}_enabled", post_type == "announcement")),
                    "feed_count": feed_count,
                    "story_count": story_count,
                    "feed_variants": json.dumps(list(range(1, feed_count + 1))),
                    "story_variants": json.dumps(list(range(1, story_count + 1))),
                    "approval": "automatic"
                    if rules.get(f"auto_approve_{'results' if post_type == 'result' else 'announcements'}")
                    else "manual",
                    **_stamp(),
                },
            )
            set_count += 1
            mode = rules.get(f"{post_type}_timing_mode", "relative")
            times = _json(rules.get(f"{post_type}_weekday_times"))
            targets = _json(rules.get(f"{post_type}_weekday_targets"))
            if mode == "weekday_fixed":
                for match_day, local_time in sorted(times.items()):
                    bind.execute(
                        sa.text(
                            "INSERT INTO publication_rule_slots "
                            "(id, club_id, rule_set_id, slot_key, label, media_kind, variant_number, "
                            "timing_model, match_weekday, target_weekday, local_time, timezone, "
                            "sort_order, active, created_at, updated_at, version) "
                            "VALUES (:id, :club_id, :rule_set_id, :slot_key, :label, 'feed', 1, "
                            "'weekday_fixed', :match_weekday, :target_weekday, :local_time, :timezone, "
                            ":sort_order, true, :created_at, :updated_at, 1)"
                        ),
                        {
                            "id": _uid(),
                            "club_id": team["club_id"],
                            "rule_set_id": rule_set_id,
                            "slot_key": f"feed:weekday:{match_day}",
                            "label": f"Feed für Spiel-Wochentag {match_day}",
                            "match_weekday": int(match_day),
                            "target_weekday": int(targets.get(match_day, match_day)),
                            "local_time": local_time,
                            "timezone": team["timezone"] or "Europe/Berlin",
                            "sort_order": int(match_day),
                            **_stamp(),
                        },
                    )
                    slot_count += 1
            elif feed_count:
                timing_model = "result_detected" if mode == "result_detected" else "relative"
                prefix = "result" if post_type == "result" else post_type
                bind.execute(
                    sa.text(
                        "INSERT INTO publication_rule_slots "
                        "(id, club_id, rule_set_id, slot_key, label, media_kind, variant_number, "
                        "timing_model, reference, direction, offset_minutes, timezone, sort_order, "
                        "active, created_at, updated_at, version) "
                        "VALUES (:id, :club_id, :rule_set_id, 'feed:default', :label, 'feed', 1, "
                        ":timing_model, :reference, :direction, :offset, :timezone, 0, true, "
                        ":created_at, :updated_at, 1)"
                    ),
                    {
                        "id": _uid(),
                        "club_id": team["club_id"],
                        "rule_set_id": rule_set_id,
                        "label": "Feed-Veröffentlichung",
                        "timing_model": timing_model,
                        "reference": "result_detected" if timing_model == "result_detected" else "kickoff",
                        "direction": rules.get(f"{prefix}_offset_direction", "after" if post_type == "result" else "before"),
                        "offset": int(rules.get(f"{prefix}_offset_minutes", rules.get("feed_before_minutes", 1440))),
                        "timezone": team["timezone"] or "Europe/Berlin",
                        **_stamp(),
                    },
                )
                slot_count += 1
            for story in [r for r in story_by_team.get(team["id"], []) if r["post_type"] == post_type]:
                weekday_times = _json(story["weekday_times"])
                weekday_targets = _json(story["weekday_targets"])
                if story["timing_mode"] == "weekday_fixed":
                    entries = sorted(weekday_times.items())
                else:
                    entries = [(None, None)]
                for match_day, local_time in entries:
                    suffix = match_day if match_day is not None else "default"
                    bind.execute(
                        sa.text(
                            "INSERT INTO publication_rule_slots "
                            "(id, club_id, rule_set_id, slot_key, label, media_kind, variant_number, "
                            "timing_model, reference, direction, offset_minutes, match_weekday, "
                            "target_weekday, local_time, timezone, sort_order, active, "
                            "legacy_story_rule_id, created_at, updated_at, version) "
                            "VALUES (:id, :club_id, :rule_set_id, :slot_key, :label, 'story', :variant, "
                            ":timing_model, :reference, :direction, :offset, :match_weekday, "
                            ":target_weekday, :local_time, :timezone, :sort_order, true, :legacy_id, "
                            ":created_at, :updated_at, 1)"
                        ),
                        {
                            "id": _uid(),
                            "club_id": team["club_id"],
                            "rule_set_id": rule_set_id,
                            "slot_key": f"story:{story['id']}:{suffix}",
                            "label": story["name"],
                            "variant": max(1, int(story["media_slot"] or 1)),
                            "timing_model": story["timing_mode"],
                            "reference": story["reference"],
                            "direction": story["direction"],
                            "offset": story["offset_minutes"],
                            "match_weekday": int(match_day) if match_day is not None else None,
                            "target_weekday": int(weekday_targets.get(match_day, match_day)) if match_day is not None else None,
                            "local_time": local_time,
                            "timezone": team["timezone"] or "Europe/Berlin",
                            "sort_order": story["sort_order"],
                            "legacy_id": story["id"],
                            **_stamp(),
                        },
                    )
                    slot_count += 1
    return {"rule_sets": set_count, "rule_slots": slot_count}


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "generated_media_versions" in inspector.get_table_names():
        return
    _create_tables()
    _add_link_columns()
    version_report = _backfill_versions()
    rule_report = _backfill_rules()
    # The report is intentionally stored as Alembic output rather than in a
    # tenant table; operators can capture it during the required preflight.
    print(json.dumps({"migration": "0024", **version_report, **rule_report}, sort_keys=True))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if "publication_media_items" in inspector.get_table_names():
        if "ix_publication_media_items_media_version_id" in {
            index["name"] for index in inspector.get_indexes("publication_media_items")
        }:
            op.drop_index(
                "ix_publication_media_items_media_version_id",
                table_name="publication_media_items",
            )
        with op.batch_alter_table("publication_media_items") as batch:
            batch.drop_column("media_version_id")
    if "publication_jobs" in inspector.get_table_names():
        for index_name in (
            "ix_publication_jobs_publication_rule_slot_id",
            "ix_publication_jobs_media_version_id",
            "ix_publication_jobs_text_version_id",
        ):
            if index_name in {index["name"] for index in inspector.get_indexes("publication_jobs")}:
                op.drop_index(index_name, table_name="publication_jobs")
        with op.batch_alter_table("publication_jobs") as batch:
            for column in (
                "schedule_source",
                "publication_rule_slot_id",
                "media_version_id",
                "text_version_id",
            ):
                batch.drop_column(column)
    if "posts" in inspector.get_table_names():
        with op.batch_alter_table("posts") as batch:
            batch.drop_column("latest_text_version_id")
            batch.drop_column("selected_text_version_id")
            batch.drop_column("text_selection_mode")
    if "generated_media_slots" in sa.inspect(op.get_bind()).get_table_names():
        with op.batch_alter_table("generated_media_slots") as batch:
            batch.drop_constraint(
                "fk_generated_media_slots_latest_version_same_slot", type_="foreignkey"
            )
            batch.drop_constraint(
                "fk_generated_media_slots_selected_version_same_slot", type_="foreignkey"
            )
    for table in (
        "generated_media_versions",
        "generated_media_slots",
        "post_text_versions",
        "publication_rule_slots",
        "content_rule_sets",
    ):
        if table in sa.inspect(op.get_bind()).get_table_names():
            op.drop_table(table)
