from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.service import allowed
from app.models import AuditLog, InstagramPage, JobStatus, Post, PostStatus, PublicationJob, User


class ApprovalError(ValueError): pass
def approve(db:Session,post:Post,user:User,selected_jobs:list[str]|None=None)->Post:
    if not allowed(db,user,"approve",post.team_id): raise ApprovalError("Keine Freigabeberechtigung")
    page=db.get(InstagramPage,post.instagram_page_id)
    jobs=list(db.scalars(select(PublicationJob).where(PublicationJob.post_id==post.id)))
    selected=[j for j in jobs if selected_jobs is None or j.id in selected_jobs]
    problems=[]
    if not post.text or not post.feed_path: problems.append("Text oder Feed fehlt")
    if not selected or any(not Path(j.media_path).is_file() for j in selected): problems.append("Veröffentlichungsdateien fehlen")
    if post.critical_warnings: problems.extend(post.critical_warnings)
    if not page or not page.active or page.connection_status!="connected": problems.append("Instagram-Seite nicht aktiv verbunden")
    if problems: raise ApprovalError("; ".join(problems))
    post.status=PostStatus.APPROVED; post.approved_by=user.id; post.approved_at=datetime.now(timezone.utc); post.approved_version=post.version
    for job in selected: job.approval_status="approved"; job.status=JobStatus.SCHEDULED; job.approved_post_version=post.version
    db.add(AuditLog(user_id=user.id,team_id=post.team_id,action="post.approved",entity_type="post",entity_id=post.id,details={"version":post.version,"jobs":[j.id for j in selected]})); db.commit(); return post
def edit_text(db:Session,post:Post,user:User,text:str,expected_version:int):
    if post.version!=expected_version: raise ApprovalError("Bearbeitungskonflikt: Beitrag wurde zwischenzeitlich geändert")
    post.text=text.strip(); post.text_version+=1; post.version+=1; post.last_edited_by=user.id
    if post.status in {PostStatus.APPROVED,PostStatus.SCHEDULED}: post.status=PostStatus.REAPPROVAL
    for job in db.scalars(select(PublicationJob).where(PublicationJob.post_id==post.id,PublicationJob.status!=JobStatus.PUBLISHED)): job.status=JobStatus.UNAPPROVED; job.approval_status="reapproval_required"
    db.commit()
