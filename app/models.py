import enum
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def now(): return datetime.now(timezone.utc)
def uid(): return str(uuid4())
class Role(str,enum.Enum): ADMIN="admin"; EDITOR="editor"; APPROVER="approver"; VIEWER="viewer"
class PostStatus(str,enum.Enum): DETECTED="detected"; PLANNED="planned"; CREATING="creating"; INCOMPLETE="incomplete"; PENDING="pending_approval"; REJECTED="rejected"; APPROVED="approved"; REAPPROVAL="reapproval_required"; SCHEDULED="scheduled"; PARTIAL="partially_published"; PUBLISHED="published"; ERROR="publishing_error"; CANCELLED="cancelled"
class JobStatus(str,enum.Enum): DRAFT="draft"; UNAPPROVED="unapproved"; APPROVED="approved"; SCHEDULED="scheduled"; WAITING="waiting"; PUBLISHING="publishing"; PUBLISHED="published"; RETRY="retry_scheduled"; FAILED="failed"; CANCELLED="cancelled"; SKIPPED="skipped"; UNCERTAIN="uncertain"
class Timestamped:
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,onupdate=now)
    version: Mapped[int]=mapped_column(Integer,default=1,nullable=False)
class User(Base,Timestamped):
    __tablename__="users"; id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); email:Mapped[str]=mapped_column(String(255),unique=True,index=True); password_hash:Mapped[str]=mapped_column(String(255)); role:Mapped[Role]=mapped_column(Enum(Role),default=Role.VIEWER); all_teams:Mapped[bool]=mapped_column(Boolean,default=False); active:Mapped[bool]=mapped_column(Boolean,default=True); archived_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); failed_logins:Mapped[int]=mapped_column(Integer,default=0); locked_until:Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
class UserTeam(Base):
    __tablename__="user_teams"; user_id:Mapped[str]=mapped_column(ForeignKey("users.id",ondelete="CASCADE"),primary_key=True); team_id:Mapped[str]=mapped_column(ForeignKey("teams.id",ondelete="CASCADE"),primary_key=True)
class InstagramPage(Base,Timestamped):
    __tablename__="instagram_pages"; id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); internal_name:Mapped[str]=mapped_column(String(120)); display_name:Mapped[str]=mapped_column(String(120)); username:Mapped[str]=mapped_column(String(80)); profile_url:Mapped[str|None]=mapped_column(String(500)); account_id:Mapped[str|None]=mapped_column(String(100)); facebook_page_id:Mapped[str|None]=mapped_column(String(100)); club:Mapped[str]=mapped_column(String(160)); active:Mapped[bool]=mapped_column(Boolean,default=False); connection_status:Mapped[str]=mapped_column(String(30),default="unconfigured"); publishing_enabled:Mapped[bool]=mapped_column(Boolean,default=False); allowed_types:Mapped[dict]=mapped_column(JSON,default=lambda:{"feed":True,"story":True}); defaults:Mapped[dict]=mapped_column(JSON,default=dict); archived_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); last_check_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); last_error:Mapped[str|None]=mapped_column(Text)
class Team(Base,Timestamped):
    __tablename__="teams"; id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); internal_name:Mapped[str]=mapped_column(String(120)); display_name:Mapped[str]=mapped_column(String(120)); short_name:Mapped[str]=mapped_column(String(30)); slug:Mapped[str]=mapped_column(String(80),unique=True); club:Mapped[str]=mapped_column(String(160)); active:Mapped[bool]=mapped_column(Boolean,default=True); archived_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); fussball_url:Mapped[str]=mapped_column(String(1000)); instagram_page_id:Mapped[str]=mapped_column(ForeignKey("instagram_pages.id")); media_subdir:Mapped[str]=mapped_column(String(500)); logo_path:Mapped[str|None]=mapped_column(String(500)); feed_template:Mapped[str]=mapped_column(String(100),default="default-feed"); story_templates:Mapped[list]=mapped_column(JSON,default=lambda:["default-story"]); primary_font:Mapped[str]=mapped_column(String(100),default="sans-serif"); secondary_font:Mapped[str]=mapped_column(String(100),default="sans-serif"); colors:Mapped[dict]=mapped_column(JSON,default=lambda:{"primary":"#172554","secondary":"#ffffff"}); text_style:Mapped[dict]=mapped_column(JSON,default=dict); hashtags:Mapped[list]=mapped_column(JSON,default=list); timezone:Mapped[str]=mapped_column(String(50),default="Europe/Berlin"); rules:Mapped[dict]=mapped_column(JSON,default=dict); publishing_enabled:Mapped[bool]=mapped_column(Boolean,default=True); last_sync_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); last_error_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); last_error:Mapped[str|None]=mapped_column(Text)
class Game(Base,Timestamped):
    __tablename__="games"; __table_args__=(UniqueConstraint("team_id","provider","external_id"),); id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); team_id:Mapped[str]=mapped_column(ForeignKey("teams.id"),index=True); provider:Mapped[str]=mapped_column(String(40),default="fussball.de"); external_id:Mapped[str]=mapped_column(String(200)); home_team:Mapped[str]=mapped_column(String(160)); away_team:Mapped[str]=mapped_column(String(160)); kickoff:Mapped[datetime]=mapped_column(DateTime(timezone=True)); original_kickoff:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); competition:Mapped[str|None]=mapped_column(String(160)); venue:Mapped[str|None]=mapped_column(String(250)); pitch:Mapped[str|None]=mapped_column(String(80)); status:Mapped[str]=mapped_column(String(40),default="scheduled"); home_score:Mapped[int|None]=mapped_column(Integer); away_score:Mapped[int|None]=mapped_column(Integer); halftime:Mapped[str|None]=mapped_column(String(20)); result_confirmed:Mapped[bool]=mapped_column(Boolean,default=False); source_url:Mapped[str]=mapped_column(String(1000)); checked_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); overrides:Mapped[dict]=mapped_column(JSON,default=dict); data_hash:Mapped[str|None]=mapped_column(String(64))
