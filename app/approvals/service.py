from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.service import allowed
from app.meta.user_tags import UserTagValidationError, user_tags_from_snapshot
from app.models import (
    AuditLog,
    InstagramPage,
    JobStatus,
    Post,
    PostStatus,
    PublicationJob,
    PublicationMediaItem,
    Team,
    User,
)
from app.posts.club_carousel import (
    matchday_bundle_jobs,
    reconcile_matchday_bundle_feed_jobs,
)
from app.posts.service import PARTIAL_GENERATION_WARNING
from app.textgen.service import caption_contains_internal_rules


class ApprovalError(ValueError):
    pass


def _clear_resolved_reapproval_error(job: PublicationJob) -> None:
    """Remove only warnings whose sole purpose was to require a new approval."""
    if job.error and "erneute freigabe erforderlich" in job.error.casefold():
        job.error = None


def approve(
    db: Session,
    post: Post,
    user: User,
    selected_jobs: list[str] | None = None,
    selected_channel_connections: list[str] | None = None,
    *,
    commit: bool = True,
) -> Post:
    if not allowed(db, user, "approve", post.team_id):
        raise ApprovalError("Keine Freigabeberechtigung")
    page = db.get(InstagramPage, post.instagram_page_id)
    team = db.get(Team, post.team_id)
    jobs = list(db.scalars(select(PublicationJob).where(PublicationJob.post_id == post.id)))
    selected = [j for j in jobs if selected_jobs is None or j.id in selected_jobs]
    from app.posts.media_versions import freeze_publication_versions

    # Resolve auto-latest/manual selections before validating and freezing the
    # approval. The legacy path columns are updated in the same transaction.
    freeze_publication_versions(db, post, selected)
    problems = []
    if post.status == PostStatus.CREATING or PARTIAL_GENERATION_WARNING in (
        post.critical_warnings or []
    ):
        problems.append("Der Beitrag ist noch nicht vollständig erzeugt")
    if caption_contains_internal_rules(post.text):
        problems.append(
            "Der Begleittext enthält interne Generierungsregeln und muss neu erzeugt werden"
        )
    if any(job.approval_status == "manual_schedule_required" for job in selected):
        problems.append("Mindestens eine Veröffentlichung benötigt noch einen manuellen Zeitpunkt")
    if any(job.approval_status == "bundle_wait" for job in selected):
        problems.append(
            "Der gemeinsame Vereins-Feed wartet noch auf weitere Spiele oder Ergebnisse"
        )
    if not post.text:
        problems.append("Text fehlt")
    if any(j.kind in {"feed", "carousel"} for j in selected) and not post.feed_path:
        problems.append("Feed fehlt")
    if not selected or any(not Path(j.media_path).is_file() for j in selected):
        problems.append("Veröffentlichungsdateien fehlen")
    for job in selected:
        if job.kind != "carousel":
            continue
        media = list(
            db.scalars(
                select(PublicationMediaItem)
                .where(PublicationMediaItem.publication_job_id == job.id)
                .order_by(PublicationMediaItem.position)
            )
        )
        if not 2 <= len(media) <= 10:
            problems.append("Karussell benötigt 2 bis 10 Bilder")
        elif [item.position for item in media] != list(range(1, len(media) + 1)):
            problems.append("Karussell-Reihenfolge ist nicht lückenlos")
        elif any(not Path(item.media_path).is_file() for item in media):
            problems.append("Karussellbilder fehlen")
    if (post.design_snapshot or {}).get("source") == "manual_upload":
        for job in selected:
            positions = (
                range(
                    1,
                    len(
                        list(
                            db.scalars(
                                select(PublicationMediaItem).where(
                                    PublicationMediaItem.publication_job_id == job.id
                                )
                            )
                        )
                    )
                    + 1,
                )
                if job.kind == "carousel"
                else range(1, 2)
            )
            try:
                tags = [
                    user_tags_from_snapshot(post.design_snapshot, position)
                    for position in positions
                ]
            except UserTagValidationError as exc:
                problems.append(f"Instagram-Markierungen sind ungültig: {exc}")
                continue
            if job.kind == "story" and any(tags):
                problems.append(
                    "Positionsbezogene Instagram-Markierungen werden für Storys nicht unterstützt"
                )
    if post.critical_warnings:
        problems.extend(post.critical_warnings)
    logos = (post.design_snapshot or {}).get("logos")
    team_logo = (logos or {}).get("team")
    if (post.design_snapshot or {}).get("source") != "manual_upload" and (
        not team_logo
        or not team_logo.get("verified")
        or not team_logo.get("id")
        or not team_logo.get("checksum")
    ):
        problems.append("Kein eingefrorenes verifiziertes Mannschaftslogo vorhanden")
    if not page or not page.active or page.connection_status != "connected":
        problems.append("Instagram-Seite nicht aktiv verbunden")
    now = datetime.now(timezone.utc)
    late = [
        j
        for j in selected
        if (
            j.scheduled_at.replace(tzinfo=timezone.utc)
            if j.scheduled_at.tzinfo is None
            else j.scheduled_at
        )
        < now
    ]
    behavior = team.rules.get("late_approval", "publish_now")
    if (
        post.post_type == "result"
        and team.rules.get("result_timing_mode", "result_detected") == "result_detected"
    ):
        behavior = "publish_now"
    if late and behavior == "manual":
        problems.append(
            "Veröffentlichungszeitpunkt verstrichen; manuelle Entscheidung erforderlich"
        )
    if problems:
        raise ApprovalError("; ".join(problems))
    post.status = PostStatus.APPROVED
    post.approved_by = user.id
    post.approved_at = now
    post.approved_version = post.version
    future_stories = sorted(
        (
            j
            for j in selected
            if j.kind == "story"
            and (
                j.scheduled_at.replace(tzinfo=timezone.utc)
                if j.scheduled_at.tzinfo is None
                else j.scheduled_at
            )
            >= now
        ),
        key=lambda j: j.scheduled_at,
    )
    for job in selected:
        job.approval_status = "approved"
        job.approved_post_version = post.version
        _clear_resolved_reapproval_error(job)
        if (
            job.scheduled_at.replace(tzinfo=timezone.utc)
            if job.scheduled_at.tzinfo is None
            else job.scheduled_at
        ) < now and behavior == "skip":
            job.status = JobStatus.SKIPPED
        elif (
            job.scheduled_at.replace(tzinfo=timezone.utc)
            if job.scheduled_at.tzinfo is None
            else job.scheduled_at
        ) < now and behavior == "next_story":
            job.status = JobStatus.SKIPPED
        else:
            if (
                job.scheduled_at.replace(tzinfo=timezone.utc)
                if job.scheduled_at.tzinfo is None
                else job.scheduled_at
            ) < now and behavior == "publish_now":
                job.scheduled_at = now
            job.status = JobStatus.SCHEDULED
    if late and behavior == "next_story" and future_stories:
        future_stories[0].status = JobStatus.SCHEDULED
    from app.channels.jobs import ensure_approved_channel_jobs

    channel_jobs = ensure_approved_channel_jobs(
        db,
        post,
        selected,
        (
            set(selected_channel_connections)
            if selected_channel_connections is not None
            else None
        ),
    )
    db.add(
        AuditLog(
            user_id=user.id,
            team_id=post.team_id,
            action="post.approved",
            entity_type="post",
            entity_id=post.id,
            details={
                "version": post.version,
                "jobs": [j.id for j in selected],
                "late_behavior": behavior,
                "channel_jobs": [job.id for job in channel_jobs],
                "selected_channel_connections": selected_channel_connections,
            },
        )
    )
    from app.creative.hooks import record_post_decision

    record_post_decision(
        db,
        post=post,
        actor_user_id=user.id,
        action="approved",
    )
    if commit:
        db.commit()
    return post


