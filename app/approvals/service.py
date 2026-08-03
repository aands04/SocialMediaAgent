from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.service import allowed
from app.models import (
    AuditLog,
    InstagramPage,
    JobStatus,
    Post,
    PostStatus,
    PublicationJob,
    Team,
    User,
)


class ApprovalError(ValueError):
    pass

def approve(db:Session,post:Post,user:User,selected_jobs:list[str]|None=None)->Post:
    if not allowed(db,user,"approve",post.team_id): raise ApprovalError("Keine Freigabeberechtigung")
    page=db.get(InstagramPage,post.instagram_page_id); team=db.get(Team,post.team_id)
    jobs=list(db.scalars(select(PublicationJob).where(PublicationJob.post_id==post.id)))
    selected=[j for j in jobs if selected_jobs is None or j.id in selected_jobs]; problems=[]
    if not post.text: problems.append("Text fehlt")
    if any(j.kind=="feed" for j in selected) and not post.feed_path: problems.append("Feed fehlt")
    if not selected or any(not Path(j.media_path).is_file() for j in selected): problems.append("Veröffentlichungsdateien fehlen")
    if post.critical_warnings: problems.extend(post.critical_warnings)
    logos=(post.design_snapshot or {}).get("logos")
    team_logo=(logos or {}).get("team")
    if (
        (post.design_snapshot or {}).get("source") != "manual_upload"
        and (
            not team_logo
            or not team_logo.get("verified")
            or not team_logo.get("id")
            or not team_logo.get("checksum")
        )
    ):
        problems.append("Kein eingefrorenes verifiziertes Mannschaftslogo vorhanden")
    if not page or not page.active or page.connection_status!="connected": problems.append("Instagram-Seite nicht aktiv verbunden")
    now=datetime.now(timezone.utc); late=[j for j in selected if (j.scheduled_at.replace(tzinfo=timezone.utc) if j.scheduled_at.tzinfo is None else j.scheduled_at)<now]; behavior=team.rules.get("late_approval","publish_now")
    if late and behavior=="manual": problems.append("Veröffentlichungszeitpunkt verstrichen; manuelle Entscheidung erforderlich")
    if problems: raise ApprovalError("; ".join(problems))
    post.status=PostStatus.APPROVED; post.approved_by=user.id; post.approved_at=now; post.approved_version=post.version
    future_stories=sorted((j for j in selected if j.kind=="story" and (j.scheduled_at.replace(tzinfo=timezone.utc) if j.scheduled_at.tzinfo is None else j.scheduled_at)>=now),key=lambda j:j.scheduled_at)
    for job in selected:
        job.approval_status="approved"; job.approved_post_version=post.version
        if (job.scheduled_at.replace(tzinfo=timezone.utc) if job.scheduled_at.tzinfo is None else job.scheduled_at)<now and behavior=="skip": job.status=JobStatus.SKIPPED
        elif (job.scheduled_at.replace(tzinfo=timezone.utc) if job.scheduled_at.tzinfo is None else job.scheduled_at)<now and behavior=="next_story": job.status=JobStatus.SKIPPED
        else:
            if (job.scheduled_at.replace(tzinfo=timezone.utc) if job.scheduled_at.tzinfo is None else job.scheduled_at)<now and behavior=="publish_now": job.scheduled_at=now
            job.status=JobStatus.SCHEDULED
    if late and behavior=="next_story" and future_stories: future_stories[0].status=JobStatus.SCHEDULED
    db.add(AuditLog(user_id=user.id,team_id=post.team_id,action="post.approved",entity_type="post",entity_id=post.id,details={"version":post.version,"jobs":[j.id for j in selected],"late_behavior":behavior})); db.commit(); return post

def edit_text(db:Session,post:Post,user:User,text:str,expected_version:int):
    if post.version!=expected_version: raise ApprovalError("Bearbeitungskonflikt: Beitrag wurde zwischenzeitlich geändert")
    post.text=text.strip(); post.text_version+=1; post.version+=1; post.last_edited_by=user.id
    if post.status in {PostStatus.APPROVED,PostStatus.SCHEDULED}: post.status=PostStatus.REAPPROVAL
    for job in db.scalars(select(PublicationJob).where(PublicationJob.post_id==post.id,PublicationJob.status!=JobStatus.PUBLISHED)):
        job.status=JobStatus.UNAPPROVED; job.approval_status="reapproval_required"
        if job.kind=="feed": job.text_snapshot=post.text
    db.commit()
