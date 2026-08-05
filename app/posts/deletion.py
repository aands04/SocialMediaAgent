from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    AuditLog,
    GenerationJob,
    GenerationJobStatus,
    JobStatus,
    MediaAsset,
    MetaCarouselItem,
    MetaPublishConfirmation,
    MetaPublishingAttempt,
    Post,
    PostStatus,
    PublicationJob,
    PublicationMediaItem,
    PublicMediaGrant,
    User,
)


class PostDeletionConflict(ValueError):
    pass


@dataclass(frozen=True)
class PostDeletionResult:
    post_id: str
    removed_files: int
    publication_jobs: int


ACTIVE_GENERATION_STATUSES = {
    GenerationJobStatus.QUEUED,
    GenerationJobStatus.RUNNING,
    GenerationJobStatus.RETRY_WAIT,
}


def _safe_remove(paths: set[str], roots: tuple[Path, ...]) -> int:
    resolved_roots = tuple(root.resolve() for root in roots)
    removed = 0
    for value in paths:
        if not value:
            continue
        path = Path(value)
        try:
            resolved = path.resolve()
        except OSError:
            continue
        root = next(
            (candidate for candidate in resolved_roots if resolved.is_relative_to(candidate)),
            None,
        )
        if root is None or resolved == root or resolved.is_symlink():
            continue
        try:
            if resolved.is_file():
                resolved.unlink()
                removed += 1
            parent = resolved.parent
            while parent != root and parent.is_relative_to(root):
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        except OSError:
            # The database deletion is authoritative.  A later maintenance
            # cleanup may remove a file that was concurrently locked.
            continue
    return removed


def delete_unpublished_post(
    db: Session,
    settings: Settings,
    post: Post,
    user: User,
    *,
    expected_version: int,
    reason: str = "",
) -> PostDeletionResult:
    reason = reason.strip()
    locked = db.scalar(select(Post).where(Post.id == post.id).with_for_update())
    if not locked:
        raise PostDeletionConflict("Der Beitrag wurde bereits gelöscht")
    if locked.version != expected_version:
        raise PostDeletionConflict("Der Beitrag wurde zwischenzeitlich geändert")
    if locked.status in {PostStatus.PUBLISHED, PostStatus.PARTIAL}:
        raise PostDeletionConflict(
            "Bereits ganz oder teilweise veröffentlichte Beiträge dürfen nicht gelöscht werden"
        )

    publications = list(
        db.scalars(
            select(PublicationJob).where(PublicationJob.post_id == locked.id).with_for_update()
        )
    )
    if any(
        publication.status in {JobStatus.PUBLISHING, JobStatus.PUBLISHED, JobStatus.UNCERTAIN}
        or publication.platform_id
        or publication.published_at
        for publication in publications
    ):
        raise PostDeletionConflict(
            "Der Beitrag besitzt einen laufenden, unklaren oder veröffentlichten Plattformvorgang"
        )

    generation_jobs = list(
        db.scalars(
            select(GenerationJob)
            .where(
                or_(
                    GenerationJob.post_id == locked.id,
                    GenerationJob.result_post_id == locked.id,
                )
            )
            .with_for_update()
        )
    )
    if any(job.status in ACTIVE_GENERATION_STATUSES for job in generation_jobs):
        raise PostDeletionConflict(
            "Für diesen Beitrag läuft noch ein Generierungsauftrag; bitte zuerst abbrechen oder abschließen"
        )

    publication_ids = [publication.id for publication in publications]
    attempts = (
        list(
            db.scalars(
                select(MetaPublishingAttempt)
                .where(MetaPublishingAttempt.publication_job_id.in_(publication_ids))
                .with_for_update()
            )
        )
        if publication_ids
        else []
    )
    if any(
        attempt.active_key
        or attempt.meta_container_id
        or attempt.meta_media_id
        or attempt.phase
        in {"creating_container", "waiting_for_container", "publishing", "reconciling", "uncertain"}
        for attempt in attempts
    ):
        raise PostDeletionConflict(
            "Der Beitrag besitzt bereits einen Meta-Container oder einen unklaren Meta-Vorgang"
        )

    media_items = (
        list(
            db.scalars(
                select(PublicationMediaItem).where(
                    PublicationMediaItem.publication_job_id.in_(publication_ids)
                )
            )
        )
        if publication_ids
        else []
    )
    candidate_paths = {
        value
        for value in [
            locked.feed_path,
            *(publication.media_path for publication in publications),
            *(media.media_path for media in media_items),
        ]
        if value
    }

    if locked.media_asset_id and locked.game_id:
        asset = db.scalar(
            select(MediaAsset).where(MediaAsset.id == locked.media_asset_id).with_for_update()
        )
        other_post = db.scalar(
            select(Post.id).where(
                Post.id != locked.id,
                Post.media_asset_id == locked.media_asset_id,
                Post.game_id == locked.game_id,
            )
        )
        if asset and not other_post and asset.reserved_game_id == locked.game_id:
            asset.reserved_game_id = None
            asset.uses = max(0, asset.uses - 1)

    attempt_ids = [attempt.id for attempt in attempts]
    if attempt_ids:
        db.execute(
            delete(MetaPublishConfirmation).where(
                MetaPublishConfirmation.attempt_id.in_(attempt_ids)
            )
        )
        db.execute(delete(MetaCarouselItem).where(MetaCarouselItem.attempt_id.in_(attempt_ids)))
        db.execute(delete(MetaPublishingAttempt).where(MetaPublishingAttempt.id.in_(attempt_ids)))
    if publication_ids:
        db.execute(
            delete(PublicMediaGrant).where(PublicMediaGrant.publication_job_id.in_(publication_ids))
        )
        db.execute(
            delete(PublicationMediaItem).where(
                PublicationMediaItem.publication_job_id.in_(publication_ids)
            )
        )
        db.execute(delete(PublicationJob).where(PublicationJob.id.in_(publication_ids)))
    if generation_jobs:
        db.execute(
            delete(GenerationJob).where(GenerationJob.id.in_([job.id for job in generation_jobs]))
        )

    db.add(
        AuditLog(
            user_id=user.id,
            team_id=locked.team_id,
            action="post.deleted",
            entity_type="post",
            entity_id=locked.id,
            details={
                "reason": reason or None,
                "post_type": locked.post_type,
                "version": locked.version,
                "publication_jobs": len(publications),
                "files_scheduled_for_removal": len(candidate_paths),
            },
        )
    )
    post_id = locked.id
    db.delete(locked)
    db.commit()

    removed = _safe_remove(
        candidate_paths,
        (settings.generated_root, settings.upload_root),
    )
    return PostDeletionResult(post_id, removed, len(publications))
