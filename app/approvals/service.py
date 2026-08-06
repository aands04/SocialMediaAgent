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
from app.posts.club_carousel import matchday_bundle_jobs


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
    *,
    commit: bool = True,
) -> Post:
    if not allowed(db, user, "approve", post.team_id):
        raise ApprovalError("Keine Freigabeberechtigung")
    page = db.get(InstagramPage, post.instagram_page_id)
    team = db.get(Team, post.team_id)
    jobs = list(db.scalars(select(PublicationJob).where(PublicationJob.post_id == post.id)))
    selected = [j for j in jobs if selected_jobs is None or j.id in selected_jobs]
    problems = []
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
            },
        )
    )
    if commit:
        db.commit()
    return post


def approve_matchday_bundle(
    db: Session,
    post: Post,
    user: User,
    selected_jobs: list[str] | None = None,
) -> Post:
    """Approve the aggregate carousel and all selected per-game stories atomically."""
    primary, members, visible_jobs, _job_posts = matchday_bundle_jobs(db, post)
    if len(members) == 1 or post.id != primary.id:
        return approve(db, post, user, selected_jobs)

    visible_ids = {job.id for job in visible_jobs}
    selected_ids = set(selected_jobs) if selected_jobs is not None else visible_ids
    if not selected_ids or not selected_ids.issubset(visible_ids):
        raise ApprovalError("Ungültige Auswahl für den gemeinsamen Spieltagsbeitrag")

    try:
        for member in members:
            member_job_ids = [
                job.id
                for job in visible_jobs
                if job.post_id == member.id and job.id in selected_ids
            ]
            if member_job_ids:
                approve(db, member, user, member_job_ids, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return primary


def edit_text(db: Session, post: Post, user: User, text: str, expected_version: int):
    if post.version != expected_version:
        raise ApprovalError("Bearbeitungskonflikt: Beitrag wurde zwischenzeitlich geändert")
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
    db.commit()
