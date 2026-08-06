"""Coordinate same-club matchday feeds without merging per-game stories."""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.games.bundles import generation_bundle_games
from app.models import (
    AuditLog,
    Game,
    JobStatus,
    Post,
    PostStatus,
    PublicationJob,
    PublicationMediaItem,
    Team,
)

BERLIN = ZoneInfo("Europe/Berlin")
GROUPABLE_POST_TYPES = {"announcement", "result"}
APPROVED_POST_STATUSES = {
    PostStatus.APPROVED,
    PostStatus.SCHEDULED,
    PostStatus.PARTIAL,
}


class ClubCarouselConflict(ValueError):
    pass


@dataclass(frozen=True)
class ClubCarouselState:
    active: bool = False
    complete: bool = False
    primary_post_id: str | None = None
    member_post_ids: tuple[str, ...] = ()
    waiting_for: tuple[str, ...] = ()


def matchday_bundle_posts(db: Session, post: Post) -> list[Post]:
    """Return a verified, ordered bundle without crossing the tenant boundary.

    Publication jobs deliberately keep their original ``post_id`` so edits,
    retries and publishing checks continue to use the correct game and team.
    This helper provides the aggregate read model used by the dashboard.
    """
    bundle = (post.design_snapshot or {}).get("club_matchday_carousel") or {}
    primary_post_id = str(bundle.get("primary_post_id") or "").strip()
    raw_member_ids = bundle.get("member_post_ids") or []
    member_ids = [str(item).strip() for item in raw_member_ids if str(item).strip()]
    member_ids = list(dict.fromkeys(member_ids))
    if not primary_post_id or len(member_ids) < 2 or post.id not in member_ids:
        return [post]
    if primary_post_id not in member_ids:
        raise ClubCarouselConflict("Der gemeinsame Beitrag besitzt keinen gültigen Hauptbeitrag")

    members = {
        item.id: item
        for item in db.scalars(
            select(Post).where(
                Post.club_id == post.club_id,
                Post.id.in_(member_ids),
            )
        )
    }
    if len(members) != len(member_ids):
        raise ClubCarouselConflict(
            "Mindestens ein Teilbeitrag des gemeinsamen Spieltags fehlt oder gehört zu einem anderen Verein"
        )
    ordered = [members[item_id] for item_id in member_ids]
    for member in ordered:
        member_bundle = (member.design_snapshot or {}).get("club_matchday_carousel") or {}
        if (
            member_bundle.get("primary_post_id") != primary_post_id
            or list(member_bundle.get("member_post_ids") or []) != member_ids
        ):
            raise ClubCarouselConflict(
                "Die Zuordnung der Teilbeiträge zum gemeinsamen Spieltag ist widersprüchlich"
            )
    return ordered


def matchday_bundle_jobs(
    db: Session, post: Post
) -> tuple[Post, list[Post], list[PublicationJob], dict[str, Post]]:
    """Build the dashboard view for a complete matchday contribution.

    It contains the single carousel publication of the primary post and every
    per-game story publication. Cancelled member feed jobs are intentionally
    excluded because their images already live in the carousel.
    """
    members = matchday_bundle_posts(db, post)
    if len(members) == 1:
        jobs = list(
            db.scalars(
                select(PublicationJob)
                .where(PublicationJob.post_id == post.id)
                .order_by(PublicationJob.scheduled_at)
            )
        )
        return post, members, jobs, {job.id: post for job in jobs}

    bundle = (post.design_snapshot or {}).get("club_matchday_carousel") or {}
    primary_id = str(bundle["primary_post_id"])
    primary = next(item for item in members if item.id == primary_id)
    member_ids = [item.id for item in members]
    jobs = list(
        db.scalars(
            select(PublicationJob).where(PublicationJob.post_id.in_(member_ids))
        )
    )
    visible = [
        job
        for job in jobs
        if job.kind == "story"
        or (job.post_id == primary.id and job.kind in {"feed", "carousel"})
    ]
    member_order = {member.id: position for position, member in enumerate(members)}
    visible.sort(
        key=lambda job: (
            0 if job.kind in {"feed", "carousel"} else 1,
            job.scheduled_at,
            member_order[job.post_id],
            job.id,
        )
    )
    post_by_id = {member.id: member for member in members}
    return primary, members, visible, {job.id: post_by_id[job.post_id] for job in visible}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _mode_groups(mode: str, post_type: str) -> bool:
    return mode == "announcements_and_results" or (
        mode == "announcements" and post_type == "announcement"
    )


def _feed_job(db: Session, post_id: str) -> PublicationJob | None:
    return db.scalar(
        select(PublicationJob)
        .where(
            PublicationJob.post_id == post_id,
            PublicationJob.kind.in_(["feed", "carousel"]),
        )
        .with_for_update()
    )


