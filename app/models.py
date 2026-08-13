import enum
import hashlib
import re
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db import Base


def now():
    return datetime.now(timezone.utc)


def uid():
    return str(uuid4())


class Role(str, enum.Enum):
    ADMIN = "admin"
    EDITOR = "editor"
    APPROVER = "approver"
    REVIEWER = "reviewer"
    VIEWER = "viewer"

    @property
    def label(self) -> str:
        return {
            Role.ADMIN: "Vereinsadministrator",
            Role.APPROVER: "Redakteur",
            Role.EDITOR: "Autor",
            Role.REVIEWER: "Freigeber",
            Role.VIEWER: "Nur Lesen",
        }[self]

    @property
    def description(self) -> str:
        return {
            Role.ADMIN: "Vollzugriff einschließlich Benutzer- und Systemeinstellungen.",
            Role.APPROVER: "Darf Beiträge erstellen, bearbeiten und freigeben.",
            Role.EDITOR: "Darf Beiträge erstellen und bearbeiten, aber nicht freigeben.",
            Role.REVIEWER: "Darf Beiträge prüfen und freigeben, aber nicht selbst erstellen.",
            Role.VIEWER: "Darf Inhalte ausschließlich ansehen.",
        }[self]


class PostStatus(str, enum.Enum):
    DETECTED = "detected"
    PLANNED = "planned"
    CREATING = "creating"
    INCOMPLETE = "incomplete"
    PENDING = "pending_approval"
    REJECTED = "rejected"
    APPROVED = "approved"
    REAPPROVAL = "reapproval_required"
    SCHEDULED = "scheduled"
    PARTIAL = "partially_published"
    PUBLISHED = "published"
    ERROR = "publishing_error"
    CANCELLED = "cancelled"


class JobStatus(str, enum.Enum):
    DRAFT = "draft"
    UNAPPROVED = "unapproved"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    WAITING = "waiting"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    RETRY = "retry_scheduled"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    UNCERTAIN = "uncertain"


class GenerationJobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"


class GenerationJobType(str, enum.Enum):
    CREATE_POST = "create_post"
    RERENDER_POST = "rerender_post"


class ClubStatus(str, enum.Enum):
    SETUP_PENDING = "setup_pending"
    TRIAL = "trial"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


class AccountType(str, enum.Enum):
    CLUB_USER = "club_user"
    PLATFORM_ADMIN = "platform_admin"


class LedgerStatus(str, enum.Enum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"
    DELETED = "deleted"
    CORRECTED = "corrected"


class UsageStatus(str, enum.Enum):
    RESERVED = "reserved"
    PROVIDER_PROCESSING = "provider_processing"
    COMPLETED_BILLABLE = "completed_billable"
    COMPLETED_NOT_BILLABLE = "completed_not_billable"
    FAILED_TECHNICAL = "failed_technical"
    REJECTED_BY_USER = "rejected_by_user"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"


class PromptStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class PlanProfile(Base, Timestamped):
    __tablename__ = "plan_profiles"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_plan_profile_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(120), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_teams: Mapped[int] = mapped_column(Integer, default=1)
    max_storage_bytes: Mapped[int] = mapped_column(BigInteger, default=1_000_000_000)
    monthly_ai_texts: Mapped[int] = mapped_column(Integer, default=20)
    monthly_ai_images: Mapped[int] = mapped_column(Integer, default=50)
    max_fonts: Mapped[int] = mapped_column(Integer, default=2)
    max_instagram_pages: Mapped[int] = mapped_column(Integer, default=1)
    trial_days: Mapped[int | None] = mapped_column(Integer)
    feature_flags: Mapped[dict] = mapped_column(JSON, default=dict)


class Club(Base, Timestamped):
    __tablename__ = "clubs"
    __table_args__ = (
        CheckConstraint("slug <> ''", name="ck_clubs_slug_not_empty"),
        CheckConstraint("version > 0", name="ck_clubs_version_positive"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(180))
    short_name: Mapped[str] = mapped_column(String(60))
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    logo_asset_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[ClubStatus] = mapped_column(
        Enum(ClubStatus), default=ClubStatus.SETUP_PENDING, index=True
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Berlin")
    contact_name: Mapped[str | None] = mapped_column(String(180))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    billing_details: Mapped[dict] = mapped_column(JSON, default=dict)
    contract_details: Mapped[dict] = mapped_column(JSON, default=dict)
    technical_settings: Mapped[dict] = mapped_column(JSON, default=dict)
    branding_settings: Mapped[dict] = mapped_column(JSON, default=dict)
    plan_profile_id: Mapped[str] = mapped_column(ForeignKey("plan_profiles.id"), index=True)
    limit_overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    usage_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ClubAdditionalAllowance(Base, Timestamped):
    __tablename__ = "club_additional_allowances"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_club_allowances_positive"),
        UniqueConstraint(
            "club_id", "limit_key", "starts_at", "ends_at", name="uq_club_allowance_period"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="CASCADE"), index=True)
    limit_key: Mapped[str] = mapped_column(String(60), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reason: Mapped[str | None] = mapped_column(String(240))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))


class ClubBrandingConfiguration(Base, Timestamped):
    __tablename__ = "club_branding_configurations"
    club_id: Mapped[str] = mapped_column(
        ForeignKey("clubs.id", ondelete="CASCADE"), primary_key=True
    )
    image_settings: Mapped[dict] = mapped_column(JSON, default=dict)
    text_settings: Mapped[dict] = mapped_column(JSON, default=dict)
    primary_font_id: Mapped[str | None] = mapped_column(String(36))
    secondary_font_id: Mapped[str | None] = mapped_column(String(36))
    updated_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))


class FeatureFlag(Base, Timestamped):
    __tablename__ = "feature_flags"
    __table_args__ = (UniqueConstraint("club_id", "key", name="uq_feature_flag_scope"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str | None] = mapped_column(
        ForeignKey("clubs.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(100), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))


class TenantMigrationReport(Base):
    __tablename__ = "tenant_migration_reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    migration_revision: Mapped[str] = mapped_column(String(32), unique=True)
    club_id: Mapped[str | None] = mapped_column(ForeignKey("clubs.id"))
    status: Mapped[str] = mapped_column(String(30), index=True)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    checksum: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class User(Base, Timestamped):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "(account_type = 'CLUB_USER' AND club_id IS NOT NULL) OR "
            "(account_type = 'PLATFORM_ADMIN' AND club_id IS NULL)",
            name="ck_users_account_tenant",
        ),
        UniqueConstraint("id", "club_id", name="uq_users_id_club"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str | None] = mapped_column(
        ForeignKey("clubs.id", ondelete="RESTRICT"), index=True
    )
    account_type: Mapped[AccountType] = mapped_column(
        Enum(AccountType), default=AccountType.CLUB_USER, index=True
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.VIEWER)
    all_teams: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auth_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    registration_status: Mapped[str] = mapped_column(
        String(20), default="approved", server_default="approved", nullable=False, index=True
    )
    registration_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registration_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registration_reviewed_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_ip: Mapped[str | None] = mapped_column(String(80))
    delivery_status: Mapped[str] = mapped_column(String(20), default="pending")
    delivery_error: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class EmailChangeToken(Base):
    __tablename__ = "email_change_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    old_email: Mapped[str] = mapped_column(String(255))
    new_email: Mapped[str] = mapped_column(String(255), index=True)
    auth_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_ip: Mapped[str | None] = mapped_column(String(80))
    delivery_status: Mapped[str] = mapped_column(String(20), default="pending")
    delivery_error: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class UserTeam(Base):
    __tablename__ = "user_teams"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "club_id"], ["users.id", "users.club_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["team_id", "club_id"], ["teams.id", "teams.club_id"], ondelete="CASCADE"
        ),
    )
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    team_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    club_id: Mapped[str] = mapped_column(String(36), index=True)


