"""Retire shared contributions atomically; published history is never rewritten."""

from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.service import allowed
from app.models import (
    AuditLog,
    Game,
    GenerationJob,
    GenerationJobStatus,
    JobStatus,
    MetaPublishingAttempt,
    Post,
    PublicationJob,
    User,
)
from app.posts.club_carousel import matchday_bundle_posts


def separate_matchday_posts(
    db: Session,
    games: list[Game],
    user: User | None = None,
) -> list[Game]:
    """Validate the complete affected group before writing; caller owns commit.

    A shared caption/carousel cannot safely be reused as an individual draft.
    Keep every version and platform receipt, cancel only unsent work, and free
    the active contribution slot so each game can generate fresh content.
    """
    if (
        not games
        or any(game is None for game in games)
        or len({game.club_id for game in games}) != 1
    ):
        raise ValueError("Die Spiele müssen zum selben Verein gehören")
    club_id = games[0].club_id
    game_ids = {game.id for game in games}
    posts = list(
        db.scalars(
            select(Post)
            .where(
                Post.club_id == club_id,
                Post.active_key == "active",
            )
            .order_by(Post.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    selected: dict[str, Post] = {}
    generation_jobs = list(
        db.scalars(
            select(GenerationJob)
            .where(
                GenerationJob.club_id == club_id,
            )
            .order_by(GenerationJob.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    # Close over both persisted contributions and unfinished coordinators.
    changed = True
    while changed:
        before = set(game_ids)
        for post in posts:
            bundle = (post.design_snapshot or {}).get("club_matchday_carousel") or {}
            if not bundle.get("member_post_ids"):
                continue
            if post.game_id in game_ids or game_ids.intersection(bundle.get("game_ids") or []):
                for member in matchday_bundle_posts(db, post):
                    selected[member.id] = member
                    if member.game_id:
                        game_ids.add(member.game_id)
        for job in generation_jobs:
            ids = set((job.parameters or {}).get("bundle_game_ids") or [])
            if job.status in {
                GenerationJobStatus.QUEUED,
                GenerationJobStatus.RETRY_WAIT,
                GenerationJobStatus.RUNNING,
            } and ids.intersection(game_ids):
                game_ids.update(ids)
        changed = before != game_ids
    affected_games = list(
        db.scalars(
            select(Game)
            .where(
                Game.club_id == club_id,
                Game.id.in_(game_ids),
            )
            .order_by(Game.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(affected_games) != len(game_ids):
        raise ValueError("Die Spielgruppe ist unvollständig oder vereinsfremd")
    if user and user.club_id != club_id:
        raise PermissionError("Keine vereinsübergreifende Spieltrennung erlaubt")
    if user and any(
        not allowed(db, user, permission, game.team_id)
        for game in affected_games
        for permission in ("edit_game", "approve")
    ):
        raise PermissionError("Keine Berechtigung für alle betroffenen Mannschaften")
    affected_generation = [
        job
        for job in generation_jobs
        if (
            job.post_id in selected
            or job.result_post_id in selected
            or game_ids.intersection((job.parameters or {}).get("bundle_game_ids") or [])
        )
    ]
    if any(job.status == GenerationJobStatus.RUNNING for job in affected_generation):
        raise ValueError("Eine Generierung läuft noch; bitte zuerst abschließen oder abbrechen")
    publications = list(
        db.scalars(
            select(PublicationJob)
            .where(
                PublicationJob.club_id == club_id,
                PublicationJob.post_id.in_(selected),
            )
            .order_by(PublicationJob.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    open_jobs = [
        job
        for job in publications
        if job.status not in {JobStatus.PUBLISHED, JobStatus.CANCELLED, JobStatus.SKIPPED}
    ]
    if any(
        job.status in {JobStatus.PUBLISHING, JobStatus.UNCERTAIN}
        or job.platform_id
        or job.published_at
        or job.locked_at
        for job in open_jobs
    ):
        raise ValueError("Ein laufender oder unklarer Plattformvorgang muss zuerst geklärt werden")
    attempts = list(
        db.scalars(
            select(MetaPublishingAttempt)
            .where(
                MetaPublishingAttempt.club_id == club_id,
                MetaPublishingAttempt.publication_job_id.in_([job.id for job in open_jobs]),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if any(
        attempt.active_key
        or attempt.meta_container_id
        or attempt.meta_media_id
        or attempt.phase
        in {"creating_container", "waiting_for_container", "publishing", "reconciling", "uncertain"}
        for attempt in attempts
    ):
        raise ValueError("Ein Meta-Vorgang muss vor dem Trennen geklärt werden")
    if (
        not selected
        and not any(
            job.status in {GenerationJobStatus.QUEUED, GenerationJobStatus.RETRY_WAIT}
            for job in affected_generation
        )
        and all(
            (game.overrides or {}).get("generation_bundle_separated") for game in affected_games
        )
    ):
        return affected_games
    marker = uuid4().hex[:20]
    for job in affected_generation:
        if job.status in {GenerationJobStatus.QUEUED, GenerationJobStatus.RETRY_WAIT}:
            job.status = GenerationJobStatus.CANCELLED
            job.cancel_requested = True
            job.active_key = None
            job.completed_at = datetime.now(timezone.utc)
    for job in open_jobs:
        job.status = JobStatus.CANCELLED
        job.approval_status = "unapproved"
        job.approved_post_version = None
        job.error = "Gemeinsamer Spieltag getrennt; neuer Einzelbeitrag erforderlich"
        job.version += 1
    for post in selected.values():
        post.active_key = marker
        post.publishing_enabled = False
        post.version += 1
    for game in affected_games:
        overrides = dict(game.overrides or {})
        overrides.pop("generation_bundle_id", None)
        overrides.pop("generation_bundle_source", None)
        overrides["generation_bundle_separated"] = True
        overrides["generation_revision"] = marker
        game.overrides = overrides
        game.version += 1
    db.add(
        AuditLog(
            club_id=club_id,
            user_id=user.id if user else None,
            team_id=games[0].team_id,
            action="games.matchday_posts_separated",
            entity_type="game_bundle",
            details={
                "game_ids": sorted(game_ids),
                "archived_post_ids": sorted(selected),
                "cancelled_publication_ids": [job.id for job in open_jobs],
            },
        )
    )
    db.flush()
    return affected_games


def separate_rescheduled_matchday(db: Session, game: Game, kickoff: datetime) -> bool:
    """A move to another Berlin day dissolves existing and queued bundles."""

    def day(value):
        return (
            value.replace(tzinfo=value.tzinfo or timezone.utc)
            .astimezone(ZoneInfo("Europe/Berlin"))
            .date()
        )

    if day(game.kickoff) == day(kickoff):
        return False
    shared_post = any(
        (post.design_snapshot or {}).get("club_matchday_carousel")
        for post in db.scalars(
            select(Post).where(
                Post.club_id == game.club_id,
                Post.game_id == game.id,
                Post.active_key == "active",
            )
        )
    )
    queued_bundle = any(
        game.id in ((job.parameters or {}).get("bundle_game_ids") or [])
        for job in db.scalars(
            select(GenerationJob).where(
                GenerationJob.club_id == game.club_id,
                GenerationJob.status.in_(
                    {
                        GenerationJobStatus.QUEUED,
                        GenerationJobStatus.RETRY_WAIT,
                        GenerationJobStatus.RUNNING,
                    }
                ),
            )
        )
    )
    if not shared_post and not queued_bundle:
        return False
    separate_matchday_posts(db, [game])
    return True