def _combined_text(club: str, post_type: str, games: list[Game], teams: dict[str, Team]) -> str:
    local_day = _utc(games[0].kickoff).astimezone(BERLIN)
    if post_type == "result":
        heading = f"⚽ Ergebnisse vom {local_day:%d.%m.%Y}"
        lines = [
            f"• {game.home_team} {game.home_score}:{game.away_score} {game.away_team}"
            for game in games
        ]
    else:
        heading = f"⚽ Spieltag für {club} am {local_day:%d.%m.%Y}"
        lines = [
            f"• {_utc(game.kickoff).astimezone(BERLIN):%H:%M} Uhr: "
            f"{game.home_team} – {game.away_team}" + (f" · {game.venue}" if game.venue else "")
            for game in games
        ]
    hashtags: list[str] = []
    for game in games:
        for hashtag in teams[game.team_id].hashtags or []:
            if hashtag not in hashtags:
                hashtags.append(hashtag)
    suffix = f"\n\n{' '.join(hashtags)}" if hashtags else ""
    return heading + "\n\n" + "\n".join(lines) + suffix


def _candidate_games(
    db: Session, post: Post, game: Game, team: Team
) -> tuple[list[Game], dict[str, Team]]:
    games, teams, key = generation_bundle_games(db, game, team, post.post_type)
    return (games, teams) if key and len(games) >= 2 else ([], teams)


def _mark_waiting(db: Session, post: Post, waiting_for: list[str]) -> None:
    feed = _feed_job(db, post.id)
    if not feed or feed.status in {JobStatus.PUBLISHED, JobStatus.PUBLISHING}:
        return
    feed.status = JobStatus.WAITING
    feed.approval_status = "bundle_wait"
    feed.approved_post_version = None
    feed.error = "Gemeinsamer Vereins-Feed wartet auf: " + ", ".join(waiting_for)


