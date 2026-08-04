from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Game,
    InstagramPage,
    JobStatus,
    Post,
    PostStatus,
    PublicationJob,
    PublicationMediaItem,
    SystemSetting,
    Team,
)
from app.publishing.service import PublishError, SocialMediaPublisher


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _grouped_games_are_publishable(db: Session, post: Post) -> tuple[bool, str]:
    bundle = (post.design_snapshot or {}).get("club_matchday_carousel") or {}
    game_ids = bundle.get("game_ids") or []
    if not game_ids:
        return True, ""
    games = {game.id: game for game in db.scalars(select(Game).where(Game.id.in_(game_ids)))}
    if len(games) != len(set(game_ids)):
        return False, "Mindestens ein Spiel des Vereins-Karussells fehlt"
    for game_id in game_ids:
        game = games[game_id]
        if game.status in {"cancelled", "postponed", "provisional"}:
            return False, "Ein Spielstatus sperrt das Vereins-Karussell"
        if (game.overrides or {}).get("automation_blocked"):
            return False, "Ein Spiel des Vereins-Karussells ist gesperrt"
    return True, ""


def _reject_before_publish(
    db: Session,
    *,
    post: Post,
    job: PublicationJob,
    message: str,
    final_check: bool,
) -> None:
    requires_reapproval = (
        final_check
        or "Version" in message
        or "Vereins-Karussell" in message
    )
    if message == "Not-Aus aktiv" or not requires_reapproval:
        # Preserve the established worker contract for temporary gates. The
        # scheduler may retry these jobs after the gate has been resolved.
        job.status = JobStatus.SCHEDULED
    elif "Zeitpunkt nicht erreicht" not in message:
        job.status = JobStatus.UNAPPROVED
        job.approval_status = "reapproval_required"
        if "Version" in message or "Vereins-Karussell" in message:
            post.status = PostStatus.REAPPROVAL
    if final_check:
        job.error = f"Prüfung unmittelbar vor Veröffentlichung: {message}"
    db.commit()
    raise PublishError(message)


def process_job(
    db: Session,
    job_id: str,
    publisher: SocialMediaPublisher,
    settings: Settings,
):
    job = db.scalar(
        select(PublicationJob).where(PublicationJob.id == job_id).with_for_update()
    )
    if not job or job.status in {JobStatus.PUBLISHED, JobStatus.PUBLISHING}:
        return job
    post = db.get(Post, job.post_id)
    game = db.get(Game, job.game_id) if job.game_id else None
    team = db.get(Team, job.team_id)
    page = db.get(InstagramPage, job.instagram_page_id)
    stop = db.get(SystemSetting, "emergency_stop")
    media_items = list(
        db.scalars(
            select(PublicationMediaItem)
            .where(PublicationMediaItem.publication_job_id == job.id)
            .order_by(PublicationMediaItem.position)
        )
    )
    carousel_ok = job.kind != "carousel" or (
        2 <= len(media_items) <= 10
        and [item.position for item in media_items]
        == list(range(1, len(media_items) + 1))
        and all(Path(item.media_path).is_file() for item in media_items)
    )
    grouped_ok, grouped_error = _grouped_games_are_publishable(db, post)
    checks = [
        (settings.global_publish_enabled, "Globales Publishing ist nicht aktiviert"),
        (not (stop and stop.value.get("enabled")), "Not-Aus aktiv"),
        (
            post.status
            in {PostStatus.APPROVED, PostStatus.SCHEDULED, PostStatus.PARTIAL},
            "Beitrag nicht freigegeben",
        ),
        (post.version == job.approved_post_version, "Freigegebene Version verändert"),
        (
            game is None or game.status not in {"cancelled", "postponed", "provisional"},
            "Spielstatus sperrt Veröffentlichung",
        ),
        (
            game is None or not (game.overrides or {}).get("automation_blocked"),
            "Spiel ist für Automatisierung gesperrt",
        ),
        (grouped_ok, grouped_error),
        (not job.stale_time, "Veröffentlichungszeit ist möglicherweise veraltet"),
        (
            post.publishing_enabled
            and team.publishing_enabled
            and page.publishing_enabled,
            "Publishing deaktiviert",
        ),
        (
            page.active and page.connection_status == "connected",
            "Instagram-Seite gestört",
        ),
        (Path(job.media_path).is_file(), "Mediendatei fehlt"),
        (carousel_ok, "Karussellbilder fehlen oder Reihenfolge ist ungültig"),
        (_aware_utc(job.scheduled_at) <= datetime.now(timezone.utc), "Zeitpunkt nicht erreicht"),
    ]
    for ok, message in checks:
        if not ok:
            _reject_before_publish(
                db, post=post, job=job, message=message, final_check=False
            )

    job.status = JobStatus.PUBLISHING
    job.attempts += 1
    job.last_attempt_at = datetime.now(timezone.utc)
    db.commit()

    db.expire_all()
    job = db.get(PublicationJob, job_id)
    post = db.get(Post, job.post_id)
    game = db.get(Game, job.game_id) if job.game_id else None
    stop = db.get(SystemSetting, "emergency_stop")
    grouped_ok, grouped_error = _grouped_games_are_publishable(db, post)
    final_checks = [
        (not (stop and stop.value.get("enabled")), "Not-Aus aktiv"),
        (
            post.status
            in {PostStatus.APPROVED, PostStatus.SCHEDULED, PostStatus.PARTIAL},
            "Beitrag nicht mehr freigegeben",
        ),
        (post.version == job.approved_post_version, "Freigegebene Version verändert"),
        (
            game is None or game.status not in {"cancelled", "postponed", "provisional"},
            "Spielstatus sperrt Veröffentlichung",
        ),
        (
            game is None or not (game.overrides or {}).get("automation_blocked"),
            "Spiel ist für Automatisierung gesperrt",
        ),
        (grouped_ok, grouped_error),
        (not job.stale_time, "Veröffentlichungszeit ist möglicherweise veraltet"),
    ]
    for ok, message in final_checks:
        if not ok:
            _reject_before_publish(
                db, post=post, job=job, message=message, final_check=True
            )

    try:
        result = publisher.publish(
            account_id=page.account_id,
            kind=job.kind,
            media_url=job.media_path,
            caption=job.text_snapshot,
            idempotency_key=job.idempotency_key,
        )
    except PublishError as exc:
        job.error = str(exc)
        job.status = (
            JobStatus.RETRY
            if exc.retryable and job.attempts < settings.max_publish_attempts
            else (JobStatus.UNCERTAIN if "unklar" in str(exc) else JobStatus.FAILED)
        )
        db.commit()
        raise
    if not result.confirmed:
        job.status = JobStatus.UNCERTAIN if result.uncertain else JobStatus.FAILED
        job.platform_id = result.platform_id
        db.commit()
        return job

    job.status = JobStatus.PUBLISHED
    job.platform_id = result.platform_id
    job.permalink = result.permalink
    job.published_at = datetime.now(timezone.utc)
    statuses = list(
        db.scalars(
            select(PublicationJob.status).where(PublicationJob.post_id == post.id)
        )
    )
    finished = {JobStatus.PUBLISHED, JobStatus.SKIPPED, JobStatus.CANCELLED}
    if all(value in finished for value in statuses):
        post.status = PostStatus.PUBLISHED
    elif any(value == JobStatus.PUBLISHED for value in statuses):
        post.status = PostStatus.PARTIAL
    db.commit()
    return job