class MediaAsset(Base,Timestamped):
    __tablename__="media_assets"; __table_args__=(UniqueConstraint("team_id","relative_path"),); id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); team_id:Mapped[str]=mapped_column(ForeignKey("teams.id")); relative_path:Mapped[str]=mapped_column(String(800)); filename:Mapped[str]=mapped_column(String(255)); mime_type:Mapped[str]=mapped_column(String(80)); size:Mapped[int]=mapped_column(Integer); checksum:Mapped[str]=mapped_column(String(64)); mtime:Mapped[datetime]=mapped_column(DateTime(timezone=True)); player_name:Mapped[str|None]=mapped_column(String(160)); active:Mapped[bool]=mapped_column(Boolean,default=True); available:Mapped[bool]=mapped_column(Boolean,default=True); reserved_game_id:Mapped[str|None]=mapped_column(ForeignKey("games.id"),unique=True); uses:Mapped[int]=mapped_column(Integer,default=0)
class StoryRule(Base,Timestamped):
    __tablename__="story_rules"; __table_args__=(UniqueConstraint("team_id","name"),); id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); team_id:Mapped[str]=mapped_column(ForeignKey("teams.id")); name:Mapped[str]=mapped_column(String(120)); active:Mapped[bool]=mapped_column(Boolean,default=True); post_type:Mapped[str]=mapped_column(String(30)); reference:Mapped[str]=mapped_column(String(40)); direction:Mapped[str]=mapped_column(String(10),default="before"); offset_minutes:Mapped[int]=mapped_column(Integer,default=0); fixed_time:Mapped[str|None]=mapped_column(String(5)); next_day:Mapped[bool]=mapped_column(Boolean,default=False); template:Mapped[str]=mapped_column(String(100)); prompt_template:Mapped[str]=mapped_column(String(160),default="default-image-story"); text_variant:Mapped[str|None]=mapped_column(String(100)); instagram_page_id:Mapped[str|None]=mapped_column(ForeignKey("instagram_pages.id")); priority:Mapped[int]=mapped_column(Integer,default=0); sort_order:Mapped[int]=mapped_column(Integer,default=0); reuse_media:Mapped[bool]=mapped_column(Boolean,default=True)
class Post(Base,Timestamped):
    __tablename__="posts"; __table_args__=(UniqueConstraint("game_id","post_type","active_key"),); id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); game_id:Mapped[str]=mapped_column(ForeignKey("games.id")); team_id:Mapped[str]=mapped_column(ForeignKey("teams.id")); instagram_page_id:Mapped[str]=mapped_column(ForeignKey("instagram_pages.id")); post_type:Mapped[str]=mapped_column(String(30)); active_key:Mapped[str]=mapped_column(String(20),default="active"); status:Mapped[PostStatus]=mapped_column(Enum(PostStatus),default=PostStatus.DETECTED); text:Mapped[str|None]=mapped_column(Text); text_version:Mapped[int]=mapped_column(Integer,default=1); feed_path:Mapped[str|None]=mapped_column(String(800)); feed_version:Mapped[int]=mapped_column(Integer,default=1); media_asset_id:Mapped[str|None]=mapped_column(ForeignKey("media_assets.id")); design_snapshot:Mapped[dict]=mapped_column(JSON,default=dict); critical_warnings:Mapped[list]=mapped_column(JSON,default=list); publishing_enabled:Mapped[bool]=mapped_column(Boolean,default=True); approved_version:Mapped[int|None]=mapped_column(Integer); approved_by:Mapped[str|None]=mapped_column(ForeignKey("users.id")); approved_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); last_edited_by:Mapped[str|None]=mapped_column(ForeignKey("users.id"))
class PublicationJob(Base,Timestamped):
    __tablename__="publication_jobs"; id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); post_id:Mapped[str]=mapped_column(ForeignKey("posts.id")); game_id:Mapped[str]=mapped_column(ForeignKey("games.id")); team_id:Mapped[str]=mapped_column(ForeignKey("teams.id")); instagram_page_id:Mapped[str]=mapped_column(ForeignKey("instagram_pages.id")); story_rule_id:Mapped[str|None]=mapped_column(ForeignKey("story_rules.id")); kind:Mapped[str]=mapped_column(String(10)); media_path:Mapped[str]=mapped_column(String(800)); text_snapshot:Mapped[str|None]=mapped_column(Text); scheduled_at:Mapped[datetime]=mapped_column(DateTime(timezone=True)); absolute_time:Mapped[bool]=mapped_column(Boolean,default=False); stale_time:Mapped[bool]=mapped_column(Boolean,default=False); approval_status:Mapped[str]=mapped_column(String(30),default="unapproved"); status:Mapped[JobStatus]=mapped_column(Enum(JobStatus),default=JobStatus.UNAPPROVED); attempts:Mapped[int]=mapped_column(Integer,default=0); last_attempt_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); published_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); platform_id:Mapped[str|None]=mapped_column(String(200)); permalink:Mapped[str|None]=mapped_column(String(1000)); error:Mapped[str|None]=mapped_column(Text); idempotency_key:Mapped[str]=mapped_column(String(100),unique=True); locked_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); approved_post_version:Mapped[int|None]=mapped_column(Integer)
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
