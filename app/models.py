import enum
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def now(): return datetime.now(timezone.utc)
def uid(): return str(uuid4())
class Role(str,enum.Enum): ADMIN="admin"; EDITOR="editor"; APPROVER="approver"; VIEWER="viewer"
class PostStatus(str,enum.Enum): DETECTED="detected"; PLANNED="planned"; CREATING="creating"; INCOMPLETE="incomplete"; PENDING="pending_approval"; REJECTED="rejected"; APPROVED="approved"; REAPPROVAL="reapproval_required"; SCHEDULED="scheduled"; PARTIAL="partially_published"; PUBLISHED="published"; ERROR="publishing_error"; CANCELLED="cancelled"
class JobStatus(str,enum.Enum): DRAFT="draft"; UNAPPROVED="unapproved"; APPROVED="approved"; SCHEDULED="scheduled"; WAITING="waiting"; PUBLISHING="publishing"; PUBLISHED="published"; RETRY="retry_scheduled"; FAILED="failed"; CANCELLED="cancelled"; SKIPPED="skipped"; UNCERTAIN="uncertain"
class GenerationJobStatus(str,enum.Enum): QUEUED="queued"; RUNNING="running"; RETRY_WAIT="retry_wait"; SUCCEEDED="succeeded"; FAILED="failed"; CANCELLED="cancelled"; MANUAL_REVIEW_REQUIRED="manual_review_required"
class GenerationJobType(str,enum.Enum): CREATE_POST="create_post"; RERENDER_POST="rerender_post"
class Timestamped:
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,onupdate=now)
    version: Mapped[int]=mapped_column(Integer,default=1,nullable=False)
class User(Base,Timestamped):
    __tablename__="users"; id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); email:Mapped[str]=mapped_column(String(255),unique=True,index=True); password_hash:Mapped[str]=mapped_column(String(255)); role:Mapped[Role]=mapped_column(Enum(Role),default=Role.VIEWER); all_teams:Mapped[bool]=mapped_column(Boolean,default=False); active:Mapped[bool]=mapped_column(Boolean,default=True); archived_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); failed_logins:Mapped[int]=mapped_column(Integer,default=0); locked_until:Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
class UserTeam(Base):
    __tablename__="user_teams"; user_id:Mapped[str]=mapped_column(ForeignKey("users.id",ondelete="CASCADE"),primary_key=True); team_id:Mapped[str]=mapped_column(ForeignKey("teams.id",ondelete="CASCADE"),primary_key=True)
class InstagramPage(Base,Timestamped):
    __tablename__="instagram_pages"; id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); internal_name:Mapped[str]=mapped_column(String(120)); display_name:Mapped[str]=mapped_column(String(120)); username:Mapped[str]=mapped_column(String(80)); profile_url:Mapped[str|None]=mapped_column(String(500)); account_id:Mapped[str|None]=mapped_column(String(100)); facebook_page_id:Mapped[str|None]=mapped_column(String(100)); club:Mapped[str]=mapped_column(String(160)); active:Mapped[bool]=mapped_column(Boolean,default=False); connection_status:Mapped[str]=mapped_column(String(30),default="unconfigured"); publishing_enabled:Mapped[bool]=mapped_column(Boolean,default=False); automatic_publishing_enabled:Mapped[bool]=mapped_column(Boolean,default=False,server_default="false"); automatic_publishing_confirmed_by:Mapped[str|None]=mapped_column(ForeignKey("users.id")); automatic_publishing_confirmed_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); allowed_types:Mapped[dict]=mapped_column(JSON,default=lambda:{"feed":True,"story":True}); defaults:Mapped[dict]=mapped_column(JSON,default=dict); archived_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); last_check_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); last_error:Mapped[str|None]=mapped_column(Text)
class Team(Base,Timestamped):
    __tablename__="teams"; id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); internal_name:Mapped[str]=mapped_column(String(120)); display_name:Mapped[str]=mapped_column(String(120)); short_name:Mapped[str]=mapped_column(String(30)); slug:Mapped[str]=mapped_column(String(80),unique=True); club:Mapped[str]=mapped_column(String(160)); active:Mapped[bool]=mapped_column(Boolean,default=True); archived_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); fussball_url:Mapped[str]=mapped_column(String(1000)); instagram_page_id:Mapped[str]=mapped_column(ForeignKey("instagram_pages.id")); media_subdir:Mapped[str]=mapped_column(String(500)); logo_path:Mapped[str|None]=mapped_column(String(500)); logo_asset_id:Mapped[str|None]=mapped_column(ForeignKey("logo_assets.id")); feed_template:Mapped[str]=mapped_column(String(100),default="default-feed"); story_templates:Mapped[list]=mapped_column(JSON,default=lambda:["default-story"]); primary_font:Mapped[str]=mapped_column(String(100),default="sans-serif"); secondary_font:Mapped[str]=mapped_column(String(100),default="sans-serif"); colors:Mapped[dict]=mapped_column(JSON,default=lambda:{"primary":"#172554","secondary":"#ffffff"}); text_style:Mapped[dict]=mapped_column(JSON,default=dict); hashtags:Mapped[list]=mapped_column(JSON,default=list); timezone:Mapped[str]=mapped_column(String(50),default="Europe/Berlin"); rules:Mapped[dict]=mapped_column(JSON,default=dict); publishing_enabled:Mapped[bool]=mapped_column(Boolean,default=True); last_sync_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); last_error_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); last_error:Mapped[str|None]=mapped_column(Text)
