from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import structlog
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.meta.api import MetaApiClient, MetaApiError
from app.meta.media import MediaGrantError
from app.meta.publishing import (
    MetaPublishingError,
    assert_automatic_scheduler_environment,
    create_attempt,
    create_container,
    publish,
    refresh_container_status,
)
from app.models import (
    Game,
    InstagramConnection,
    InstagramPage,
    JobStatus,
    MetaPublishingAttempt,
    Post,
    PostStatus,
    PublicationJob,
    User,
)

log = structlog.get_logger()


@dataclass
class AutomaticPublishingCycle:
    queued: int = 0
    containers_created: int = 0
    statuses_checked: int = 0
    published: int = 0
    paused: int = 0
    uncertain: int = 0


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _candidate_ids(db: Session, settings: Settings) -> list[str]:
    now = datetime.now(timezone.utc)
    query = (
        select(PublicationJob.id)
        .join(Post, Post.id == PublicationJob.post_id)
        .join(Game, Game.id == PublicationJob.game_id)
        .join(InstagramPage, InstagramPage.id == PublicationJob.instagram_page_id)
        .join(
            InstagramConnection,
            InstagramConnection.instagram_page_id == InstagramPage.id,
        )
        .where(
            PublicationJob.status.in_([JobStatus.SCHEDULED, JobStatus.RETRY]),
            PublicationJob.approval_status == "approved",
            PublicationJob.platform_id.is_(None),
            PublicationJob.scheduled_at <= now,
            or_(
                PublicationJob.next_attempt_at.is_(None),
                PublicationJob.next_attempt_at <= now,
            ),
            Post.status.in_(
                [PostStatus.APPROVED, PostStatus.SCHEDULED, PostStatus.PARTIAL]
            ),
            Post.approved_version.is_not(None),
            Post.approved_version == Post.version,
            PublicationJob.approved_post_version == Post.version,
            InstagramPage.active.is_(True),
            InstagramPage.publishing_enabled.is_(True),
            InstagramPage.automatic_publishing_enabled.is_(True),
            InstagramConnection.status == "connected",
            Game.status.notin_(["cancelled", "postponed", "provisional"]),
        )
        .order_by(PublicationJob.scheduled_at, PublicationJob.kind, PublicationJob.id)
        .limit(settings.meta_scheduler_batch_size)
    )
    return list(db.scalars(query))


def _attempt_ids(db: Session, settings: Settings) -> list[str]:
    now = datetime.now(timezone.utc)
    return list(
        db.scalars(
            select(MetaPublishingAttempt.id)
            .where(
                MetaPublishingAttempt.trigger_mode == "automatic",
                MetaPublishingAttempt.active_key.is_not(None),
                MetaPublishingAttempt.phase.in_(
                    [
                        "creating_media_grant",
                        "waiting_for_container",
                        "ready_to_publish",
                    ]
                ),
                or_(
                    MetaPublishingAttempt.next_action_at.is_(None),
                    MetaPublishingAttempt.next_action_at <= now,
                ),
            )
            .order_by(MetaPublishingAttempt.next_action_at, MetaPublishingAttempt.created_at)
            .limit(settings.meta_scheduler_batch_size)
        )
    )


def _mark_interrupted_calls_uncertain(db: Session, settings: Settings) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=max(120, int(settings.meta_http_timeout_seconds * 3))
    )
    attempts = db.scalars(
        select(MetaPublishingAttempt)
        .where(
            MetaPublishingAttempt.trigger_mode == "automatic",
            MetaPublishingAttempt.active_key.is_not(None),
            MetaPublishingAttempt.phase.in_(
                ["validating_public_media", "creating_container", "publishing"]
            ),
            MetaPublishingAttempt.updated_at < cutoff,
        )
        .with_for_update(skip_locked=True)
    ).all()
    for attempt in attempts:
        attempt.phase = "uncertain"
        attempt.error_category = "interrupted_external_call"
        attempt.error_message = (
            "Automatischer externer Schritt wurde unterbrochen; "
            "vor einer Wiederholung ist manueller Abgleich erforderlich"
        )
        job = db.get(PublicationJob, attempt.publication_job_id)
        if job:
            job.status = JobStatus.UNCERTAIN
            job.error = attempt.error_message
    if attempts:
        db.commit()
    return len(attempts)