def coordinate_club_matchday_feed(
    db: Session,
    post: Post,
    *,
    requested_by: str | None,
) -> ClubCarouselState:
    """Bundle feeds once every configured team/game has produced its post.

    Each Post and all story jobs remain game-specific. Only one feed job becomes
    a carousel; the other feed jobs are cancelled as explicit bundle members.
    """
    if post.post_type not in GROUPABLE_POST_TYPES or not post.game_id:
        return ClubCarouselState()
    game = db.get(Game, post.game_id)
    team = db.get(Team, post.team_id)
    if not game or not team:
        return ClubCarouselState()
    games, teams = _candidate_games(db, post, game, team)
    if not games:
        return ClubCarouselState()
    mode = str((team.rules or {}).get("club_matchday_feed_mode", "separate"))
    manual_bundle = bool((game.overrides or {}).get("generation_bundle_id"))
    if not manual_bundle and not _mode_groups(mode, post.post_type):
        return ClubCarouselState()

    if post.post_type == "result":
        missing_results = [
            teams[item.team_id].display_name for item in games if not item.result_confirmed
        ]
        if missing_results:
            _mark_waiting(db, post, missing_results)
            db.flush()
            return ClubCarouselState(
                active=True,
                waiting_for=tuple(missing_results),
            )

    game_ids = [item.id for item in games]
    posts = list(
        db.scalars(
            select(Post)
            .where(
                Post.game_id.in_(game_ids),
                Post.post_type == post.post_type,
                Post.active_key == "active",
            )
            .order_by(Post.id)
            .with_for_update()
        )
    )
    by_game = {item.game_id: item for item in posts}
    missing_posts = [teams[item.team_id].display_name for item in games if item.id not in by_game]
    if missing_posts:
        _mark_waiting(db, post, missing_posts)
        db.flush()
        return ClubCarouselState(active=True, waiting_for=tuple(missing_posts))

    ordered_posts = [by_game[item.id] for item in games]
    feed_jobs = [_feed_job(db, item.id) for item in ordered_posts]
    if any(item is None for item in feed_jobs):
        raise ClubCarouselConflict("Mindestens ein Feed-Auftrag des Vereins fehlt")
    if any(
        item.status in {JobStatus.PUBLISHED, JobStatus.PUBLISHING}
        for item in feed_jobs
        if item is not None
    ):
        raise ClubCarouselConflict(
            "Ein Feed des gemeinsamen Spieltags wurde bereits veröffentlicht"
        )

    primary = ordered_posts[0]
    primary_feed = feed_jobs[0]
    assert primary_feed is not None
    bundle_key = f"{_utc(games[0].kickoff).astimezone(BERLIN):%Y-%m-%d}:{post.post_type}"
    old_bundle = (primary.design_snapshot or {}).get("club_matchday_carousel") or {}
    if old_bundle.get("key") == bundle_key and primary_feed.kind == "carousel":
        return ClubCarouselState(
            active=True,
            complete=True,
            primary_post_id=primary.id,
            member_post_ids=tuple(item.id for item in ordered_posts),
        )

    if primary.status in APPROVED_POST_STATUSES:
        primary.status = PostStatus.REAPPROVAL
        primary.approved_version = None
        primary.approved_by = None
        primary.approved_at = None
        primary.version += 1
        for open_job in db.scalars(
            select(PublicationJob).where(
                PublicationJob.post_id == primary.id,
                PublicationJob.status.not_in(
                    [JobStatus.PUBLISHED, JobStatus.CANCELLED, JobStatus.SKIPPED]
                ),
            )
        ):
            open_job.status = JobStatus.UNAPPROVED
            open_job.approval_status = "reapproval_required"
            open_job.approved_post_version = None
            open_job.error = (
                "Gemeinsamer Vereins-Karussellfeed wurde erstellt; erneute Freigabe erforderlich"
            )
    shared_texts = {
        item.text
        for item in ordered_posts
        if (item.design_snapshot or {}).get("matchday_bundle")
        and item.text
    }
    primary.text = (
        shared_texts.pop()
        if len(shared_texts) == 1
        else _combined_text(team.club, post.post_type, games, teams)
    )
    primary.text_version += 1
    primary_feed.kind = "carousel"
    primary_feed.media_path = ordered_posts[0].feed_path
    primary_feed.text_snapshot = primary.text
    primary_feed.scheduled_at = (
        max(item.scheduled_at for item in feed_jobs if item is not None)
        if post.post_type == "result"
        else min(item.scheduled_at for item in feed_jobs if item is not None)
    )
    primary_feed.absolute_time = any(item.absolute_time for item in feed_jobs if item is not None)
    primary_feed.status = JobStatus.UNAPPROVED
    primary_feed.approval_status = "reapproval_required"
    primary_feed.approved_post_version = None
    primary_feed.error = "Gemeinsamer Vereins-Karussellfeed wurde erstellt; Freigabe erforderlich"
    primary_feed.idempotency_key = f"{primary.id}:club-carousel:{post.post_type}:v{primary.version}"

    db.execute(
        delete(PublicationMediaItem).where(
            PublicationMediaItem.publication_job_id == primary_feed.id
        )
    )
    for position, member in enumerate(ordered_posts, start=1):
        path = Path(member.feed_path or "")
        if not path.is_file():
            raise ClubCarouselConflict(f"Feed-Datei für {teams[member.team_id].display_name} fehlt")
        payload = path.read_bytes()
        with Image.open(path) as image:
            width, height = image.size
        db.add(
            PublicationMediaItem(
                publication_job_id=primary_feed.id,
                position=position,
                media_path=str(path),
                checksum=sha256(payload).hexdigest(),
                mime_type="image/png",
                file_size=len(payload),
                width=width,
                height=height,
            )
        )

    member_ids = [item.id for item in ordered_posts]
    for member, feed in zip(ordered_posts, feed_jobs, strict=True):
        snapshot = dict(member.design_snapshot or {})
        snapshot["club_matchday_carousel"] = {
            "key": bundle_key,
            "mode": mode,
            "source": "manual" if manual_bundle else "club_rule",
            "post_type": post.post_type,
            "primary_post_id": primary.id,
            "member_post_ids": member_ids,
            "game_ids": game_ids,
            "role": "primary" if member.id == primary.id else "member",
            "stories_remain_separate": True,
            "text_model": (
                "single-shared-ai-prompt-v1"
                if (member.design_snapshot or {}).get("matchday_bundle")
                else "deterministic-club-matchday-v1"
            ),
        }
        member.design_snapshot = snapshot
        if member.id == primary.id:
            continue
        assert feed is not None
        feed.status = JobStatus.CANCELLED
        feed.approval_status = "bundled"
        feed.approved_post_version = None
        feed.error = f"Im Vereins-Karussell {primary.id} gebündelt"

    db.add(
        AuditLog(
            user_id=requested_by,
            team_id=primary.team_id,
            action="post.club_matchday_carousel_created",
            entity_type="post",
            entity_id=primary.id,
            details={
                "post_type": post.post_type,
                "game_ids": game_ids,
                "post_ids": member_ids,
                "stories_remain_separate": True,
            },
        )
    )
    db.flush()
    return ClubCarouselState(
        active=True,
        complete=True,
        primary_post_id=primary.id,
        member_post_ids=tuple(member_ids),
    )
