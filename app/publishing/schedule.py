"""Safe manual scheduling changes for persistent publication jobs."""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth.service import allowed
from app.models import (
    AuditLog,
    JobStatus,
    MetaPublishingAttempt,
    Post,
    PostStatus,
    PublicationJob,
    User,
)


class PublicationScheduleError(ValueError):
    pass


EDITABLE_JOB_STATUSES = {
    JobStatus.DRAFT,
    JobStatus.UNAPPROVED,
    JobStatus.APPROVED,
    JobStatus.SCHEDULED,
    JobStatus.WAITING,
}

APPROVAL_INVALIDATION_JOB_STATUSES = {
    JobStatus.DRAFT,
    JobStatus.UNAPPROVED,
    JobStatus.APPROVED,
    JobStatus.SCHEDULED,
}


@dataclass(frozen=True)
class PublicationScheduleChange:
    old_scheduled_at: datetime
    new_scheduled_at: datetime
    approval_invalidated: bool


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def reschedule_publication_job(
    db: Session,
    *,
    post: Post,
    job: PublicationJob,
    user: User,
    scheduled_at: datetime,
    expected_job_version: int,
) -> PublicationScheduleChange:
    """Set a future absolute time and invalidate an existing approval safely."""
    if job.post_id != post.id or job.club_id != post.club_id or job.team_id != post.team_id:
        raise PublicationScheduleError("Veröffentlichungsauftrag gehört nicht zu diesem Beitrag")
    if not allowed(db, user, "edit_post", job.team_id):
        raise PublicationScheduleError("Keine Berechtigung zum Ändern des Zeitpunkts")
    if job.version != expected_job_version:
        raise PublicationScheduleError(
            "Bearbeitungskonflikt: Der Veröffentlichungsauftrag wurde zwischenzeitlich geändert"
        )
    if job.status not in EDITABLE_JOB_STATUSES:
        raise PublicationScheduleError(
            "Dieser Veröffentlichungsauftrag kann in seinem aktuellen Status nicht neu geplant werden"
        )
    if job.published_at or job.platform_id:
        raise PublicationScheduleError("Bereits veröffentlichte Aufträge bleiben unverändert")
    if job.attempts or job.locked_at:
        raise PublicationScheduleError(
            "Für diesen Auftrag wurde die Plattformverarbeitung bereits begonnen"
        )
    active_attempt = db.scalar(
        select(MetaPublishingAttempt.id).where(
            MetaPublishingAttempt.publication_job_id == job.id,
            or_(
                MetaPublishingAttempt.active_key.is_not(None),
                MetaPublishingAttempt.phase.notin_(["completed", "failed"]),
            ),
        )
    )
    if active_attempt:
        raise PublicationScheduleError(
            "Für diesen Auftrag existiert bereits ein offener Meta-Vorgang"
        )
    processing_sibling = db.scalar(
        select(PublicationJob.id).where(
            PublicationJob.post_id == post.id,
            PublicationJob.id != job.id,
            or_(
                PublicationJob.status.in_(
                    [JobStatus.PUBLISHING, JobStatus.RETRY, JobStatus.UNCERTAIN]
                ),
                PublicationJob.locked_at.is_not(None),
            ),
        )
    )
    if processing_sibling:
        raise PublicationScheduleError(
            "Ein anderer Auftrag dieses Beitrags wird bereits von der Plattform verarbeitet"
        )
    active_sibling_attempt = db.scalar(
        select(MetaPublishingAttempt.id)
        .join(
            PublicationJob,
            PublicationJob.id == MetaPublishingAttempt.publication_job_id,
        )
        .where(
            PublicationJob.post_id == post.id,
            PublicationJob.id != job.id,
            or_(
                MetaPublishingAttempt.active_key.is_not(None),
                MetaPublishingAttempt.phase.notin_(["completed", "failed"]),
            ),
        )
    )
    if active_sibling_attempt:
        raise PublicationScheduleError(
            "Ein anderer Auftrag dieses Beitrags besitzt bereits einen offenen Meta-Vorgang"
        )

    new_time = _utc(scheduled_at)
    if new_time <= datetime.now(timezone.utc):
        raise PublicationScheduleError("Der neue Veröffentlichungszeitpunkt muss in der Zukunft liegen")
    old_time = _utc(job.scheduled_at)

    approval_invalidated = bool(
        post.approved_version is not None
        or job.approval_status == "approved"
        or job.status in {JobStatus.APPROVED, JobStatus.SCHEDULED}
    )
    job.scheduled_at = new_time
    job.absolute_time = True
    job.schedule_source = "manual"
    job.stale_time = False
    job.next_attempt_at = None
    job.version += 1
    if job.approval_status == "manual_schedule_required":
        job.approval_status = "unapproved"
        job.status = JobStatus.UNAPPROVED
        job.error = None

    if approval_invalidated:
        post.version += 1
        post.approved_version = None
        post.approved_by = None
        post.approved_at = None
        post.status = PostStatus.REAPPROVAL
        for open_job in db.scalars(
            select(PublicationJob).where(
                PublicationJob.post_id == post.id,
                PublicationJob.status.in_(APPROVAL_INVALIDATION_JOB_STATUSES),
            )
        ):
            open_job.approval_status = "reapproval_required"
            open_job.approved_post_version = None
            if open_job.status in {
                JobStatus.DRAFT,
                JobStatus.APPROVED,
                JobStatus.SCHEDULED,
            }:
                open_job.status = JobStatus.UNAPPROVED
            open_job.error = "Veröffentlichungszeitpunkt geändert; erneute Freigabe erforderlich"

    db.add(
        AuditLog(
            user_id=user.id,
            team_id=job.team_id,
            action="publication.schedule_changed",
            entity_type="publication_job",
            entity_id=job.id,
            details={
                "post_id": post.id,
                "kind": job.kind,
                "old_scheduled_at": old_time.isoformat(),
                "new_scheduled_at": new_time.isoformat(),
                "absolute_time": True,
                "approval_invalidated": approval_invalidated,
            },
        )
    )
    db.commit()
    return PublicationScheduleChange(old_time, new_time, approval_invalidated)