def _pause_attempt(db: Session, attempt_id: str, message: str, settings: Settings) -> None:
    attempt = db.get(MetaPublishingAttempt, attempt_id)
    if not attempt or attempt.phase in {"completed", "failed", "uncertain"}:
        return
    attempt.error_category = "automatic_gate_paused"
    attempt.error_message = message
    attempt.next_action_at = datetime.now(timezone.utc) + timedelta(
        seconds=max(30, settings.meta_container_poll_interval_seconds)
    )
    db.commit()


def _poll_failed_safely(
    db: Session, attempt_id: str, message: str, settings: Settings
) -> None:
    attempt = db.get(MetaPublishingAttempt, attempt_id)
    if not attempt or attempt.phase != "waiting_for_container":
        return
    if datetime.now(timezone.utc) - _utc(attempt.created_at) >= timedelta(
        seconds=settings.meta_container_max_wait_seconds
    ):
        attempt.phase = "failed"
        attempt.active_key = None
        attempt.error_category = "container_timeout"
        attempt.error_message = "Meta-Container wurde nicht rechtzeitig fertig"
        job = db.get(PublicationJob, attempt.publication_job_id)
        if job:
            job.status = JobStatus.FAILED
            job.error = attempt.error_message
    else:
        attempt.error_category = "container_status_unavailable"
        attempt.error_message = message
        attempt.next_action_at = datetime.now(timezone.utc) + timedelta(
            seconds=settings.meta_container_poll_interval_seconds
        )
    db.commit()


def run_automatic_publishing_cycle(
    db: Session,
    settings: Settings,
    *,
    api: MetaApiClient | None = None,
    media_http_client: httpx.Client | None = None,
) -> AutomaticPublishingCycle:
    """Advance due, approved Instagram jobs by at most one external step each."""

    assert_automatic_scheduler_environment(settings)
    api = api or MetaApiClient(settings)
    owns_http_client = media_http_client is None
    media_http_client = media_http_client or httpx.Client(
        timeout=settings.meta_http_timeout_seconds
    )
    result = AutomaticPublishingCycle()
    try:
        result.uncertain = _mark_interrupted_calls_uncertain(db, settings)
        for job_id in _candidate_ids(db, settings):
            job = db.get(PublicationJob, job_id)
            post = db.get(Post, job.post_id) if job else None
            approver = db.get(User, post.approved_by) if post and post.approved_by else None
            if not approver:
                continue
            try:
                attempt, _ = create_attempt(
                    db,
                    settings,
                    publication_job_id=job_id,
                    stage="publish",
                    user=approver,
                    media_http_client=media_http_client,
                    trigger_mode="automatic",
                )
                if attempt.trigger_mode == "automatic":
                    result.queued += 1
            except (MediaGrantError, MetaPublishingError, MetaApiError) as exc:
                db.rollback()
                log.warning("automatic_meta_job_not_queued", job_id=job_id, error=str(exc))

        for attempt_id in _attempt_ids(db, settings):
            attempt = db.get(MetaPublishingAttempt, attempt_id)
            user = db.get(User, attempt.started_by) if attempt else None
            if not attempt or not user:
                continue
            phase = attempt.phase
            try:
                if phase == "creating_media_grant":
                    create_container(
                        db,
                        settings,
                        attempt_id=attempt_id,
                        user=user,
                        confirmation_code="",
                        api=api,
                        media_http_client=media_http_client,
                        automatic=True,
                    )
                    result.containers_created += 1
                elif phase == "waiting_for_container":
                    refresh_container_status(
                        db,
                        settings,
                        attempt_id=attempt_id,
                        user=user,
                        api=api,
                    )
                    result.statuses_checked += 1
                elif phase == "ready_to_publish":
                    publish(
                        db,
                        settings,
                        attempt_id=attempt_id,
                        user=user,
                        confirmation_code="",
                        api=api,
                        automatic=True,
                    )
                    result.published += 1
            except MetaApiError as exc:
                db.rollback()
                if phase == "waiting_for_container":
                    _poll_failed_safely(db, attempt_id, str(exc), settings)
                else:
                    _pause_attempt(db, attempt_id, str(exc), settings)
                result.paused += 1
            except (MediaGrantError, MetaPublishingError) as exc:
                db.rollback()
                _pause_attempt(db, attempt_id, str(exc), settings)
                result.paused += 1
                log.warning(
                    "automatic_meta_attempt_paused",
                    attempt_id=attempt_id,
                    phase=phase,
                    error=str(exc),
                )
        return result
    finally:
        if owns_http_client:
            media_http_client.close()
