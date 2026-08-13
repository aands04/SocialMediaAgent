"""tenant-scoped creative intelligence and onboarding

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    ]


def upgrade() -> None:
    op.alter_column(
        "usage_ledger_entries",
        "generation_type",
        existing_type=sa.String(length=20),
        type_=sa.String(length=40),
        existing_nullable=False,
    )
    op.add_column(
        "ai_prompt_dispatches",
        sa.Column(
            "creative_profile_snapshot",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )

    op.create_table(
        "creative_feedback_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("club_id", sa.String(length=36), nullable=False),
        sa.Column("team_id", sa.String(length=36)),
        sa.Column("post_id", sa.String(length=36)),
        sa.Column("generated_media_slot_id", sa.String(length=36)),
        sa.Column("media_version_id", sa.String(length=36)),
        sa.Column("text_version_id", sa.String(length=36)),
        sa.Column("generation_job_id", sa.String(length=36)),
        sa.Column("user_id", sa.String(length=36)),
        sa.Column("modality", sa.String(length=10), nullable=False),
        sa.Column("content_type", sa.String(length=30), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("sentiment", sa.String(length=10)),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("free_text", sa.Text()),
        sa.Column("traits_snapshot", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("correction_of_id", sa.String(length=36)),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "modality IN ('image', 'text')", name="ck_creative_feedback_modality"
        ),
        sa.CheckConstraint(
            "action IN ('selected', 'published', 'approved', 'rejected', "
            "'regenerated', 'reverted', 'manually_edited', 'replaced', 'skipped')",
            name="ck_creative_feedback_action",
        ),
        sa.CheckConstraint(
            "source IN ('onboarding_explicit', 'onboarding_calibration', 'normal_usage', "
            "'explicit_feedback', 'platform_admin_override')",
            name="ck_creative_feedback_source",
        ),
        sa.CheckConstraint(
            "sentiment IS NULL OR sentiment IN ('positive', 'negative', 'neutral')",
            name="ck_creative_feedback_sentiment",
        ),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["generated_media_slot_id"], ["generated_media_slots.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["media_version_id"], ["generated_media_versions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["text_version_id"], ["post_text_versions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["generation_job_id"], ["generation_jobs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["correction_of_id"], ["creative_feedback_events.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "club_id", "idempotency_key", name="uq_creative_feedback_idempotency"
        ),
    )
    for column in (
        "club_id",
        "team_id",
        "post_id",
        "generated_media_slot_id",
        "media_version_id",
        "text_version_id",
        "generation_job_id",
        "user_id",
        "modality",
        "content_type",
        "action",
        "source",
        "sentiment",
        "correction_of_id",
        "occurred_at",
    ):
        op.create_index(
            f"ix_creative_feedback_events_{column}", "creative_feedback_events", [column]
        )

    op.create_table(
        "creative_preference_profiles",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("club_id", sa.String(length=36), nullable=False),
        sa.Column("modality", sa.String(length=10), nullable=False),
        sa.Column("content_type", sa.String(length=30), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("preferences", sa.JSON(), nullable=False),
        sa.Column("avoidances", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("source_summary", sa.JSON(), nullable=False),
        sa.Column("learner_version", sa.String(length=40), nullable=False),
        sa.Column("generated_by", sa.String(length=80), nullable=False),
        sa.Column("build_reason", sa.String(length=40), nullable=False),
        sa.Column("last_feedback_at", sa.DateTime(timezone=True)),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint(
            "modality IN ('image', 'text')", name="ck_creative_profile_modality"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'archived')",
            name="ck_creative_profile_status",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_creative_profile_confidence"
        ),
        sa.CheckConstraint("sample_count >= 0", name="ck_creative_profile_sample_count"),
        sa.CheckConstraint("profile_version > 0", name="ck_creative_profile_version"),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "club_id",
            "modality",
            "content_type",
            "profile_version",
            name="uq_creative_preference_profile_version",
        ),
    )
    for column in ("club_id", "modality", "content_type", "status"):
        op.create_index(
            f"ix_creative_preference_profiles_{column}",
            "creative_preference_profiles",
            [column],
        )

    op.create_table(
        "creative_example_references",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("club_id", sa.String(length=36), nullable=False),
        sa.Column("profile_id", sa.String(length=36)),
        sa.Column("modality", sa.String(length=10), nullable=False),
        sa.Column("content_type", sa.String(length=30), nullable=False),
        sa.Column("sentiment", sa.String(length=10), nullable=False),
        sa.Column("media_version_id", sa.String(length=36)),
        sa.Column("text_version_id", sa.String(length=36)),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("traits", sa.JSON(), nullable=False),
        sa.Column("score", sa.Numeric(8, 4), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=36)),
        *_timestamps(),
        sa.CheckConstraint(
            "modality IN ('image', 'text')", name="ck_creative_example_modality"
        ),
        sa.CheckConstraint(
            "sentiment IN ('positive', 'negative')", name="ck_creative_example_sentiment"
        ),
        sa.CheckConstraint(
            "(modality = 'image' AND media_version_id IS NOT NULL AND text_version_id IS NULL) "
            "OR (modality = 'text' AND text_version_id IS NOT NULL AND media_version_id IS NULL)",
            name="ck_creative_example_reference",
        ),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["creative_preference_profiles.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["media_version_id"], ["generated_media_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["text_version_id"], ["post_text_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
    )
    for column in (
        "club_id",
        "profile_id",
        "modality",
        "content_type",
        "sentiment",
        "media_version_id",
        "text_version_id",
        "active",
    ):
        op.create_index(
            f"ix_creative_example_references_{column}",
            "creative_example_references",
            [column],
        )

    op.create_table(
        "creative_profile_overrides",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("club_id", sa.String(length=36), nullable=False),
        sa.Column("modality", sa.String(length=10), nullable=False),
        sa.Column("content_type", sa.String(length=30), nullable=False),
        sa.Column("override_version", sa.Integer(), nullable=False),
        sa.Column("preferences", sa.JSON(), nullable=False),
        sa.Column("avoidances", sa.JSON(), nullable=False),
        sa.Column("trait", sa.String(length=80)),
        sa.Column("override_type", sa.String(length=30), nullable=False),
        sa.Column("override_value", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(length=500)),
        sa.Column("notes", sa.Text()),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True)),
        sa.Column("valid_until", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "modality IN ('image', 'text')", name="ck_creative_override_modality"
        ),
        sa.CheckConstraint("override_version > 0", name="ck_creative_override_version"),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "club_id",
            "modality",
            "content_type",
            "override_version",
            name="uq_creative_profile_override_version",
        ),
    )
    for column in ("club_id", "modality", "content_type", "trait", "active"):
        op.create_index(
            f"ix_creative_profile_overrides_{column}",
            "creative_profile_overrides",
            [column],
        )

    op.create_table(
        "creative_recipes",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("modality", sa.String(length=10), nullable=False),
        sa.Column("content_type", sa.String(length=30), nullable=False),
        sa.Column("recipe_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("traits", sa.JSON(), nullable=False),
        sa.Column("constraints", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint(
            "modality IN ('image', 'text')", name="ck_creative_recipe_modality"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'archived')", name="ck_creative_recipe_status"
        ),
        sa.CheckConstraint("recipe_version > 0", name="ck_creative_recipe_version"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("key", "recipe_version", name="uq_creative_recipe_version"),
    )
    for column in ("key", "modality", "content_type", "status"):
        op.create_index(f"ix_creative_recipes_{column}", "creative_recipes", [column])

    op.create_table(
        "visual_trait_analysis_cache",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("club_id", sa.String(length=36), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("media_asset_id", sa.String(length=36)),
        sa.Column("media_version_id", sa.String(length=36)),
        sa.Column("analyzer_version", sa.String(length=40), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("traits", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_summary", sa.String(length=500)),
        sa.Column("usage_ledger_entry_id", sa.String(length=36)),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'failed')",
            name="ck_visual_trait_analysis_status",
        ),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["media_asset_id"], ["media_assets.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["media_version_id"], ["generated_media_versions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["usage_ledger_entry_id"], ["usage_ledger_entries.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "club_id", "checksum", "analyzer_version", name="uq_visual_trait_analysis_cache"
        ),
    )
    for column in ("club_id", "checksum", "media_asset_id", "media_version_id", "status"):
        op.create_index(
            f"ix_visual_trait_analysis_cache_{column}",
            "visual_trait_analysis_cache",
            [column],
        )

    op.create_table(
        "club_onboarding_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("club_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("current_step", sa.Integer(), nullable=False),
        sa.Column("onboarding_version", sa.String(length=20), nullable=False),
        sa.Column("completed_steps", sa.JSON(), nullable=False),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("skipped_calibration_at", sa.DateTime(timezone=True)),
        sa.Column("created_by_user_id", sa.String(length=36)),
        sa.Column("last_actor_user_id", sa.String(length=36)),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('not_started', 'in_progress', 'calibration_pending', "
            "'completed', 'skipped')",
            name="ck_club_onboarding_status",
        ),
        sa.CheckConstraint(
            "current_step >= 1 AND current_step <= 11", name="ck_club_onboarding_step"
        ),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["last_actor_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("club_id", name="uq_club_onboarding_sessions_club_id"),
    )
    op.create_index(
        "ix_club_onboarding_sessions_club_id",
        "club_onboarding_sessions",
        ["club_id"],
        unique=True,
    )
    op.create_index(
        "ix_club_onboarding_sessions_status", "club_onboarding_sessions", ["status"]
    )

    op.create_table(
        "onboarding_calibration_samples",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("club_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("modality", sa.String(length=10), nullable=False),
        sa.Column("content_type", sa.String(length=30), nullable=False),
        sa.Column("recipe_key", sa.String(length=100), nullable=False),
        sa.Column("sample_index", sa.Integer(), nullable=False),
        sa.Column("generation_job_id", sa.String(length=36)),
        sa.Column("media_version_id", sa.String(length=36)),
        sa.Column("text_version_id", sa.String(length=36)),
        sa.Column("rendered_text", sa.Text()),
        sa.Column("preview_payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("ranking", sa.Integer()),
        sa.Column("feedback", sa.JSON(), nullable=False),
        sa.Column("usage_ledger_entry_id", sa.String(length=36)),
        sa.Column("publishing_blocked", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "modality IN ('image', 'text')", name="ck_onboarding_sample_modality"
        ),
        sa.CheckConstraint("sample_index > 0", name="ck_onboarding_sample_index"),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["club_onboarding_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["generation_job_id"], ["generation_jobs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["media_version_id"], ["generated_media_versions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["text_version_id"], ["post_text_versions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["usage_ledger_entry_id"], ["usage_ledger_entries.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "club_id",
            "session_id",
            "modality",
            "content_type",
            "sample_index",
            name="uq_onboarding_calibration_sample",
        ),
    )
    for column in (
        "club_id",
        "session_id",
        "modality",
        "content_type",
        "generation_job_id",
        "status",
    ):
        op.create_index(
            f"ix_onboarding_calibration_samples_{column}",
            "onboarding_calibration_samples",
            [column],
        )


def downgrade() -> None:
    op.drop_table("onboarding_calibration_samples")
    op.drop_table("club_onboarding_sessions")
    op.drop_table("visual_trait_analysis_cache")
    op.drop_table("creative_recipes")
    op.drop_table("creative_profile_overrides")
    op.drop_table("creative_example_references")
    op.drop_table("creative_preference_profiles")
    op.drop_table("creative_feedback_events")
    op.drop_column("ai_prompt_dispatches", "creative_profile_snapshot")
    # These feature-specific rows cannot be represented by the previous
    # VARCHAR(20) column.  The downgrade removes the feature tables as well,
    # so keeping their now-orphaned internal usage rows would make the type
    # narrowing fail on PostgreSQL.
    op.execute(
        sa.text(
            "DELETE FROM usage_ledger_entries "
            "WHERE generation_type IN ("
            "'creative_director', 'preference_learning', "
            "'visual_trait_analysis', 'onboarding_calibration')"
        )
    )
    op.alter_column(
        "usage_ledger_entries",
        "generation_type",
        existing_type=sa.String(length=40),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