class Game(Base,Timestamped):
    __tablename__="games"; __table_args__=(UniqueConstraint("team_id","provider","external_id"),); id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); team_id:Mapped[str]=mapped_column(ForeignKey("teams.id"),index=True); provider:Mapped[str]=mapped_column(String(40),default="fussball.de"); external_id:Mapped[str]=mapped_column(String(200)); home_team:Mapped[str]=mapped_column(String(160)); away_team:Mapped[str]=mapped_column(String(160)); kickoff:Mapped[datetime]=mapped_column(DateTime(timezone=True)); original_kickoff:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); competition:Mapped[str|None]=mapped_column(String(160)); venue:Mapped[str|None]=mapped_column(String(250)); pitch:Mapped[str|None]=mapped_column(String(80)); status:Mapped[str]=mapped_column(String(40),default="scheduled"); home_score:Mapped[int|None]=mapped_column(Integer); away_score:Mapped[int|None]=mapped_column(Integer); halftime:Mapped[str|None]=mapped_column(String(20)); result_confirmed:Mapped[bool]=mapped_column(Boolean,default=False); source_url:Mapped[str]=mapped_column(String(1000)); checked_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); overrides:Mapped[dict]=mapped_column(JSON,default=dict); data_hash:Mapped[str|None]=mapped_column(String(64)); opponent_logo_id:Mapped[str|None]=mapped_column(ForeignKey("logo_assets.id"))
class LogoAsset(Base,Timestamped):
    __tablename__="logo_assets"
    __table_args__=(
        UniqueConstraint("logo_type","team_id","normalized_name","version"),
        Index(
            "uq_logo_assets_team_checksum",
            "team_id",
            "checksum",
            unique=True,
            postgresql_where=text("logo_type = 'team'"),
            sqlite_where=text("logo_type = 'team'"),
        ),
        Index(
            "uq_logo_assets_opponent_checksum",
            "checksum",
            unique=True,
            postgresql_where=text("logo_type = 'opponent'"),
            sqlite_where=text("logo_type = 'opponent'"),
        ),
    )
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid)
    logo_type:Mapped[str]=mapped_column(String(20),index=True)
    team_id:Mapped[str|None]=mapped_column(ForeignKey("teams.id"),index=True)
    display_name:Mapped[str]=mapped_column(String(200))
    normalized_name:Mapped[str]=mapped_column(String(200),index=True)
    original_path:Mapped[str]=mapped_column(String(800),unique=True)
    render_path:Mapped[str|None]=mapped_column(String(800),unique=True)
    original_filename:Mapped[str]=mapped_column(String(255))
    mime_type:Mapped[str]=mapped_column(String(80))
    size:Mapped[int]=mapped_column(Integer)
    width:Mapped[int]=mapped_column(Integer)
    height:Mapped[int]=mapped_column(Integer)
    checksum:Mapped[str]=mapped_column(String(64),index=True)
    active:Mapped[bool]=mapped_column(Boolean,default=True)
    archived_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    uploaded_by:Mapped[str]=mapped_column(ForeignKey("users.id"))
class MediaAsset(Base,Timestamped):
    __tablename__="media_assets"; __table_args__=(UniqueConstraint("team_id","relative_path"),); id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); team_id:Mapped[str]=mapped_column(ForeignKey("teams.id")); storage_kind:Mapped[str]=mapped_column(String(20),default="external",server_default="external",nullable=False); relative_path:Mapped[str]=mapped_column(String(800)); filename:Mapped[str]=mapped_column(String(255)); mime_type:Mapped[str]=mapped_column(String(80)); size:Mapped[int]=mapped_column(Integer); checksum:Mapped[str]=mapped_column(String(64)); mtime:Mapped[datetime]=mapped_column(DateTime(timezone=True)); player_name:Mapped[str|None]=mapped_column(String(160)); active:Mapped[bool]=mapped_column(Boolean,default=True); available:Mapped[bool]=mapped_column(Boolean,default=True); reserved_game_id:Mapped[str|None]=mapped_column(ForeignKey("games.id"),unique=True); uses:Mapped[int]=mapped_column(Integer,default=0)
