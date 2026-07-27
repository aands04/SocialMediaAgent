from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    InstagramPage,
    JobStatus,
    Post,
    PostStatus,
    PublicationJob,
    SystemSetting,
    Team,
)
from app.publishing.service import PublishError, SocialMediaPublisher


def process_job(db:Session,job_id:str,publisher:SocialMediaPublisher,settings:Settings):
    job=db.scalar(select(PublicationJob).where(PublicationJob.id==job_id).with_for_update())
    if not job or job.status==JobStatus.PUBLISHED:return job
    post=db.get(Post,job.post_id); team=db.get(Team,job.team_id); page=db.get(InstagramPage,job.instagram_page_id); stop=db.get(SystemSetting,"emergency_stop")
    checks=[(settings.global_publish_enabled,"Globales Publishing ist nicht aktiviert"),(not(stop and stop.value.get("enabled")),"Not-Aus aktiv"),(post.status in {PostStatus.APPROVED,PostStatus.SCHEDULED},"Beitrag nicht freigegeben"),(post.version==job.approved_post_version,"Freigegebene Version verändert"),(post.publishing_enabled and team.publishing_enabled and page.publishing_enabled,"Publishing deaktiviert"),(page.active and page.connection_status=="connected","Instagram-Seite gestört"),(Path(job.media_path).is_file(),"Mediendatei fehlt"),(job.scheduled_at<=datetime.now(timezone.utc),"Zeitpunkt nicht erreicht")]
    for ok,message in checks:
        if not ok:
            if "Version" in message: post.status=PostStatus.REAPPROVAL; job.status=JobStatus.UNAPPROVED
            db.commit(); raise PublishError(message)
    job.status=JobStatus.PUBLISHING; job.attempts+=1; job.last_attempt_at=datetime.now(timezone.utc); db.commit()
    try: result=publisher.publish(account_id=page.account_id,kind=job.kind,media_url=job.media_path,caption=job.text_snapshot,idempotency_key=job.idempotency_key)
    except PublishError as e:
        job.error=str(e); job.status=JobStatus.RETRY if e.retryable and job.attempts<settings.max_publish_attempts else (JobStatus.UNCERTAIN if "unklar" in str(e) else JobStatus.FAILED); db.commit(); raise
    if not result.confirmed:
        job.status=JobStatus.UNCERTAIN if result.uncertain else JobStatus.FAILED; job.platform_id=result.platform_id; db.commit(); return job
    job.status=JobStatus.PUBLISHED; job.platform_id=result.platform_id; job.permalink=result.permalink; job.published_at=datetime.now(timezone.utc); db.commit(); return job
