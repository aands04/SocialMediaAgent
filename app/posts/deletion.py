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
    posts: int = 1


ACTIVE_GENERATION_STATUSES = {
    GenerationJobStatus.QUEUED,
    GenerationJobStatus.RUNNING,
    GenerationJobStatus.RETRY_WAIT,
}


def _bundle_identity(post: Post) -> tuple[str, tuple[str, ...]] | None:
    bundle = (post.design_snapshot or {}).get("club_matchday_carousel") or {}
    primary_id = str(bundle.get("primary_post_id") or "").strip()
    member_ids = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in (bundle.get("member_post_ids") or [])
            if str(value).strip()
        )
    )
    if (
        not primary_id
        or len(member_ids) < 2
        or post.id not in member_ids
        or primary_id not in member_ids
    ):
        return None
    return primary_id, member_ids


def _locked_deletion_group(db: Session, post: Post) -> list[Post]:
    """Lock the post and every still-present member of its matchday bundle.

    Missing members are tolerated so a bundle damaged by an older single-post
    deletion can be cleaned up. Existing members must still be reciprocal and
    belong to the same tenant.
    """
    identity = _bundle_identity(post)
    member_ids = identity[1] if identity else (post.id,)
    foreign_member = db.scalar(
        select(Post.id).where(
            Post.id.in_(member_ids),
            Post.club_id != post.club_id,
        )
    )
    if foreign_member:
        raise PostDeletionConflict(
            "Der gemeinsame Beitrag enthält eine unzulässige vereinsfremde Referenz"
        )
    locked = {
        item.id: item
        for item in db.scalars(
            select(Post)
            .where(Post.club_id == post.club_id, Post.id.in_(member_ids))
            .with_for_update()
        )
    }
    if post.id not in locked:
        raise PostDeletionConflict("Der Beitrag wurde bereits gelöscht")
    if identity:
        for member in locked.values():
            if _bundle_identity(member) != identity:
                raise PostDeletionConflict(
                    "Die Zuordnung der Teilbeiträge zum gemeinsamen Spieltag ist widersprüchlich"
                )
    return [locked[item_id] for item_id in member_ids if item_id in locked]


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
    locked_posts = _locked_deletion_group(db, post)
    locked = next(item for item in locked_posts if item.id == post.id)
    if locked.version != expected_version:
        raise PostDeletionConflict("Der Beitrag wurde zwischenzeitlich geändert")
    if any(
        item.status in {PostStatus.PUBLISHED, PostStatus.PARTIAL}
        for item in locked_posts
    ):
        raise PostDeletionConflict(
            "Bereits ganz oder teilweise veröffentlichte Beiträge dürfen nicht gelöscht werden"
        )

    post_ids = [item.id for item in locked_posts]

    publications = list(
        db.scalars(
            select(PublicationJob)
            .where(PublicationJob.post_id.in_(post_ids))
            .with_for_update()
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
                    GenerationJob.post_id.in_(post_ids),
                    GenerationJob.result_post_id.in_(post_ids),
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
            *(item.feed_path for item in locked_posts),
            *(publication.media_path for publication in publications),
            *(media.media_path for media in media_items),
        ]
        if value
    }

    for member in locked_posts:
        if member.media_asset_id and member.game_id:
            asset = db.scalar(
                select(MediaAsset)
                .where(MediaAsset.id == member.media_asset_id)
                .with_for_update()
            )
            other_post = db.scalar(
                select(Post.id).where(
                    Post.id.not_in(post_ids),
                    Post.media_asset_id == member.media_asset_id,
                    Post.game_id == member.game_id,
                )
            )
            if asset and not other_post and asset.reserved_game_id == member.game_id:
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

    for member in locked_posts:
        db.add(
            AuditLog(
                user_id=user.id,
                team_id=member.team_id,
                action="post.deleted",
                entity_type="post",
                entity_id=member.id,
                details={
                    "reason": reason or None,
                    "post_type": member.post_type,
                    "version": member.version,
                    "bundle_post_ids": post_ids if len(post_ids) > 1 else None,
                    "publication_jobs": len(
                        [job for job in publications if job.post_id == member.id]
                    ),
                    "files_scheduled_for_removal": len(candidate_paths),
                },
            )
        )
    post_id = locked.id
    for member in locked_posts:
        db.delete(member)
    db.commit()

    removed = _safe_remove(
        candidate_paths,
        (settings.generated_root, settings.upload_root),
    )
    return PostDeletionResult(post_id, removed, len(publications), len(locked_posts))