class StoryRule(Base,Timestamped):
    __tablename__="story_rules"; __table_args__=(UniqueConstraint("team_id","name"),); id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); team_id:Mapped[str]=mapped_column(ForeignKey("teams.id")); name:Mapped[str]=mapped_column(String(120)); active:Mapped[bool]=mapped_column(Boolean,default=True); post_type:Mapped[str]=mapped_column(String(30)); reference:Mapped[str]=mapped_column(String(40)); direction:Mapped[str]=mapped_column(String(10),default="before"); offset_minutes:Mapped[int]=mapped_column(Integer,default=0); fixed_time:Mapped[str|None]=mapped_column(String(5)); next_day:Mapped[bool]=mapped_column(Boolean,default=False); template:Mapped[str]=mapped_column(String(100)); prompt_template:Mapped[str]=mapped_column(String(160),default="default-image-story"); text_variant:Mapped[str|None]=mapped_column(String(100)); instagram_page_id:Mapped[str|None]=mapped_column(ForeignKey("instagram_pages.id")); priority:Mapped[int]=mapped_column(Integer,default=0); sort_order:Mapped[int]=mapped_column(Integer,default=0); reuse_media:Mapped[bool]=mapped_column(Boolean,default=True)
class Post(Base,Timestamped):
    __tablename__="posts"; __table_args__=(UniqueConstraint("game_id","post_type","active_key"),UniqueConstraint("manual_submission_id",name="uq_posts_manual_submission_id"),); id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); game_id:Mapped[str|None]=mapped_column(ForeignKey("games.id"),nullable=True); team_id:Mapped[str]=mapped_column(ForeignKey("teams.id")); instagram_page_id:Mapped[str]=mapped_column(ForeignKey("instagram_pages.id")); post_type:Mapped[str]=mapped_column(String(30)); active_key:Mapped[str]=mapped_column(String(20),default="active"); manual_submission_id:Mapped[str|None]=mapped_column(String(120)); status:Mapped[PostStatus]=mapped_column(Enum(PostStatus),default=PostStatus.DETECTED); text:Mapped[str|None]=mapped_column(Text); text_version:Mapped[int]=mapped_column(Integer,default=1); feed_path:Mapped[str|None]=mapped_column(String(800)); feed_version:Mapped[int]=mapped_column(Integer,default=1); media_asset_id:Mapped[str|None]=mapped_column(ForeignKey("media_assets.id")); design_snapshot:Mapped[dict]=mapped_column(JSON,default=dict); critical_warnings:Mapped[list]=mapped_column(JSON,default=list); publishing_enabled:Mapped[bool]=mapped_column(Boolean,default=True); approved_version:Mapped[int|None]=mapped_column(Integer); approved_by:Mapped[str|None]=mapped_column(ForeignKey("users.id")); approved_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); last_edited_by:Mapped[str|None]=mapped_column(ForeignKey("users.id"))
class PublicationJob(Base,Timestamped):
    __tablename__="publication_jobs"; id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); post_id:Mapped[str]=mapped_column(ForeignKey("posts.id")); game_id:Mapped[str|None]=mapped_column(ForeignKey("games.id"),nullable=True); team_id:Mapped[str]=mapped_column(ForeignKey("teams.id")); instagram_page_id:Mapped[str]=mapped_column(ForeignKey("instagram_pages.id")); story_rule_id:Mapped[str|None]=mapped_column(ForeignKey("story_rules.id")); kind:Mapped[str]=mapped_column(String(10)); media_path:Mapped[str]=mapped_column(String(800)); text_snapshot:Mapped[str|None]=mapped_column(Text); scheduled_at:Mapped[datetime]=mapped_column(DateTime(timezone=True)); next_attempt_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),index=True); absolute_time:Mapped[bool]=mapped_column(Boolean,default=False); stale_time:Mapped[bool]=mapped_column(Boolean,default=False); approval_status:Mapped[str]=mapped_column(String(30),default="unapproved"); status:Mapped[JobStatus]=mapped_column(Enum(JobStatus),default=JobStatus.UNAPPROVED); attempts:Mapped[int]=mapped_column(Integer,default=0); last_attempt_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); published_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); platform_id:Mapped[str|None]=mapped_column(String(200)); permalink:Mapped[str|None]=mapped_column(String(1000)); error:Mapped[str|None]=mapped_column(Text); idempotency_key:Mapped[str]=mapped_column(String(100),unique=True); locked_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); approved_post_version:Mapped[int|None]=mapped_column(Integer)