class InstagramPage(Base, Timestamped):
    __tablename__ = "instagram_pages"
    __table_args__ = (
        UniqueConstraint("id", "club_id", name="uq_instagram_pages_id_club"),
        UniqueConstraint("club_id", "username", name="uq_instagram_pages_club_username"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    internal_name: Mapped[str] = mapped_column(String(120))
    display_name: Mapped[str] = mapped_column(String(120))
    username: Mapped[str] = mapped_column(String(80))
    profile_url: Mapped[str | None] = mapped_column(String(500))
    account_id: Mapped[str | None] = mapped_column(String(100))
    facebook_page_id: Mapped[str | None] = mapped_column(String(100))
    club: Mapped[str] = mapped_column(String(160))
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    connection_status: Mapped[str] = mapped_column(String(30), default="unconfigured")
    publishing_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    automatic_publishing_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    automatic_publishing_confirmed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    automatic_publishing_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    allowed_types: Mapped[dict] = mapped_column(JSON, default=lambda: {"feed": True, "story": True})
    defaults: Mapped[dict] = mapped_column(JSON, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class Team(Base, Timestamped):
    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("id", "club_id", name="uq_teams_id_club"),
        UniqueConstraint("club_id", "slug", name="uq_teams_club_slug"),
        ForeignKeyConstraint(
            ["instagram_page_id", "club_id"],
            ["instagram_pages.id", "instagram_pages.club_id"],
            ondelete="RESTRICT",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    internal_name: Mapped[str] = mapped_column(String(120))
    display_name: Mapped[str] = mapped_column(String(120))
    short_name: Mapped[str] = mapped_column(String(30))
    slug: Mapped[str] = mapped_column(String(80))
    club: Mapped[str] = mapped_column(String(160))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fussball_url: Mapped[str] = mapped_column(String(1000))
    # Compatibility link for the original Instagram-only workflow. New teams
    # may be created without Instagram and use TeamChannelAssignment instead.
    instagram_page_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    media_subdir: Mapped[str] = mapped_column(String(500))
    logo_path: Mapped[str | None] = mapped_column(String(500))
    logo_asset_id: Mapped[str | None] = mapped_column(ForeignKey("logo_assets.id"))
    feed_template: Mapped[str] = mapped_column(String(100), default="default-feed")
    story_templates: Mapped[list] = mapped_column(JSON, default=lambda: ["default-story"])
    primary_font: Mapped[str] = mapped_column(String(100), default="sans-serif")
    secondary_font: Mapped[str] = mapped_column(String(100), default="sans-serif")
    colors: Mapped[dict] = mapped_column(
        JSON, default=lambda: {"primary": "#172554", "secondary": "#ffffff"}
    )
    text_style: Mapped[dict] = mapped_column(JSON, default=dict)
    hashtags: Mapped[list] = mapped_column(JSON, default=list)
    timezone: Mapped[str] = mapped_column(String(50), default="Europe/Berlin")
    rules: Mapped[dict] = mapped_column(JSON, default=dict)
    publishing_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class Game(Base, Timestamped):
    __tablename__ = "games"
    __table_args__ = (
        UniqueConstraint(
            "club_id", "team_id", "provider", "external_id", name="uq_games_club_provider_external"
        ),
        UniqueConstraint("id", "club_id", name="uq_games_id_club"),
        ForeignKeyConstraint(
            ["team_id", "club_id"], ["teams.id", "teams.club_id"], ondelete="RESTRICT"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    team_id: Mapped[str] = mapped_column(String(36), index=True)
    provider: Mapped[str] = mapped_column(String(40), default="fussball.de")
    external_id: Mapped[str] = mapped_column(String(200))
    home_team: Mapped[str] = mapped_column(String(160))
    away_team: Mapped[str] = mapped_column(String(160))
    kickoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    original_kickoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    competition: Mapped[str | None] = mapped_column(String(160))
    venue: Mapped[str | None] = mapped_column(String(250))
    pitch: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(40), default="scheduled")
    home_score: Mapped[int | None] = mapped_column(Integer)
    away_score: Mapped[int | None] = mapped_column(Integer)
    halftime: Mapped[str | None] = mapped_column(String(20))
    result_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    source_url: Mapped[str] = mapped_column(String(1000))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    data_hash: Mapped[str | None] = mapped_column(String(64))
    opponent_logo_id: Mapped[str | None] = mapped_column(ForeignKey("logo_assets.id"))


class LogoAsset(Base, Timestamped):
    __tablename__ = "logo_assets"
    __table_args__ = (
        UniqueConstraint(
            "club_id",
            "logo_type",
            "team_id",
            "normalized_name",
            "version",
            name="uq_logo_assets_club_version",
        ),
        Index(
            "uq_logo_assets_team_checksum",
            "team_id",
            "club_id",
            "checksum",
            unique=True,
            postgresql_where=text("logo_type = 'team'"),
            sqlite_where=text("logo_type = 'team'"),
        ),
        Index(
            "uq_logo_assets_opponent_checksum",
            "club_id",
            "checksum",
            unique=True,
            postgresql_where=text("logo_type = 'opponent'"),
            sqlite_where=text("logo_type = 'opponent'"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    logo_type: Mapped[str] = mapped_column(String(20), index=True)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(200), index=True)
    original_path: Mapped[str] = mapped_column(String(800))
    render_path: Mapped[str | None] = mapped_column(String(800))
    original_filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(80))
    size: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_by: Mapped[str] = mapped_column(ForeignKey("users.id"))


class SharedOpponentLogo(Base, Timestamped):
    """Platform-wide, verified opponent-logo catalog.

    The binary is a dedicated canonical copy.  Tenant upload paths and user
    details are never required to browse or select this catalog.
    """

    __tablename__ = "shared_opponent_logos"
    __table_args__ = (
        UniqueConstraint(
            "normalized_name", "checksum", name="uq_shared_opponent_logo_name_checksum"
        ),
        Index("ix_shared_opponent_logo_name_active", "normalized_name", "active"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    display_name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(200), index=True)
    original_path: Mapped[str] = mapped_column(String(800))
    original_filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(80))
    size: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    catalog_version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_club_id: Mapped[str | None] = mapped_column(
        ForeignKey("clubs.id", ondelete="SET NULL"), index=True
    )
    uploaded_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class MediaAsset(Base, Timestamped):
    __tablename__ = "media_assets"
    __table_args__ = (
        UniqueConstraint("id", "club_id", name="uq_media_assets_id_club"),
        UniqueConstraint(
            "club_id", "team_id", "relative_path", name="uq_media_assets_club_team_path"
        ),
        ForeignKeyConstraint(
            ["team_id", "club_id"],
            ["teams.id", "teams.club_id"],
            ondelete="RESTRICT",
            name="fk_media_assets_team_club",
        ),
        CheckConstraint(
            "media_category IN ('match_photo', 'player_portrait', 'team_photo')",
            name="ck_media_assets_category",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"))
    storage_kind: Mapped[str] = mapped_column(
        String(20), default="external", server_default="external", nullable=False
    )
    relative_path: Mapped[str] = mapped_column(String(800))
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(80))
    size: Mapped[int] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    checksum: Mapped[str] = mapped_column(String(64))
    mtime: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    player_name: Mapped[str | None] = mapped_column(String(160))
    media_category: Mapped[str] = mapped_column(
        String(30), default="match_photo", server_default="match_photo", index=True
    )
    game_id: Mapped[str | None] = mapped_column(
        ForeignKey("games.id", ondelete="SET NULL"), index=True
    )
    description: Mapped[str | None] = mapped_column(String(500))
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    photographer: Mapped[str | None] = mapped_column(String(160))
    uploaded_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    automatic_usage_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", index=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    reserved_game_id: Mapped[str | None] = mapped_column(ForeignKey("games.id"), index=True)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class MediaUsageHistory(Base):
    """Immutable, user-facing history of media selection and consumption."""

    __tablename__ = "media_usage_history"
    __table_args__ = (
        CheckConstraint(
            "action IN ('reserved', 'reservation_released', 'used', 'released', 'manual_reuse', "
            "'automatic_excluded', 'automatic_enabled', 'soft_deleted')",
            name="ck_media_usage_history_action",
        ),
        ForeignKeyConstraint(
            ["media_asset_id", "club_id"],
            ["media_assets.id", "media_assets.club_id"],
            ondelete="RESTRICT",
            name="fk_media_usage_history_asset_club",
        ),
        ForeignKeyConstraint(
            ["team_id", "club_id"],
            ["teams.id", "teams.club_id"],
            ondelete="RESTRICT",
            name="fk_media_usage_history_team_club",
        ),
        ForeignKeyConstraint(
            ["game_id", "club_id"],
            ["games.id", "games.club_id"],
            ondelete="RESTRICT",
            name="fk_media_usage_history_game_club",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    media_asset_id: Mapped[str] = mapped_column(String(36), index=True)
    team_id: Mapped[str] = mapped_column(String(36), index=True)
    game_id: Mapped[str | None] = mapped_column(String(36), index=True)
    post_id: Mapped[str | None] = mapped_column(
        ForeignKey("posts.id", ondelete="SET NULL"), index=True
    )
    contribution_type: Mapped[str | None] = mapped_column(String(30), index=True)
    action: Mapped[str] = mapped_column(String(30), index=True)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class ClubMediaUsagePolicy(Base, Timestamped):
    """Tenant policy defining which media categories a contribution may use."""

    __tablename__ = "club_media_usage_policies"
    __table_args__ = (
        UniqueConstraint(
            "club_id", "contribution_type", name="uq_club_media_policy_contribution"
        ),
        CheckConstraint(
            "contribution_type IN ('announcement', 'reminder', 'result', 'live')",
            name="ck_club_media_policy_contribution_type",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(
        ForeignKey("clubs.id", ondelete="CASCADE"), index=True
    )
    contribution_type: Mapped[str] = mapped_column(String(30), index=True)
    allowed_media_categories: Mapped[list] = mapped_column(JSON, default=list)
    category_priority: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    updated_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class GameMediaPreference(Base, Timestamped):
    """Explicit or automatic media choice for one game and contribution type."""

    __tablename__ = "game_media_preferences"
    __table_args__ = (
        UniqueConstraint(
            "club_id",
            "game_id",
            "team_id",
            "contribution_type",
            name="uq_game_media_preference_scope",
        ),
        CheckConstraint(
            "selection_mode IN ('automatic', 'manual')",
            name="ck_game_media_preference_mode",
        ),
        CheckConstraint(
            "contribution_type IN ('announcement', 'reminder', 'result', 'live')",
            name="ck_game_media_preference_contribution_type",
        ),
        CheckConstraint(
            "selection_mode = 'automatic' OR selected_media_asset_id IS NOT NULL",
            name="ck_game_media_preference_manual_asset",
        ),
        ForeignKeyConstraint(
            ["game_id", "club_id"],
            ["games.id", "games.club_id"],
            ondelete="CASCADE",
            name="fk_game_media_preference_game_club",
        ),
        ForeignKeyConstraint(
            ["team_id", "club_id"],
            ["teams.id", "teams.club_id"],
            ondelete="RESTRICT",
            name="fk_game_media_preference_team_club",
        ),
        ForeignKeyConstraint(
            ["selected_media_asset_id", "club_id"],
            ["media_assets.id", "media_assets.club_id"],
            ondelete="RESTRICT",
            name="fk_game_media_preference_asset_club",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    game_id: Mapped[str] = mapped_column(String(36), index=True)
    team_id: Mapped[str] = mapped_column(String(36), index=True)
    contribution_type: Mapped[str] = mapped_column(String(30), index=True)
    selection_mode: Mapped[str] = mapped_column(
        String(20), default="automatic", server_default="automatic"
    )
    selected_media_asset_id: Mapped[str | None] = mapped_column(String(36), index=True)
    allow_used_once: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    selected_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StoryRule(Base, Timestamped):
    __tablename__ = "story_rules"
    __table_args__ = (
        UniqueConstraint("club_id", "team_id", "name", name="uq_story_rules_club_team_name"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"))
    name: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    post_type: Mapped[str] = mapped_column(String(30))
    reference: Mapped[str] = mapped_column(String(40))
    direction: Mapped[str] = mapped_column(String(10), default="before")
    offset_minutes: Mapped[int] = mapped_column(Integer, default=0)
    fixed_time: Mapped[str | None] = mapped_column(String(5))
    timing_mode: Mapped[str] = mapped_column(
        String(20), default="relative", server_default="relative", nullable=False
    )
    weekday_times: Mapped[dict] = mapped_column(
        JSON, default=dict, server_default="{}", nullable=False
    )
    weekday_targets: Mapped[dict] = mapped_column(
        JSON, default=dict, server_default="{}", nullable=False
    )
    media_slot: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    next_day: Mapped[bool] = mapped_column(Boolean, default=False)
    template: Mapped[str] = mapped_column(String(100))
    prompt_template: Mapped[str] = mapped_column(String(160), default="default-image-story")
    text_variant: Mapped[str | None] = mapped_column(String(100))
    instagram_page_id: Mapped[str | None] = mapped_column(ForeignKey("instagram_pages.id"))
    priority: Mapped[int] = mapped_column(Integer, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    reuse_media: Mapped[bool] = mapped_column(Boolean, default=True)


class ContentRuleSet(Base, Timestamped):
    """Versioned generation policy at club, team, or game scope.

    ``scope_key`` deliberately avoids nullable-column uniqueness semantics and
    is also safe to include in cache and idempotency keys.
    """

    __tablename__ = "content_rule_sets"
    __table_args__ = (
        UniqueConstraint(
            "club_id",
            "scope_key",
            "post_type",
            "rule_version",
            name="uq_content_rule_set_scope_version",
        ),
        CheckConstraint("scope_type IN ('club', 'team', 'game')", name="ck_content_rule_scope"),
        CheckConstraint(
            "feed_generation_count >= 0 AND feed_generation_count <= 10",
            name="ck_content_rule_feed_count",
        ),
        CheckConstraint(
            "story_generation_count >= 0 AND story_generation_count <= 10",
            name="ck_content_rule_story_count",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    scope_type: Mapped[str] = mapped_column(String(10))
    scope_key: Mapped[str] = mapped_column(String(80), index=True)
    team_id: Mapped[str | None] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), index=True
    )
    game_id: Mapped[str | None] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), index=True
    )
    post_type: Mapped[str] = mapped_column(String(30), index=True)
    rule_version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    feed_generation_count: Mapped[int] = mapped_column(Integer, default=1)
    story_generation_count: Mapped[int] = mapped_column(Integer, default=1)
    feed_publish_variants: Mapped[list] = mapped_column(JSON, default=lambda: [1])
    story_publish_variants: Mapped[list] = mapped_column(JSON, default=lambda: [1])
    approval_policy: Mapped[str] = mapped_column(String(30), default="manual")
    inherited_from_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_rule_sets.id", ondelete="SET NULL")
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PublicationRuleSlot(Base, Timestamped):
    """One deterministic publication slot within a content rule set."""

    __tablename__ = "publication_rule_slots"
    __table_args__ = (
        UniqueConstraint("rule_set_id", "slot_key", name="uq_publication_rule_slot_key"),
        CheckConstraint("media_kind IN ('feed', 'story')", name="ck_publication_rule_media_kind"),
        CheckConstraint(
            "timing_model IN ('relative', 'weekday_fixed', 'result_detected', 'manual')",
            name="ck_publication_rule_timing_model",
        ),
        CheckConstraint("variant_number > 0", name="ck_publication_rule_variant"),
        CheckConstraint(
            "match_weekday IS NULL OR (match_weekday >= 0 AND match_weekday <= 6)",
            name="ck_publication_rule_match_weekday",
        ),
        CheckConstraint(
            "target_weekday IS NULL OR (target_weekday >= 0 AND target_weekday <= 6)",
            name="ck_publication_rule_target_weekday",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    rule_set_id: Mapped[str] = mapped_column(
        ForeignKey("content_rule_sets.id", ondelete="CASCADE"), index=True
    )
    slot_key: Mapped[str] = mapped_column(String(100))
    label: Mapped[str] = mapped_column(String(160))
    media_kind: Mapped[str] = mapped_column(String(10))
    variant_number: Mapped[int] = mapped_column(Integer, default=1)
    timing_model: Mapped[str] = mapped_column(String(30), default="manual")
    reference: Mapped[str | None] = mapped_column(String(40))
    direction: Mapped[str | None] = mapped_column(String(10))
    offset_minutes: Mapped[int | None] = mapped_column(Integer)
    match_weekday: Mapped[int | None] = mapped_column(Integer, index=True)
    target_weekday: Mapped[int | None] = mapped_column(Integer)
    local_time: Mapped[str | None] = mapped_column(String(5))
    timezone: Mapped[str] = mapped_column(String(50), default="Europe/Berlin")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    instagram_page_id: Mapped[str | None] = mapped_column(
        ForeignKey("instagram_pages.id", ondelete="SET NULL"), index=True
    )
    template: Mapped[str | None] = mapped_column(String(100))
    reuse_media: Mapped[bool] = mapped_column(Boolean, default=False)
    legacy_story_rule_id: Mapped[str | None] = mapped_column(
        ForeignKey("story_rules.id", ondelete="SET NULL"), index=True
    )


class Post(Base, Timestamped):
    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("id", "club_id", name="uq_posts_id_club"),
        UniqueConstraint(
            "club_id",
            "game_id",
            "post_type",
            "active_key",
            name="uq_posts_club_game_type_active",
        ),
        UniqueConstraint(
            "club_id", "manual_submission_id", name="uq_posts_club_manual_submission_id"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    game_id: Mapped[str | None] = mapped_column(ForeignKey("games.id"), nullable=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"))
    instagram_page_id: Mapped[str] = mapped_column(ForeignKey("instagram_pages.id"))
    post_type: Mapped[str] = mapped_column(String(30))
    active_key: Mapped[str] = mapped_column(String(20), default="active")
    manual_submission_id: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[PostStatus] = mapped_column(Enum(PostStatus), default=PostStatus.DETECTED)
    text: Mapped[str | None] = mapped_column(Text)
    text_version: Mapped[int] = mapped_column(Integer, default=1)
    text_selection_mode: Mapped[str] = mapped_column(
        String(20), default="auto_latest", server_default="auto_latest"
    )
    selected_text_version_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "post_text_versions.id",
            name="fk_posts_selected_text_version",
            use_alter=True,
            ondelete="SET NULL",
        )
    )
    latest_text_version_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "post_text_versions.id",
            name="fk_posts_latest_text_version",
            use_alter=True,
            ondelete="SET NULL",
        )
    )
    feed_path: Mapped[str | None] = mapped_column(String(800))
    feed_version: Mapped[int] = mapped_column(Integer, default=1)
    media_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    design_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    critical_warnings: Mapped[list] = mapped_column(JSON, default=list)
    publishing_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    approved_version: Mapped[int | None] = mapped_column(Integer)
    approved_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_edited_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))


class PostTextVersion(Base, Timestamped):
    """Immutable, tenant-scoped history of a post caption."""

    __tablename__ = "post_text_versions"
    __table_args__ = (
        UniqueConstraint("post_id", "version_number", name="uq_post_text_version"),
        CheckConstraint("version_number > 0", name="ck_post_text_version_number"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    generation_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL"), index=True
    )
    prompt_template_id: Mapped[str | None] = mapped_column(
        ForeignKey("prompt_templates.id", ondelete="SET NULL")
    )
    prompt_version: Mapped[int | None] = mapped_column(Integer)
    prompt_checksum: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(30), default="generation")
    validation_status: Mapped[str] = mapped_column(String(30), default="valid")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class GeneratedMediaSlot(Base, Timestamped):
    """A stable feed/story output; variants and versions never replace it."""

    __tablename__ = "generated_media_slots"
    __table_args__ = (
        UniqueConstraint("club_id", "post_id", "slot_key", name="uq_generated_media_slot_key"),
        UniqueConstraint("id", "club_id", name="uq_generated_media_slots_id_club"),
        CheckConstraint("media_kind IN ('feed', 'story')", name="ck_generated_media_slot_kind"),
        CheckConstraint("variant_number > 0", name="ck_generated_media_slot_variant"),
        CheckConstraint("output_position > 0", name="ck_generated_media_slot_position"),
        CheckConstraint(
            "selection_mode IN ('auto_latest', 'manual')",
            name="ck_generated_media_slot_selection_mode",
        ),
        ForeignKeyConstraint(
            ["selected_version_id", "id"],
            ["generated_media_versions.id", "generated_media_versions.slot_id"],
            name="fk_generated_media_slots_selected_version_same_slot",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["latest_version_id", "id"],
            ["generated_media_versions.id", "generated_media_versions.slot_id"],
            name="fk_generated_media_slots_latest_version_same_slot",
            use_alter=True,
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    game_id: Mapped[str | None] = mapped_column(
        ForeignKey("games.id", ondelete="SET NULL"), index=True
    )
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="RESTRICT"), index=True)
    story_rule_id: Mapped[str | None] = mapped_column(
        ForeignKey("story_rules.id", ondelete="SET NULL"), index=True
    )
    slot_key: Mapped[str] = mapped_column(String(120))
    media_kind: Mapped[str] = mapped_column(String(10), index=True)
    output_position: Mapped[int] = mapped_column(Integer, default=1)
    variant_number: Mapped[int] = mapped_column(Integer, default=1)
    label: Mapped[str] = mapped_column(String(180))
    selection_mode: Mapped[str] = mapped_column(String(20), default="auto_latest")
    selected_version_id: Mapped[str | None] = mapped_column(String(36))
    latest_version_id: Mapped[str | None] = mapped_column(String(36))


class GeneratedMediaVersion(Base, Timestamped):
    """Immutable technical result for one generated media slot."""

    __tablename__ = "generated_media_versions"
    __table_args__ = (
        UniqueConstraint("slot_id", "version_number", name="uq_generated_media_version"),
        UniqueConstraint("id", "club_id", name="uq_generated_media_versions_id_club"),
        UniqueConstraint("id", "slot_id", name="uq_generated_media_versions_id_slot"),
        CheckConstraint("version_number > 0", name="ck_generated_media_version_number"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    slot_id: Mapped[str] = mapped_column(
        ForeignKey("generated_media_slots.id", ondelete="CASCADE"), index=True
    )
    version_number: Mapped[int] = mapped_column(Integer)
    media_path: Mapped[str] = mapped_column(String(800))
    checksum: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(80), default="image/png")
    file_size: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    generation_status: Mapped[str] = mapped_column(String(30), default="completed")
    validation_status: Mapped[str] = mapped_column(String(30), default="valid")
    generation_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL"), index=True
    )
    source_media_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="SET NULL")
    )
    prompt_template_id: Mapped[str | None] = mapped_column(
        ForeignKey("prompt_templates.id", ondelete="SET NULL")
    )
    prompt_version: Mapped[int | None] = mapped_column(Integer)
    prompt_checksum: Mapped[str | None] = mapped_column(String(64))
    logo_references: Mapped[dict] = mapped_column(JSON, default=dict)
    design_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    legacy_import: Mapped[bool] = mapped_column(Boolean, default=False)


class PublicationJob(Base, Timestamped):
    __tablename__ = "publication_jobs"
    __table_args__ = (
        UniqueConstraint("club_id", "idempotency_key", name="uq_publication_jobs_club_idempotency"),
        ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="RESTRICT",
            name="fk_publication_jobs_channel_connection_club",
        ),
        CheckConstraint(
            "channel_type IN ('instagram', 'facebook', 'whatsapp')",
            name="ck_publication_jobs_channel_type",
        ),
        CheckConstraint(
            "delivery_action IN ('publish', 'send')",
            name="ck_publication_jobs_delivery_action",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"))
    game_id: Mapped[str | None] = mapped_column(ForeignKey("games.id"), nullable=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"))
    instagram_page_id: Mapped[str | None] = mapped_column(
        ForeignKey("instagram_pages.id"), nullable=True
    )
    channel_type: Mapped[str] = mapped_column(
        String(20), default="instagram", server_default="instagram", index=True
    )
    channel_connection_id: Mapped[str | None] = mapped_column(String(36), index=True)
    content_type: Mapped[str | None] = mapped_column(String(40))
    target: Mapped[str | None] = mapped_column(String(200))
    delivery_action: Mapped[str] = mapped_column(
        String(20), default="publish", server_default="publish"
    )
    story_rule_id: Mapped[str | None] = mapped_column(ForeignKey("story_rules.id"))
    kind: Mapped[str] = mapped_column(String(10))
    media_path: Mapped[str] = mapped_column(String(800))
    text_snapshot: Mapped[str | None] = mapped_column(Text)
    text_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("post_text_versions.id", ondelete="SET NULL"), index=True
    )
    media_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("generated_media_versions.id", ondelete="SET NULL"), index=True
    )
    publication_rule_slot_id: Mapped[str | None] = mapped_column(
        ForeignKey("publication_rule_slots.id", ondelete="SET NULL"), index=True
    )
    schedule_source: Mapped[str] = mapped_column(
        String(30), default="legacy", server_default="legacy"
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    absolute_time: Mapped[bool] = mapped_column(Boolean, default=False)
    stale_time: Mapped[bool] = mapped_column(Boolean, default=False)
    approval_status: Mapped[str] = mapped_column(String(30), default="unapproved")
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.UNAPPROVED)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    platform_id: Mapped[str | None] = mapped_column(String(200))
    permalink: Mapped[str | None] = mapped_column(String(1000))
    error: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_post_version: Mapped[int | None] = mapped_column(Integer)


class PublicationMediaItem(Base, Timestamped):
    __tablename__ = "publication_media_items"
    __table_args__ = (
        UniqueConstraint("publication_job_id", "position", name="uq_publication_media_position"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    publication_job_id: Mapped[str] = mapped_column(
        ForeignKey("publication_jobs.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    media_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("generated_media_versions.id", ondelete="SET NULL"), index=True
    )
    media_path: Mapped[str] = mapped_column(String(800))
    checksum: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(80), default="image/png")
    file_size: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)


class GenerationJob(Base, Timestamped):
    __tablename__ = "generation_jobs"
    __table_args__ = (
        UniqueConstraint("club_id", "active_key", name="uq_generation_jobs_club_active_key"),
        UniqueConstraint("club_id", "idempotency_key", name="uq_generation_jobs_club_idempotency"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    job_type: Mapped[GenerationJobType] = mapped_column(Enum(GenerationJobType), index=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"), index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    post_id: Mapped[str | None] = mapped_column(ForeignKey("posts.id"), index=True)
    result_post_id: Mapped[str | None] = mapped_column(ForeignKey("posts.id"))
    post_type: Mapped[str] = mapped_column(String(30))
    requested_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    status: Mapped[GenerationJobStatus] = mapped_column(
        Enum(GenerationJobStatus), default=GenerationJobStatus.QUEUED, index=True
    )
    phase: Mapped[str] = mapped_column(String(40), default="preparing")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    planned_outputs: Mapped[int] = mapped_column(Integer, default=0)
    completed_outputs: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    locked_by: Mapped[str | None] = mapped_column(String(160))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error_category: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    active_key: Mapped[str | None] = mapped_column(String(255))
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InstagramConnection(Base, Timestamped):
    __tablename__ = "instagram_connections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    instagram_page_id: Mapped[str] = mapped_column(
        ForeignKey("instagram_pages.id", ondelete="CASCADE"), unique=True, index=True
    )
    instagram_user_id: Mapped[str | None] = mapped_column(String(100), index=True)
    confirmed_username: Mapped[str | None] = mapped_column(String(100))
    account_type: Mapped[str | None] = mapped_column(String(40))
    login_variant: Mapped[str] = mapped_column(String(40), default="instagram_login")
    api_version: Mapped[str] = mapped_column(String(20), default="v23.0")
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(40), default="disconnected", index=True)
    encrypted_token: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    token_key_version: Mapped[str | None] = mapped_column(String(40))
    test_account: Mapped[bool] = mapped_column(Boolean, default=False)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SocialChannelConnection(Base, Timestamped):
    """Tenant-owned external channel without exposing provider credentials.

    Existing Instagram rows remain authoritative during the compatibility
    period and are linked through ``legacy_instagram_page_id``. New Facebook
    and WhatsApp credentials are encrypted with the existing Meta token key.
    """

    __tablename__ = "social_channel_connections"
    __table_args__ = (
        UniqueConstraint("id", "club_id", name="uq_social_channel_connections_id_club"),
        UniqueConstraint(
            "club_id",
            "channel_type",
            "external_account_id",
            name="uq_social_channel_external_account",
        ),
        CheckConstraint(
            "channel_type IN ('instagram', 'facebook', 'whatsapp')",
            name="ck_social_channel_type",
        ),
        ForeignKeyConstraint(
            ["legacy_instagram_page_id", "club_id"],
            ["instagram_pages.id", "instagram_pages.club_id"],
            ondelete="RESTRICT",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    channel_type: Mapped[str] = mapped_column(String(20), index=True)
    internal_name: Mapped[str] = mapped_column(String(120))
    display_name: Mapped[str] = mapped_column(String(160))
    username: Mapped[str | None] = mapped_column(String(120))
    external_account_id: Mapped[str | None] = mapped_column(String(160), index=True)
    parent_business_id: Mapped[str | None] = mapped_column(String(160))
    phone_number_id: Mapped[str | None] = mapped_column(String(160), index=True)
    display_phone_number: Mapped[str | None] = mapped_column(String(40))
    legacy_instagram_page_id: Mapped[str | None] = mapped_column(
        String(36), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(40), default="setup_required", index=True)
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    encrypted_token: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    token_key_version: Mapped[str | None] = mapped_column(String(40))
    api_version: Mapped[str | None] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    publishing_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    automatic_delivery_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PostChannelContent(Base, Timestamped):
    """Editable, tenant-bound text variant for one concrete target channel."""

    __tablename__ = "post_channel_contents"
    __table_args__ = (
        UniqueConstraint(
            "club_id",
            "post_id",
            "channel_connection_id",
            name="uq_post_channel_content_target",
        ),
        ForeignKeyConstraint(
            ["post_id", "club_id"],
            ["posts.id", "posts.club_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "channel_type IN ('facebook', 'whatsapp')",
            name="ck_post_channel_content_type",
        ),
        CheckConstraint(
            "source IN ('derived', 'manual', 'ai')",
            name="ck_post_channel_content_source",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    post_id: Mapped[str] = mapped_column(String(36), index=True)
    channel_connection_id: Mapped[str] = mapped_column(String(36), index=True)
    channel_type: Mapped[str] = mapped_column(String(20), index=True)
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="derived")
    updated_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )


class SocialChannelOAuthState(Base):
    __tablename__ = "social_channel_oauth_states"
    __table_args__ = (
        CheckConstraint(
            "channel_type IN ('facebook', 'whatsapp')",
            name="ck_social_channel_oauth_type",
        ),
        ForeignKeyConstraint(
            ["user_id", "club_id"],
            ["users.id", "users.club_id"],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    channel_type: Mapped[str] = mapped_column(String(20), index=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    redirect_uri: Mapped[str] = mapped_column(String(1000))
    encrypted_selection_payload: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TeamChannelAssignment(Base, Timestamped):
    __tablename__ = "team_channel_assignments"
    __table_args__ = (
        UniqueConstraint("team_id", "channel_connection_id", name="uq_team_channel_assignment"),
        ForeignKeyConstraint(
            ["team_id", "club_id"], ["teams.id", "teams.club_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(String(36), index=True)
    team_id: Mapped[str] = mapped_column(String(36), index=True)
    channel_connection_id: Mapped[str] = mapped_column(String(36), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    announcement_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    result_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    story_enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class WhatsAppRecipient(Base, Timestamped):
    __tablename__ = "whatsapp_recipients"
    __table_args__ = (
        UniqueConstraint(
            "club_id",
            "channel_connection_id",
            "normalized_phone",
            name="uq_whatsapp_recipient_phone",
        ),
        UniqueConstraint("id", "club_id", name="uq_whatsapp_recipients_id_club"),
        ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "opt_in_status IN ('pending', 'confirmed', 'revoked')",
            name="ck_whatsapp_recipient_opt_in",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    channel_connection_id: Mapped[str] = mapped_column(String(36), index=True)
    normalized_phone: Mapped[str] = mapped_column(String(32), index=True)
    display_name: Mapped[str | None] = mapped_column(String(160))
    opt_in_status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    opt_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opt_in_source: Mapped[str | None] = mapped_column(String(160))
    opt_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    preferred_message_types: Mapped[list] = mapped_column(JSON, default=list)
    provider_recipient_id: Mapped[str | None] = mapped_column(String(160))


class WhatsAppAudience(Base, Timestamped):
    """Tenant-owned WhatsApp target: official group or opt-in recipient list."""

    __tablename__ = "whatsapp_audiences"
    __table_args__ = (
        UniqueConstraint(
            "club_id",
            "channel_connection_id",
            "name",
            name="uq_whatsapp_audience_name",
        ),
        UniqueConstraint("id", "club_id", name="uq_whatsapp_audiences_id_club"),
        ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "audience_type IN ('group','recipient_list')",
            name="ck_whatsapp_audience_type",
        ),
        CheckConstraint(
            "eligibility_status IN ('available','not_available','unknown','connection_error')",
            name="ck_whatsapp_audience_eligibility",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    channel_connection_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(String(500))
    audience_type: Mapped[str] = mapped_column(String(30), index=True)
    external_group_id: Mapped[str | None] = mapped_column(String(200), index=True)
    eligibility_status: Mapped[str] = mapped_column(String(30), default="unknown")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class WhatsAppAudienceRecipient(Base):
    __tablename__ = "whatsapp_audience_recipients"
    __table_args__ = (
        ForeignKeyConstraint(
            ["audience_id", "club_id"],
            ["whatsapp_audiences.id", "whatsapp_audiences.club_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["recipient_id", "club_id"],
            ["whatsapp_recipients.id", "whatsapp_recipients.club_id"],
            ondelete="CASCADE",
        ),
    )
    audience_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    recipient_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)


class WhatsAppMessageTemplate(Base, Timestamped):
    __tablename__ = "whatsapp_message_templates"
    __table_args__ = (
        UniqueConstraint(
            "club_id",
            "channel_connection_id",
            "provider_template_id",
            name="uq_whatsapp_provider_template",
        ),
        UniqueConstraint("id", "club_id", name="uq_whatsapp_templates_id_club"),
        ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    channel_connection_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(160))
    provider_template_id: Mapped[str] = mapped_column(String(160))
    language: Mapped[str] = mapped_column(String(20), default="de")
    category: Mapped[str] = mapped_column(String(40), default="utility")
    message_type: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    components: Mapped[list] = mapped_column(JSON, default=list)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class ChannelDeliveryAttempt(Base, Timestamped):
    __tablename__ = "channel_delivery_attempts"
    __table_args__ = (
        UniqueConstraint("club_id", "idempotency_key", name="uq_channel_delivery_idempotency"),
        ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["recipient_id", "club_id"],
            ["whatsapp_recipients.id", "whatsapp_recipients.club_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["template_id", "club_id"],
            ["whatsapp_message_templates.id", "whatsapp_message_templates.club_id"],
            ondelete="RESTRICT",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    publication_job_id: Mapped[str] = mapped_column(
        ForeignKey("publication_jobs.id", ondelete="CASCADE"), index=True
    )
    channel_connection_id: Mapped[str] = mapped_column(String(36), index=True)
    recipient_id: Mapped[str | None] = mapped_column(String(36), index=True)
    template_id: Mapped[str | None] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(20), default="publish")
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    platform_id: Mapped[str | None] = mapped_column(String(200), index=True)
    cost_amount: Mapped[float | None] = mapped_column(Numeric(12, 6))
    cost_currency: Mapped[str | None] = mapped_column(String(8))
    sanitized_response: Mapped[dict] = mapped_column(JSON, default=dict)
    error_category: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(180))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MetaWebhookEvent(Base):
    __tablename__ = "meta_webhook_events"
    __table_args__ = (
        UniqueConstraint(
            "club_id",
            "channel_type",
            "provider_event_key",
            name="uq_meta_webhook_event_key",
        ),
        ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    channel_type: Mapped[str] = mapped_column(String(20), index=True)
    channel_connection_id: Mapped[str] = mapped_column(String(36), index=True)
    provider_event_key: Mapped[str] = mapped_column(String(200))
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    payload_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="received", index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LiveReporter(Base, Timestamped):
    """A tenant-owned person allowed to report events for selected teams."""

    __tablename__ = "live_reporters"
    __table_args__ = (
        UniqueConstraint("id", "club_id", name="uq_live_reporters_id_club"),
        UniqueConstraint(
            "club_id",
            "channel_connection_id",
            "normalized_phone",
            name="uq_live_reporter_phone",
        ),
        ForeignKeyConstraint(
            ["user_id", "club_id"],
            ["users.id", "users.club_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["active_game_id", "club_id"],
            ["games.id", "games.club_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "user_id IS NOT NULL OR normalized_phone IS NOT NULL",
            name="ck_live_reporter_identity",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), index=True)
    channel_connection_id: Mapped[str | None] = mapped_column(String(36), index=True)
    normalized_phone: Mapped[str | None] = mapped_column(String(32), index=True)
    display_name: Mapped[str] = mapped_column(String(160))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    all_teams: Mapped[bool] = mapped_column(Boolean, default=False)
    trusted_auto_confirm: Mapped[bool] = mapped_column(Boolean, default=False)
    may_correct: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_event_types: Mapped[list] = mapped_column(JSON, default=list)
    active_game_id: Mapped[str | None] = mapped_column(String(36), index=True)
    active_game_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LiveReporterTeam(Base):
    __tablename__ = "live_reporter_teams"
    __table_args__ = (
        ForeignKeyConstraint(
            ["reporter_id", "club_id"],
            ["live_reporters.id", "live_reporters.club_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["team_id", "club_id"],
            ["teams.id", "teams.club_id"],
            ondelete="CASCADE",
        ),
    )
    reporter_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    team_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)


class LiveGameState(Base, Timestamped):
    """Materialized state derived from confirmed, immutable match events."""

    __tablename__ = "live_game_states"
    __table_args__ = (
        UniqueConstraint("game_id", name="uq_live_game_state_game"),
        UniqueConstraint("id", "club_id", name="uq_live_game_states_id_club"),
        ForeignKeyConstraint(
            ["game_id", "club_id"], ["games.id", "games.club_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["team_id", "club_id"], ["teams.id", "teams.club_id"], ondelete="CASCADE"
        ),
        CheckConstraint("home_score >= 0 AND away_score >= 0", name="ck_live_score_nonnegative"),
        CheckConstraint(
            "phase IN ('scheduled','first_half','halftime','second_half','interrupted','finished','abandoned')",
            name="ck_live_game_phase",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    game_id: Mapped[str] = mapped_column(String(36), index=True)
    team_id: Mapped[str] = mapped_column(String(36), index=True)
    phase: Mapped[str] = mapped_column(String(30), default="scheduled", index=True)
    home_score: Mapped[int] = mapped_column(Integer, default=0)
    away_score: Mapped[int] = mapped_column(Integer, default=0)
    minute: Mapped[int | None] = mapped_column(Integer)
    stoppage_minute: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(40), default="dashboard")
    last_event_id: Mapped[str | None] = mapped_column(String(36), index=True)
    last_event_sequence: Mapped[int] = mapped_column(Integer, default=0)
    live_publishing_paused: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MatchEvent(Base, Timestamped):
    """Neutral, append-only football event used by every input provider."""

    __tablename__ = "match_events"
    __table_args__ = (
        UniqueConstraint("id", "club_id", name="uq_match_events_id_club"),
        UniqueConstraint("club_id", "idempotency_key", name="uq_match_events_idempotency"),
        ForeignKeyConstraint(
            ["game_id", "club_id"], ["games.id", "games.club_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["team_id", "club_id"], ["teams.id", "teams.club_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["reporter_id", "club_id"],
            ["live_reporters.id", "live_reporters.club_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["supersedes_event_id", "club_id"],
            ["match_events.id", "match_events.club_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "event_type IN ('kickoff','goal','opponent_goal','own_goal','penalty_scored','penalty_missed','yellow_card','second_yellow_card','red_card','substitution','halftime','second_half','fulltime','interruption','resume','abandoned','comment','score_correction','event_correction')",
            name="ck_match_event_type",
        ),
        CheckConstraint(
            "status IN ('pending','confirmed','rejected','superseded')",
            name="ck_match_event_status",
        ),
        CheckConstraint("minute IS NULL OR minute BETWEEN 0 AND 150", name="ck_match_event_minute"),
        CheckConstraint(
            "home_score_after IS NULL OR home_score_after >= 0",
            name="ck_match_event_home_score",
        ),
        CheckConstraint(
            "away_score_after IS NULL OR away_score_after >= 0",
            name="ck_match_event_away_score",
        ),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_match_event_confidence"),
        CheckConstraint("event_sequence >= 1", name="ck_match_event_sequence"),
        CheckConstraint(
            "team_side IS NULL OR team_side IN ('own','opponent','neutral')",
            name="ck_match_event_team_side",
        ),
        UniqueConstraint("club_id", "game_id", "event_sequence", name="uq_match_event_sequence"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    game_id: Mapped[str] = mapped_column(String(36), index=True)
    team_id: Mapped[str] = mapped_column(String(36), index=True)
    reporter_id: Mapped[str | None] = mapped_column(String(36), index=True)
    channel_connection_id: Mapped[str | None] = mapped_column(String(36), index=True)
    provider: Mapped[str] = mapped_column(String(40), default="dashboard", index=True)
    provider_event_id: Mapped[str | None] = mapped_column(String(200), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    event_sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    team_side: Mapped[str | None] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    minute: Mapped[int | None] = mapped_column(Integer)
    stoppage_minute: Mapped[int | None] = mapped_column(Integer)
    home_score_after: Mapped[int | None] = mapped_column(Integer)
    away_score_after: Mapped[int | None] = mapped_column(Integer)
    own_score_after: Mapped[int | None] = mapped_column(Integer)
    opponent_score_after: Mapped[int | None] = mapped_column(Integer)
    player_name: Mapped[str | None] = mapped_column(String(160))
    player_id: Mapped[str | None] = mapped_column(String(36))
    assist_name: Mapped[str | None] = mapped_column(String(160))
    assist_player_id: Mapped[str | None] = mapped_column(String(36))
    related_player_name: Mapped[str | None] = mapped_column(String(160))
    card_color: Mapped[str | None] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(String(250))
    comment: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), default=1)
    needs_confirmation: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    supersedes_event_id: Mapped[str | None] = mapped_column(String(36), index=True)
    raw_text_digest: Mapped[str | None] = mapped_column(String(64))
    source_sender_digest: Mapped[str | None] = mapped_column(String(64))
    sanitized_input: Mapped[str | None] = mapped_column(String(500))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    corrected_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class LiveEventRule(Base, Timestamped):
    __tablename__ = "live_event_rules"
    __table_args__ = (
        UniqueConstraint("club_id", "team_id", "event_type", name="uq_live_event_rule_team_type"),
        ForeignKeyConstraint(
            ["team_id", "club_id"], ["teams.id", "teams.club_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["whatsapp_audience_id", "club_id"],
            ["whatsapp_audiences.id", "whatsapp_audiences.club_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "delivery_mode IN ('off','manual','automatic')",
            name="ck_live_event_rule_delivery_mode",
        ),
        CheckConstraint(
            "audience_type IN ('dashboard','opt_in_recipients','eligible_group')",
            name="ck_live_event_rule_audience",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    team_id: Mapped[str] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    delivery_mode: Mapped[str] = mapped_column(String(20), default="off")
    audience_type: Mapped[str] = mapped_column(String(30), default="dashboard")
    whatsapp_audience_id: Mapped[str | None] = mapped_column(String(36), index=True)
    channel_types: Mapped[list] = mapped_column(JSON, default=lambda: ["dashboard"])
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    require_confirmation: Mapped[bool] = mapped_column(Boolean, default=True)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)


class LiveEventDelivery(Base, Timestamped):
    __tablename__ = "live_event_deliveries"
    __table_args__ = (
        UniqueConstraint("id", "club_id", name="uq_live_event_deliveries_id_club"),
        UniqueConstraint("club_id", "idempotency_key", name="uq_live_delivery_idempotency"),
        ForeignKeyConstraint(
            ["event_id", "club_id"],
            ["match_events.id", "match_events.club_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["channel_connection_id", "club_id"],
            ["social_channel_connections.id", "social_channel_connections.club_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["whatsapp_audience_id", "club_id"],
            ["whatsapp_audiences.id", "whatsapp_audiences.club_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "channel_type IN ('dashboard','instagram','facebook','whatsapp')",
            name="ck_live_delivery_channel",
        ),
        CheckConstraint(
            "status IN ('awaiting_approval','queued','processing','sent','delivered','failed','cancelled','blocked')",
            name="ck_live_delivery_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    event_id: Mapped[str] = mapped_column(String(36), index=True)
    rule_id: Mapped[str | None] = mapped_column(
        ForeignKey("live_event_rules.id", ondelete="SET NULL"), index=True
    )
    channel_type: Mapped[str] = mapped_column(String(20), index=True)
    channel_connection_id: Mapped[str | None] = mapped_column(String(36), index=True)
    whatsapp_audience_id: Mapped[str | None] = mapped_column(String(36), index=True)
    publication_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("publication_jobs.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="awaiting_approval", index=True)
    target: Mapped[str | None] = mapped_column(String(200))
    message_snapshot: Mapped[str | None] = mapped_column(Text)
    platform_id: Mapped[str | None] = mapped_column(String(200))
    last_error: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(220))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LiveDeliveryAttempt(Base, Timestamped):
    """One idempotent external send for a live-event delivery.

    A recipient-list delivery can create one attempt per opted-in recipient.  A
    group delivery uses a null recipient and the immutable provider group ID
    stored on the audience.  Provider payloads and access tokens are never
    persisted here.
    """

    __tablename__ = "live_delivery_attempts"
    __table_args__ = (
        UniqueConstraint("club_id", "idempotency_key", name="uq_live_delivery_attempt_idempotency"),
        ForeignKeyConstraint(
            ["delivery_id", "club_id"],
            ["live_event_deliveries.id", "live_event_deliveries.club_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["recipient_id", "club_id"],
            ["whatsapp_recipients.id", "whatsapp_recipients.club_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["template_id", "club_id"],
            ["whatsapp_message_templates.id", "whatsapp_message_templates.club_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('queued','processing','sent','delivered','read','failed','uncertain','cancelled')",
            name="ck_live_delivery_attempt_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    delivery_id: Mapped[str] = mapped_column(String(36), index=True)
    recipient_id: Mapped[str | None] = mapped_column(String(36), index=True)
    template_id: Mapped[str | None] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    platform_id: Mapped[str | None] = mapped_column(String(200), index=True)
    error_category: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    sanitized_response: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(240))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InstagramOAuthState(Base):
    __tablename__ = "instagram_oauth_states"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    instagram_page_id: Mapped[str] = mapped_column(
        ForeignKey("instagram_pages.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    redirect_uri: Mapped[str] = mapped_column(String(1000))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PublicMediaGrant(Base):
    __tablename__ = "public_media_grants"
    __table_args__ = (
        UniqueConstraint("club_id", "active_key", name="uq_public_media_grants_club_active"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    publication_job_id: Mapped[str] = mapped_column(
        ForeignKey("publication_jobs.id", ondelete="CASCADE"), index=True
    )
    publication_media_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("publication_media_items.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    active_key: Mapped[str | None] = mapped_column(String(120))
    media_path: Mapped[str] = mapped_column(String(800))
    file_checksum: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(80))
    file_size: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetch_count: Mapped[int] = mapped_column(Integer, default=0)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MetaPublishingAttempt(Base, Timestamped):
    __tablename__ = "meta_publishing_attempts"
    __table_args__ = (
        UniqueConstraint("club_id", "active_key", name="uq_meta_attempts_club_active"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    publication_job_id: Mapped[str] = mapped_column(
        ForeignKey("publication_jobs.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[str] = mapped_column(
        ForeignKey("instagram_connections.id", ondelete="RESTRICT"), index=True
    )
    public_media_grant_id: Mapped[str | None] = mapped_column(
        ForeignKey("public_media_grants.id", ondelete="SET NULL")
    )
    active_key: Mapped[str | None] = mapped_column(String(120))
    target_account_id: Mapped[str] = mapped_column(String(100))
    media_kind: Mapped[str] = mapped_column(String(20))
    local_media_version: Mapped[int] = mapped_column(Integer)
    media_path: Mapped[str] = mapped_column(String(800))
    file_checksum: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(20), default="validate-only")
    trigger_mode: Mapped[str] = mapped_column(
        String(20), default="manual", server_default="manual", index=True
    )
    phase: Mapped[str] = mapped_column(String(40), default="validating", index=True)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    meta_container_id: Mapped[str | None] = mapped_column(String(120), index=True)
    container_status: Mapped[str | None] = mapped_column(String(80))
    meta_media_id: Mapped[str | None] = mapped_column(String(120), index=True)
    permalink: Mapped[str | None] = mapped_column(String(1000))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sanitized_response: Mapped[dict] = mapped_column(JSON, default=dict)
    error_category: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MetaCarouselItem(Base, Timestamped):
    __tablename__ = "meta_carousel_items"
    __table_args__ = (
        UniqueConstraint("attempt_id", "position", name="uq_meta_carousel_position"),
        UniqueConstraint(
            "attempt_id",
            "publication_media_item_id",
            name="uq_meta_carousel_media_item",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("meta_publishing_attempts.id", ondelete="CASCADE"), index=True
    )
    publication_media_item_id: Mapped[str] = mapped_column(
        ForeignKey("publication_media_items.id", ondelete="RESTRICT"), index=True
    )
    public_media_grant_id: Mapped[str | None] = mapped_column(
        ForeignKey("public_media_grants.id", ondelete="SET NULL")
    )
    position: Mapped[int] = mapped_column(Integer)
    meta_container_id: Mapped[str | None] = mapped_column(String(120), index=True)
    container_status: Mapped[str | None] = mapped_column(String(80))
    sanitized_response: Mapped[dict] = mapped_column(JSON, default=dict)
    error_category: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)


class MetaPublishConfirmation(Base):
    __tablename__ = "meta_publish_confirmations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("meta_publishing_attempts.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    purpose: Mapped[str] = mapped_column(String(30))
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(
            "(scope = 'platform' AND club_id IS NULL) OR (scope = 'club' AND club_id IS NOT NULL)",
            name="ck_audit_log_scope",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str | None] = mapped_column(
        ForeignKey("clubs.id", ondelete="RESTRICT"), index=True
    )
    scope: Mapped[str] = mapped_column(
        String(20), default="club", server_default="club", index=True
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"))
    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(36))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(80))


class SystemSetting(Base):
    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"))
    kind: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    read: Mapped[bool] = mapped_column(Boolean, default=False)


class FontAsset(Base, Timestamped):
    __tablename__ = "font_assets"
    __table_args__ = (
        UniqueConstraint("club_id", "name", name="uq_font_assets_club_name"),
        UniqueConstraint("club_id", "relative_path", name="uq_font_assets_club_path"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    family: Mapped[str] = mapped_column(String(160))
    relative_path: Mapped[str] = mapped_column(String(800))
    mime_type: Mapped[str] = mapped_column(String(80))
    size: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DesignTemplate(Base, Timestamped):
    __tablename__ = "design_templates"
    __table_args__ = (
        UniqueConstraint("club_id", "name", "version", name="uq_design_templates_club_version"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    post_type: Mapped[str] = mapped_column(String(30))
    media_kind: Mapped[str] = mapped_column(String(10))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    html_template: Mapped[str] = mapped_column(Text)
    css: Mapped[str] = mapped_column(Text)
    defaults: Mapped[dict] = mapped_column(JSON, default=dict)
    required_assets: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PromptTemplate(Base, Timestamped):
    __tablename__ = "prompt_templates"
    __table_args__ = (
        UniqueConstraint("name", "prompt_kind", "post_type", "media_kind", "version"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source_club_id: Mapped[str | None] = mapped_column(ForeignKey("clubs.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    prompt_kind: Mapped[str] = mapped_column(String(20))
    post_type: Mapped[str] = mapped_column(String(30))
    media_kind: Mapped[str] = mapped_column(String(10), default="none")
    prompt_body: Mapped[str] = mapped_column(Text)
    status: Mapped[PromptStatus] = mapped_column(
        Enum(PromptStatus), default=PromptStatus.ACTIVE, index=True
    )
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    allowed_variables: Mapped[list] = mapped_column(JSON, default=list)
    validation_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    change_description: Mapped[str | None] = mapped_column(String(500))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    style_direction: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(100))
    quality: Mapped[str] = mapped_column(String(20), default="medium")
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @validates("prompt_body")
    def _prepare_prompt_metadata(self, _key: str, value: str) -> str:
        """Maintain non-secret integrity metadata for every construction path."""
        self.checksum = hashlib.sha256(value.encode("utf-8")).hexdigest()
        self.allowed_variables = sorted(
            set(re.findall(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}", value))
        )
        return value


class ClubPromptOverride(Base, Timestamped):
    __tablename__ = "club_prompt_overrides"
    __table_args__ = (
        UniqueConstraint("club_id", "prompt_kind", "post_type", "media_kind", "version"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="CASCADE"), index=True)
    prompt_kind: Mapped[str] = mapped_column(String(20))
    post_type: Mapped[str] = mapped_column(String(30))
    media_kind: Mapped[str] = mapped_column(String(10), default="none")
    additional_instruction: Mapped[str | None] = mapped_column(Text)
    forbidden_phrases: Mapped[list] = mapped_column(JSON, default=list)
    preferred_design: Mapped[dict] = mapped_column(JSON, default=dict)
    sponsor_rules: Mapped[list] = mapped_column(JSON, default=list)
    club_rules: Mapped[list] = mapped_column(JSON, default=list)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[PromptStatus] = mapped_column(Enum(PromptStatus), default=PromptStatus.DRAFT)
    checksum: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))


class AiPromptDispatch(Base):
    """Exact provider input, visible only inside the PlatformAdmin area.

    Tenant-owned posts deliberately keep only non-secret prompt metadata.  This
    separate table allows platform support to audit actual provider requests
    without exposing protected prompt contents through club routes or exports.
    """

    __tablename__ = "ai_prompt_dispatches"
    __table_args__ = (
        UniqueConstraint("club_id", "idempotency_key", name="uq_ai_prompt_dispatch_idempotency"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    generation_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    post_id: Mapped[str | None] = mapped_column(
        ForeignKey("posts.id", ondelete="SET NULL"), index=True
    )
    team_id: Mapped[str | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), index=True
    )
    game_id: Mapped[str | None] = mapped_column(
        ForeignKey("games.id", ondelete="SET NULL"), index=True
    )
    prompt_kind: Mapped[str] = mapped_column(String(20), index=True)
    post_type: Mapped[str] = mapped_column(String(30), index=True)
    media_kind: Mapped[str] = mapped_column(String(10), default="none", index=True)
    provider: Mapped[str] = mapped_column(String(80), default="openai")
    model: Mapped[str] = mapped_column(String(120))
    prompt_template_id: Mapped[str | None] = mapped_column(
        ForeignKey("prompt_templates.id", ondelete="SET NULL"), index=True
    )
    prompt_name: Mapped[str | None] = mapped_column(String(160))
    prompt_version: Mapped[int | None] = mapped_column(Integer)
    prompt_checksum: Mapped[str] = mapped_column(String(64), index=True)
    rendered_prompt: Mapped[str] = mapped_column(Text)
    creative_profile_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    call_index: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="dispatched", index=True)
    error_summary: Mapped[str | None] = mapped_column(String(500))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    dispatched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CreativeFeedbackEvent(Base):
    """Append-only, tenant-scoped creative feedback ledger."""

    __tablename__ = "creative_feedback_events"
    __table_args__ = (
        UniqueConstraint(
            "club_id", "idempotency_key", name="uq_creative_feedback_idempotency"
        ),
        CheckConstraint(
            "modality IN ('image', 'text')", name="ck_creative_feedback_modality"
        ),
        CheckConstraint(
            "action IN ('selected', 'published', 'approved', 'rejected', "
            "'regenerated', 'reverted', 'manually_edited', 'replaced', 'skipped')",
            name="ck_creative_feedback_action",
        ),
        CheckConstraint(
            "source IN ('onboarding_explicit', 'onboarding_calibration', 'normal_usage', "
            "'explicit_feedback', 'platform_admin_override')",
            name="ck_creative_feedback_source",
        ),
        CheckConstraint(
            "sentiment IS NULL OR sentiment IN ('positive', 'negative', 'neutral')",
            name="ck_creative_feedback_sentiment",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(
        ForeignKey("clubs.id", ondelete="RESTRICT"), index=True
    )
    team_id: Mapped[str | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), index=True
    )
    post_id: Mapped[str | None] = mapped_column(
        ForeignKey("posts.id", ondelete="SET NULL"), index=True
    )
    generated_media_slot_id: Mapped[str | None] = mapped_column(
        ForeignKey("generated_media_slots.id", ondelete="SET NULL"), index=True
    )
    media_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("generated_media_versions.id", ondelete="SET NULL"), index=True
    )
    text_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("post_text_versions.id", ondelete="SET NULL"), index=True
    )
    generation_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL"), index=True
    )
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    modality: Mapped[str] = mapped_column(String(10), index=True)
    content_type: Mapped[str] = mapped_column(String(30), index=True)
    action: Mapped[str] = mapped_column(String(30), index=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    sentiment: Mapped[str | None] = mapped_column(String(10), index=True)
    reason_codes: Mapped[list] = mapped_column(JSON, default=list)
    free_text: Mapped[str | None] = mapped_column(Text)
    traits_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    event_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    correction_of_id: Mapped[str | None] = mapped_column(
        ForeignKey("creative_feedback_events.id", ondelete="RESTRICT"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, index=True
    )


class CreativePreferenceProfile(Base, Timestamped):
    """A learned profile version; older versions remain traceable."""

    __tablename__ = "creative_preference_profiles"
    __table_args__ = (
        UniqueConstraint(
            "club_id",
            "modality",
            "content_type",
            "profile_version",
            name="uq_creative_preference_profile_version",
        ),
        CheckConstraint(
            "modality IN ('image', 'text')", name="ck_creative_profile_modality"
        ),
        CheckConstraint(
            "status IN ('active', 'superseded', 'archived')",
            name="ck_creative_profile_status",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_creative_profile_confidence"
        ),
        CheckConstraint("sample_count >= 0", name="ck_creative_profile_sample_count"),
        CheckConstraint("profile_version > 0", name="ck_creative_profile_version"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(
        ForeignKey("clubs.id", ondelete="CASCADE"), index=True
    )
    modality: Mapped[str] = mapped_column(String(10), index=True)
    content_type: Mapped[str] = mapped_column(String(30), index=True)
    profile_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    preferences: Mapped[dict] = mapped_column(JSON, default=dict)
    avoidances: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), default=0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    source_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    learner_version: Mapped[str] = mapped_column(String(40), default="deterministic-v1")
    generated_by: Mapped[str] = mapped_column(
        String(80), default="deterministic_preference_learner"
    )
    build_reason: Mapped[str] = mapped_column(String(40), default="threshold")
    last_feedback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CreativeExampleReference(Base, Timestamped):
    __tablename__ = "creative_example_references"
    __table_args__ = (
        CheckConstraint(
            "modality IN ('image', 'text')", name="ck_creative_example_modality"
        ),
        CheckConstraint(
            "sentiment IN ('positive', 'negative')", name="ck_creative_example_sentiment"
        ),
        CheckConstraint(
            "(modality = 'image' AND media_version_id IS NOT NULL AND text_version_id IS NULL) "
            "OR (modality = 'text' AND text_version_id IS NOT NULL AND media_version_id IS NULL)",
            name="ck_creative_example_reference",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(
        ForeignKey("clubs.id", ondelete="CASCADE"), index=True
    )
    profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("creative_preference_profiles.id", ondelete="SET NULL"), index=True
    )
    modality: Mapped[str] = mapped_column(String(10), index=True)
    content_type: Mapped[str] = mapped_column(String(30), index=True)
    sentiment: Mapped[str] = mapped_column(String(10), index=True)
    media_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("generated_media_versions.id", ondelete="CASCADE"), index=True
    )
    text_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("post_text_versions.id", ondelete="CASCADE"), index=True
    )
    rank: Mapped[int] = mapped_column(Integer, default=0)
    traits: Mapped[dict] = mapped_column(JSON, default=dict)
    score: Mapped[float] = mapped_column(Numeric(8, 4), default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class CreativeProfileOverride(Base, Timestamped):
    __tablename__ = "creative_profile_overrides"
    __table_args__ = (
        UniqueConstraint(
            "club_id",
            "modality",
            "content_type",
            "override_version",
            name="uq_creative_profile_override_version",
        ),
        CheckConstraint(
            "modality IN ('image', 'text')", name="ck_creative_override_modality"
        ),
        CheckConstraint("override_version > 0", name="ck_creative_override_version"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(
        ForeignKey("clubs.id", ondelete="CASCADE"), index=True
    )
    modality: Mapped[str] = mapped_column(String(10), index=True)
    content_type: Mapped[str] = mapped_column(String(30), index=True)
    override_version: Mapped[int] = mapped_column(Integer)
    preferences: Mapped[dict] = mapped_column(JSON, default=dict)
    avoidances: Mapped[dict] = mapped_column(JSON, default=dict)
    trait: Mapped[str | None] = mapped_column(String(80), index=True)
    override_type: Mapped[str] = mapped_column(String(30), default="structured_profile")
    override_value: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class CreativeRecipe(Base, Timestamped):
    """Versioned platform-wide creative recipe without tenant data."""

    __tablename__ = "creative_recipes"
    __table_args__ = (
        UniqueConstraint("key", "recipe_version", name="uq_creative_recipe_version"),
        CheckConstraint(
            "modality IN ('image', 'text')", name="ck_creative_recipe_modality"
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'archived')", name="ck_creative_recipe_status"
        ),
        CheckConstraint("recipe_version > 0", name="ck_creative_recipe_version"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    key: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text)
    modality: Mapped[str] = mapped_column(String(10), index=True)
    content_type: Mapped[str] = mapped_column(String(30), index=True)
    recipe_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    traits: Mapped[dict] = mapped_column(JSON, default=dict)
    constraints: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VisualTraitAnalysisCache(Base, Timestamped):
    __tablename__ = "visual_trait_analysis_cache"
    __table_args__ = (
        UniqueConstraint(
            "club_id", "checksum", "analyzer_version", name="uq_visual_trait_analysis_cache"
        ),
        CheckConstraint(
            "status IN ('pending', 'completed', 'failed')",
            name="ck_visual_trait_analysis_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(
        ForeignKey("clubs.id", ondelete="CASCADE"), index=True
    )
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    media_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="SET NULL"), index=True
    )
    media_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("generated_media_versions.id", ondelete="SET NULL"), index=True
    )
    analyzer_version: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(80), default="openai")
    model: Mapped[str] = mapped_column(String(120))
    traits: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    error_summary: Mapped[str | None] = mapped_column(String(500))
    usage_ledger_entry_id: Mapped[str | None] = mapped_column(
        ForeignKey("usage_ledger_entries.id", ondelete="SET NULL")
    )


class ClubOnboardingSession(Base, Timestamped):
    __tablename__ = "club_onboarding_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('not_started', 'in_progress', 'calibration_pending', "
            "'completed', 'skipped')",
            name="ck_club_onboarding_status",
        ),
        CheckConstraint(
            "current_step >= 1 AND current_step <= 11", name="ck_club_onboarding_step"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(
        ForeignKey("clubs.id", ondelete="CASCADE"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="not_started", index=True)
    current_step: Mapped[int] = mapped_column(Integer, default=1)
    onboarding_version: Mapped[str] = mapped_column(String(20), default="1")
    completed_steps: Mapped[list] = mapped_column(JSON, default=list)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    skipped_calibration_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    last_actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class OnboardingCalibrationSample(Base, Timestamped):
    __tablename__ = "onboarding_calibration_samples"
    __table_args__ = (
        UniqueConstraint(
            "club_id",
            "session_id",
            "modality",
            "content_type",
            "sample_index",
            name="uq_onboarding_calibration_sample",
        ),
        CheckConstraint(
            "modality IN ('image', 'text')", name="ck_onboarding_sample_modality"
        ),
        CheckConstraint("sample_index > 0", name="ck_onboarding_sample_index"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(
        ForeignKey("clubs.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("club_onboarding_sessions.id", ondelete="CASCADE"), index=True
    )
    modality: Mapped[str] = mapped_column(String(10), index=True)
    content_type: Mapped[str] = mapped_column(String(30), index=True)
    recipe_key: Mapped[str] = mapped_column(String(100))
    sample_index: Mapped[int] = mapped_column(Integer)
    generation_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL"), index=True
    )
    media_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("generated_media_versions.id", ondelete="SET NULL")
    )
    text_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("post_text_versions.id", ondelete="SET NULL")
    )
    rendered_text: Mapped[str | None] = mapped_column(Text)
    preview_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    ranking: Mapped[int | None] = mapped_column(Integer)
    feedback: Mapped[dict] = mapped_column(JSON, default=dict)
    usage_ledger_entry_id: Mapped[str | None] = mapped_column(
        ForeignKey("usage_ledger_entries.id", ondelete="SET NULL")
    )
    publishing_blocked: Mapped[bool] = mapped_column(Boolean, default=True)


class ProviderSnapshot(Base):
    __tablename__ = "provider_snapshots"
    __table_args__ = (
        UniqueConstraint("club_id", "relative_path", name="uq_provider_snapshots_club_path"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"))
    source_url: Mapped[str] = mapped_column(String(1000))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    status_code: Mapped[int] = mapped_column(Integer)
    checksum: Mapped[str] = mapped_column(String(64))
    relative_path: Mapped[str] = mapped_column(String(800))
    parser_result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class FussballSyncState(Base):
    __tablename__ = "fussball_sync_states"
    team_id: Mapped[str] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="idle", index=True)
    next_poll_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(160))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("provider_snapshots.id", ondelete="SET NULL")
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_result_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class StorageObject(Base, Timestamped):
    __tablename__ = "storage_objects"
    __table_args__ = (
        UniqueConstraint("provider", "bucket", "object_key", name="uq_storage_object_location"),
        CheckConstraint("size_bytes >= 0", name="ck_storage_object_size"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    bucket: Mapped[str] = mapped_column(String(160))
    object_key: Mapped[str] = mapped_column(String(1000))
    category: Mapped[str] = mapped_column(String(80), index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    mime_type: Mapped[str] = mapped_column(String(100))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    references: Mapped[dict] = mapped_column(JSON, default=dict)
    billable: Mapped[bool] = mapped_column(Boolean, default=True)
    provider_metadata: Mapped[dict] = mapped_column(JSON, default=dict)


class StorageLedgerEntry(Base):
    __tablename__ = "storage_ledger_entries"
    __table_args__ = (
        UniqueConstraint("club_id", "idempotency_key", name="uq_storage_ledger_idempotency"),
        CheckConstraint("reserved_bytes >= 0", name="ck_storage_ledger_reserved"),
        CheckConstraint("actual_bytes >= 0", name="ck_storage_ledger_actual"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    storage_object_id: Mapped[str | None] = mapped_column(
        ForeignKey("storage_objects.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[LedgerStatus] = mapped_column(Enum(LedgerStatus), index=True)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    actual_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    actor_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class DirectUploadSession(Base, Timestamped):
    __tablename__ = "direct_upload_sessions"
    __table_args__ = (
        UniqueConstraint("club_id", "idempotency_key", name="uq_direct_upload_idempotency"),
        CheckConstraint("expected_size_bytes > 0", name="ck_direct_upload_expected_size"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="CASCADE"), index=True)
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    ledger_entry_id: Mapped[str] = mapped_column(
        ForeignKey("storage_ledger_entries.id", ondelete="CASCADE"), unique=True
    )
    provider: Mapped[str] = mapped_column(String(40))
    bucket: Mapped[str] = mapped_column(String(160))
    object_key: Mapped[str] = mapped_column(String(1000), unique=True)
    category: Mapped[str] = mapped_column(String(80), index=True)
    expected_size_bytes: Mapped[int] = mapped_column(BigInteger)
    expected_mime_type: Mapped[str] = mapped_column(String(100))
    expected_checksum: Mapped[str | None] = mapped_column(String(64))
    upload_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="reserved", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    storage_object_id: Mapped[str | None] = mapped_column(
        ForeignKey("storage_objects.id", ondelete="SET NULL")
    )
    idempotency_key: Mapped[str] = mapped_column(String(255))


class StorageReconciliationRun(Base, Timestamped):
    __tablename__ = "storage_reconciliation_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str | None] = mapped_column(
        ForeignKey("clubs.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30), index=True)
    checked_objects: Mapped[int] = mapped_column(Integer, default=0)
    missing_objects: Mapped[int] = mapped_column(Integer, default=0)
    unexpected_objects: Mapped[int] = mapped_column(Integer, default=0)
    size_mismatches: Mapped[int] = mapped_column(Integer, default=0)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    started_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UsageLedgerEntry(Base):
    __tablename__ = "usage_ledger_entries"
    __table_args__ = (
        UniqueConstraint("club_id", "idempotency_key", name="uq_usage_ledger_idempotency"),
        CheckConstraint("reserved_quantity >= 0", name="ck_usage_ledger_reserved"),
        CheckConstraint("actual_quantity >= 0", name="ck_usage_ledger_actual"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    generation_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL"), index=True
    )
    post_id: Mapped[str | None] = mapped_column(ForeignKey("posts.id", ondelete="SET NULL"))
    generation_type: Mapped[str] = mapped_column(String(40), index=True)
    provider: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(120))
    prompt_template_id: Mapped[str | None] = mapped_column(ForeignKey("prompt_templates.id"))
    prompt_version: Mapped[int | None] = mapped_column(Integer)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[UsageStatus] = mapped_column(Enum(UsageStatus), index=True)
    reserved_quantity: Mapped[int] = mapped_column(Integer, default=0)
    actual_quantity: Mapped[int] = mapped_column(Integer, default=0)
    provider_cost: Mapped[float | None] = mapped_column(Numeric(12, 6))
    billable: Mapped[bool] = mapped_column(Boolean, default=False)
    platform_test: Mapped[bool] = mapped_column(Boolean, default=False)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class PromptTestRun(Base, Timestamped):
    __tablename__ = "prompt_test_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))
    game_id: Mapped[str | None] = mapped_column(ForeignKey("games.id", ondelete="SET NULL"))
    old_prompt_template_id: Mapped[str | None] = mapped_column(ForeignKey("prompt_templates.id"))
    new_prompt_template_id: Mapped[str] = mapped_column(ForeignKey("prompt_templates.id"))
    fixture_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    result_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    provider_cost: Mapped[float | None] = mapped_column(Numeric(12, 6))
    status: Mapped[str] = mapped_column(String(30), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))


class RegistrationIntent(Base, Timestamped):
    __tablename__ = "registration_intents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(255), index=True)
    club_name: Mapped[str] = mapped_column(String(180))
    requested_plan_profile_id: Mapped[str | None] = mapped_column(ForeignKey("plan_profiles.id"))
    status: Mapped[str] = mapped_column(
        String(40), default="email_confirmation_pending", index=True
    )
    email_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    email_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    registration_metadata: Mapped[dict] = mapped_column(JSON, default=dict)


class ClubSubscription(Base, Timestamped):
    __tablename__ = "club_subscriptions"
    __table_args__ = (
        UniqueConstraint("club_id", "active_key", name="uq_club_active_subscription"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    club_id: Mapped[str] = mapped_column(ForeignKey("clubs.id", ondelete="RESTRICT"), index=True)
    plan_profile_id: Mapped[str] = mapped_column(ForeignKey("plan_profiles.id"))
    provider: Mapped[str] = mapped_column(String(40), default="mock")
    provider_customer_id: Mapped[str | None] = mapped_column(String(160))
    provider_subscription_id: Mapped[str | None] = mapped_column(String(160))
    contract_status: Mapped[str] = mapped_column(String(40), index=True)
    subscription_status: Mapped[str] = mapped_column(String(40), index=True)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    grace_period_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_key: Mapped[str | None] = mapped_column(String(40), default="active")
    last_payment_status: Mapped[str | None] = mapped_column(String(40))
    provider_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
