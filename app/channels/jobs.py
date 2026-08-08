from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    JobStatus,
    Post,
    PostChannelContent,
    PublicationJob,
    PublicationMediaItem,
    SocialChannelConnection,
    TeamChannelAssignment,
)


def channel_text(
    db: Session,
    post: Post,
    connection: SocialChannelConnection,
) -> str:
    variant = db.scalar(
        select(PostChannelContent).where(
            PostChannelContent.post_id == post.id,
            PostChannelContent.channel_connection_id == connection.id,
        )
    )
    return variant.text if variant else (post.text or "")


def ensure_approved_channel_jobs(
    db: Session,
    post: Post,
    approved_jobs: list[PublicationJob],
    selected_connection_ids: set[str] | None = None,
) -> list[PublicationJob]:
    """Legt nur ausdrücklich zugewiesene, zukünftige Kanalaufträge an.

    Die Funktion wird innerhalb derselben Freigabetransaktion ausgeführt. Eine
    später neu angelegte Kanalverbindung aktiviert daher keine alten Beiträge.
    """
    assignments = list(
        db.scalars(
            select(TeamChannelAssignment)
            .join(
                SocialChannelConnection,
                SocialChannelConnection.id == TeamChannelAssignment.channel_connection_id,
            )
            .where(
                TeamChannelAssignment.team_id == post.team_id,
                TeamChannelAssignment.enabled.is_(True),
                SocialChannelConnection.active.is_(True),
                SocialChannelConnection.status == "connected",
            )
        )
    )
    eligible_ids = {
        assignment.channel_connection_id
        for assignment in assignments
        if selected_connection_ids is None
        or assignment.channel_connection_id in selected_connection_ids
    }
    for existing_job in db.scalars(
        select(PublicationJob).where(
            PublicationJob.post_id == post.id,
            PublicationJob.channel_type.in_({"facebook", "whatsapp"}),
            PublicationJob.status != JobStatus.PUBLISHED,
        )
    ):
        if existing_job.channel_connection_id not in eligible_ids:
            existing_job.status = JobStatus.CANCELLED
            existing_job.approval_status = "rejected"
            existing_job.error = "Zielkanal wurde bei der Freigabe abgewählt"
    created = []
    for assignment in assignments:
        connection = db.get(SocialChannelConnection, assignment.channel_connection_id)
        if connection is None or connection.channel_type == "instagram":
            continue
        if selected_connection_ids is not None and connection.id not in selected_connection_ids:
            continue
        enabled_for_type = (
            assignment.result_enabled
            if post.post_type == "result"
            else assignment.announcement_enabled
        )
        if not enabled_for_type:
            continue
        source_jobs = [job for job in approved_jobs if job.kind in {"feed", "carousel"}]
        if not source_jobs:
            continue
        source = sorted(source_jobs, key=lambda item: item.scheduled_at)[0]
        target_text = channel_text(db, post, connection)
        key = f"{source.id}:channel:{connection.channel_type}:{connection.id}:v1"
        existing = db.scalar(select(PublicationJob).where(PublicationJob.idempotency_key == key))
        if existing:
            if existing.status != JobStatus.PUBLISHED:
                existing.status = JobStatus.SCHEDULED
                existing.approval_status = "approved"
                existing.error = None
                existing.text_snapshot = target_text
                existing.approved_post_version = post.version
                existing.scheduled_at = source.scheduled_at
                created.append(existing)
            continue
        target = PublicationJob(
            post_id=post.id,
            game_id=post.game_id,
            team_id=post.team_id,
            instagram_page_id=None,
            channel_type=connection.channel_type,
            channel_connection_id=connection.id,
            content_type=post.post_type,
            target="audience"
            if connection.channel_type == "whatsapp"
            else connection.external_account_id,
            delivery_action="send" if connection.channel_type == "whatsapp" else "publish",
            kind="message" if connection.channel_type == "whatsapp" else source.kind,
            media_path=source.media_path,
            text_snapshot=target_text,
            text_version_id=source.text_version_id,
            media_version_id=source.media_version_id,
            publication_rule_slot_id=source.publication_rule_slot_id,
            schedule_source=f"channel:{source.schedule_source}"[:30],
            scheduled_at=source.scheduled_at,
            next_attempt_at=None,
            absolute_time=source.absolute_time,
            stale_time=source.stale_time,
            approval_status="approved",
            status=JobStatus.SCHEDULED,
            idempotency_key=key,
            approved_post_version=post.version,
        )
        db.add(target)
        db.flush()
        if source.kind == "carousel" and connection.channel_type == "facebook":
            for item in db.scalars(
                select(PublicationMediaItem)
                .where(PublicationMediaItem.publication_job_id == source.id)
                .order_by(PublicationMediaItem.position)
            ):
                db.add(
                    PublicationMediaItem(
                        publication_job_id=target.id,
                        position=item.position,
                        media_version_id=item.media_version_id,
                        media_path=item.media_path,
                        checksum=item.checksum,
                        mime_type=item.mime_type,
                        file_size=item.file_size,
                        width=item.width,
                        height=item.height,
                    )
                )
        created.append(target)
    return created