def approve_matchday_bundle(
    db: Session,
    post: Post,
    user: User,
    selected_jobs: list[str] | None = None,
    selected_channel_connections: list[str] | None = None,
) -> Post:
    """Approve the aggregate carousel and all selected per-game stories atomically."""
    try:
        primary, members, visible_jobs, _job_posts = matchday_bundle_jobs(db, post)
        if len(members) == 1 or post.id != primary.id:
            return approve(
                db,
                post,
                user,
                selected_jobs,
                selected_channel_connections,
            )
        reconcile_matchday_bundle_feed_jobs(db, primary)
        primary, members, visible_jobs, _job_posts = matchday_bundle_jobs(db, primary)
        visible_ids = {job.id for job in visible_jobs}
        selected_ids = set(selected_jobs) if selected_jobs is not None else visible_ids
        if not selected_ids or not selected_ids.issubset(visible_ids):
            raise ApprovalError("Ungültige Auswahl für den gemeinsamen Spieltagsbeitrag")
        for member in members:
            member_job_ids = [
                job.id
                for job in visible_jobs
                if job.post_id == member.id and job.id in selected_ids
            ]
            if member_job_ids:
                approve(
                    db,
                    member,
                    user,
                    member_job_ids,
                    selected_channel_connections,
                    commit=False,
                )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return primary


def edit_text(db: Session, post: Post, user: User, text: str, expected_version: int):
    if post.version != expected_version:
        raise ApprovalError("Bearbeitungskonflikt: Beitrag wurde zwischenzeitlich geändert")
    from app.models import PostTextVersion

    old_text = post.text or ""
    previous = (
        db.get(PostTextVersion, post.selected_text_version_id)
        if post.selected_text_version_id
        else None
    )
    post.text = text.strip()
    post.text_version += 1
    post.version += 1
    post.last_edited_by = user.id
    if post.status in {PostStatus.APPROVED, PostStatus.SCHEDULED}:
        post.status = PostStatus.REAPPROVAL
    for job in db.scalars(
        select(PublicationJob).where(
            PublicationJob.post_id == post.id, PublicationJob.status != JobStatus.PUBLISHED
        )
    ):
        job.status = JobStatus.UNAPPROVED
        job.approval_status = "reapproval_required"
        if job.kind in {"feed", "carousel"}:
            job.text_snapshot = post.text
    from app.posts.media_versions import ensure_text_version

    ensure_text_version(db, post, created_by=user.id, source="manual_edit")
    from app.creative.hooks import record_material_text_edit

    record_material_text_edit(
        db,
        post=post,
        actor_user_id=user.id,
        previous=previous,
        old_text=old_text,
        new_text=post.text,
    )
    db.commit()