class GenerationJob(Base,Timestamped):
    __tablename__="generation_jobs"
    __table_args__=(UniqueConstraint("active_key"),UniqueConstraint("idempotency_key"),)
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid)
    job_type:Mapped[GenerationJobType]=mapped_column(Enum(GenerationJobType),index=True)
    game_id:Mapped[str]=mapped_column(ForeignKey("games.id"),index=True)
    team_id:Mapped[str]=mapped_column(ForeignKey("teams.id"),index=True)
    post_id:Mapped[str|None]=mapped_column(ForeignKey("posts.id"),index=True)
    result_post_id:Mapped[str|None]=mapped_column(ForeignKey("posts.id"))
    post_type:Mapped[str]=mapped_column(String(30))
    requested_by:Mapped[str]=mapped_column(ForeignKey("users.id"))
    status:Mapped[GenerationJobStatus]=mapped_column(Enum(GenerationJobStatus),default=GenerationJobStatus.QUEUED,index=True)
    phase:Mapped[str]=mapped_column(String(40),default="preparing")
    progress:Mapped[int]=mapped_column(Integer,default=0)
    planned_outputs:Mapped[int]=mapped_column(Integer,default=0)
    completed_outputs:Mapped[int]=mapped_column(Integer,default=0)
    attempts:Mapped[int]=mapped_column(Integer,default=0)
    max_attempts:Mapped[int]=mapped_column(Integer,default=3)
    available_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,index=True)
    locked_by:Mapped[str|None]=mapped_column(String(160))
    locked_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    lease_expires_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),index=True)
    cancel_requested:Mapped[bool]=mapped_column(Boolean,default=False)
    error_category:Mapped[str|None]=mapped_column(String(80))
    error_message:Mapped[str|None]=mapped_column(Text)
    idempotency_key:Mapped[str]=mapped_column(String(255))
    active_key:Mapped[str|None]=mapped_column(String(255))
    parameters:Mapped[dict]=mapped_column(JSON,default=dict)
    started_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    completed_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True))


class InstagramConnection(Base, Timestamped):
    __tablename__ = "instagram_connections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
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


class InstagramOAuthState(Base):
    __tablename__ = "instagram_oauth_states"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
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
    __table_args__ = (UniqueConstraint("active_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    publication_job_id: Mapped[str] = mapped_column(
        ForeignKey("publication_jobs.id", ondelete="CASCADE"), index=True
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
    __table_args__ = (UniqueConstraint("active_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
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
    next_action_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
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


class MetaPublishConfirmation(Base):
    __tablename__ = "meta_publish_confirmations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
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
    __tablename__="audit_logs"; id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,index=True); user_id:Mapped[str|None]=mapped_column(ForeignKey("users.id")); team_id:Mapped[str|None]=mapped_column(ForeignKey("teams.id")); action:Mapped[str]=mapped_column(String(100)); entity_type:Mapped[str]=mapped_column(String(80)); entity_id:Mapped[str|None]=mapped_column(String(36)); details:Mapped[dict]=mapped_column(JSON,default=dict); ip:Mapped[str|None]=mapped_column(String(80))
class SystemSetting(Base):
    __tablename__="system_settings"; key:Mapped[str]=mapped_column(String(100),primary_key=True); value:Mapped[dict]=mapped_column(JSON,default=dict); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,onupdate=now)
class Notification(Base):
    __tablename__="notifications"; id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); team_id:Mapped[str|None]=mapped_column(ForeignKey("teams.id")); kind:Mapped[str]=mapped_column(String(80)); message:Mapped[str]=mapped_column(Text); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); read:Mapped[bool]=mapped_column(Boolean,default=False)

class FontAsset(Base, Timestamped):
    __tablename__ = "font_assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    family: Mapped[str] = mapped_column(String(160))
    relative_path: Mapped[str] = mapped_column(String(800), unique=True)
    mime_type: Mapped[str] = mapped_column(String(80))
    size: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DesignTemplate(Base, Timestamped):
    __tablename__ = "design_templates"
    __table_args__ = (UniqueConstraint("name", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
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
        UniqueConstraint(
            "name", "prompt_kind", "post_type", "media_kind", "version"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(160))
    prompt_kind: Mapped[str] = mapped_column(String(20))
    post_type: Mapped[str] = mapped_column(String(30))
    media_kind: Mapped[str] = mapped_column(String(10), default="none")
    prompt_body: Mapped[str] = mapped_column(Text)
    style_direction: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(100))
    quality: Mapped[str] = mapped_column(String(20), default="medium")
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderSnapshot(Base):
    __tablename__ = "provider_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"))
    source_url: Mapped[str] = mapped_column(String(1000))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    status_code: Mapped[int] = mapped_column(Integer)
    checksum: Mapped[str] = mapped_column(String(64))
    relative_path: Mapped[str] = mapped_column(String(800), unique=True)
    parser_result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class FussballSyncState(Base):
    __tablename__ = "fussball_sync_states"
    team_id: Mapped[str] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(30), default="idle", index=True)
    next_poll_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, index=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(160))
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now
    )
