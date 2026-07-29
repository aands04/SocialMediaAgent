"""Idempotent staging E2E scenario. It can only instantiate DryRunPublisher."""
import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image
from sqlalchemy import select

from app.approvals.service import approve
from app.auth.service import hash_password
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import (
    AuditLog,
    Game,
    InstagramPage,
    JobStatus,
    MediaAsset,
    PostStatus,
    PublicationJob,
    Role,
    StoryRule,
    Team,
    User,
)
from app.posts.service import create_post
from app.publishing.service import DryRunPublisher
from app.publishing.worker import process_job
from app.rendering.service import Renderer
from app.textgen.service import FixtureTextGenerator

SMOKE_KEY="staging-smoke-v1"
def run()->dict:
    settings=get_settings()
    if settings.environment!="staging" or settings.publisher_mode!="dry-run" or settings.meta_access_token: raise RuntimeError("Smoke-Test nur in sicherem Staging-Dry-Run ohne Meta-Token")
    password_file=Path(os.environ.get("SMOKE_ADMIN_PASSWORD_FILE","/run/secrets/smoke_admin_password"))
    if not password_file.is_file() or len(password_file.read_text().strip())<16: raise RuntimeError("Smoke-Admin-Secret fehlt oder ist zu kurz")
    media_dir=settings.media_root/"staging_smoke"/"spieler"
    if not media_dir.is_dir(): raise RuntimeError(f"Read-only SMB-Testordner fehlt: {media_dir}")
    image_path=media_dir/"smoke-player.png"
    if not image_path.is_file(): raise RuntimeError(f"SMB-Testbild fehlt: {image_path}")
    with Image.open(image_path) as image: image.verify()
    with SessionLocal() as db:
        admin=db.scalar(select(User).where(User.email=="staging-smoke@example.invalid"))
        if not admin: admin=User(email="staging-smoke@example.invalid",password_hash=hash_password(password_file.read_text().strip()),role=Role.ADMIN,all_teams=True); db.add(admin); db.flush()
        page=db.scalar(select(InstagramPage).where(InstagramPage.internal_name==SMOKE_KEY))
        if not page: page=InstagramPage(internal_name=SMOKE_KEY,display_name="Staging Dry-Run",username="dry_run_only",club="Staging",account_id="dry-run-account",active=True,connection_status="connected",publishing_enabled=True); db.add(page); db.flush()
        team=db.scalar(select(Team).where(Team.slug==SMOKE_KEY))
        if not team: team=Team(internal_name=SMOKE_KEY,display_name="Staging Testmannschaft",short_name="SMK",slug=SMOKE_KEY,club="Staging",fussball_url="https://www.fussball.de/staging-fixture",instagram_page_id=page.id,media_subdir="staging_smoke/spieler",rules={"announcement_enabled":True,"feed_before_minutes":60,"late_approval":"publish_now"}); db.add(team); db.flush()
        digest=hashlib.sha256(image_path.read_bytes()).hexdigest(); asset=db.scalar(select(MediaAsset).where(MediaAsset.team_id==team.id,MediaAsset.relative_path=="staging_smoke/spieler/smoke-player.png"))
        if not asset: asset=MediaAsset(team_id=team.id,relative_path="staging_smoke/spieler/smoke-player.png",filename=image_path.name,mime_type="image/png",size=image_path.stat().st_size,checksum=digest,mtime=datetime.fromtimestamp(image_path.stat().st_mtime,timezone.utc)); db.add(asset)
        for name,minutes in (("Smoke 24h",1440),("Smoke 3h",180)):
            if not db.scalar(select(StoryRule).where(StoryRule.team_id==team.id,StoryRule.name==name)): db.add(StoryRule(team_id=team.id,name=name,post_type="announcement",reference="kickoff",direction="before",offset_minutes=minutes,template="default-story",sort_order=minutes))
        db.flush(); game=db.scalar(select(Game).where(Game.team_id==team.id,Game.provider=="fixture",Game.external_id==SMOKE_KEY))
        if not game: game=Game(team_id=team.id,provider="fixture",external_id=SMOKE_KEY,home_team="Staging Testmannschaft",away_team="Fixture FC",kickoff=datetime.now(timezone.utc)+timedelta(days=2),venue="Staging-Platz",source_url="fixture://staging-smoke"); db.add(game); db.flush()
        post=create_post(db,game,team,FixtureTextGenerator(),Renderer(settings.generated_root)); db.refresh(post)
        if post.critical_warnings: raise RuntimeError(f"Beitrag unvollständig: {post.critical_warnings}")
        jobs=list(db.scalars(select(PublicationJob).where(PublicationJob.post_id==post.id)))
        if len(jobs)!=3: raise RuntimeError(f"Erwartet: Feed + 2 Storys; erhalten: {len(jobs)}")
        for job in jobs: Renderer(settings.generated_root).validate(Path(job.media_path),job.kind)
        if not post.text: raise RuntimeError("Text fehlt")
        if post.status not in {PostStatus.APPROVED,PostStatus.PARTIAL,PostStatus.PUBLISHED}: approve(db,post,admin)
        for job in jobs:
            if job.status!=JobStatus.PUBLISHED: job.scheduled_at=datetime.now(timezone.utc)-timedelta(seconds=1)
        if not db.scalar(select(AuditLog).where(AuditLog.action=="staging.smoke",AuditLog.entity_id==post.id)): db.add(AuditLog(user_id=admin.id,team_id=team.id,action="staging.smoke",entity_type="post",entity_id=post.id,details={"key":SMOKE_KEY}))
        db.commit()
        smoke_settings=Settings(**{**settings.model_dump(),"global_publish_enabled":True,"publisher_mode":"dry-run","meta_access_token":None})
        publisher=DryRunPublisher()
        for job in jobs: process_job(db,job.id,publisher,smoke_settings)
        db.refresh(post); db.refresh(asset)
        platform_ids=[job.platform_id for job in jobs]
        if post.status!=PostStatus.PUBLISHED or not all(value and value.startswith("dry-run:") for value in platform_ids): raise RuntimeError("Dry-Run-Abschlussstatus ungültig")
        return {"post":post.id,"jobs":len(jobs),"asset_uses":asset.uses,"audit":db.scalar(select(AuditLog).where(AuditLog.action=="staging.smoke",AuditLog.entity_id==post.id)) is not None,"platform_ids":platform_ids,"status":post.status.value}
if __name__=="__main__": print(run())
