"""Coordinate same-club matchday feeds without merging per-game stories."""

from dataclasses import dataclass
from datetime import datetime, time, timezone
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.logos.service import normalize_club_name
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
    club_key = normalize_club_name(team.club)
    teams = {
        item.id: item
        for item in db.scalars(
            select(Team).where(
                Team.instagram_page_id == team.instagram_page_id,
                Team.active.is_(True),
                Team.archived_at.is_(None),
            )
        )
        if normalize_club_name(item.club) == club_key
        and _mode_groups(
            str((item.rules or {}).get("club_matchday_feed_mode", "separate")),
            post.post_type,
        )
        and bool(
            (item.rules or {}).get(
                "result_enabled" if post.post_type == "result" else "announcement_enabled"
            )
        )
    }
    if len(teams) < 2:
        return [], teams
    local_date = _utc(game.kickoff).astimezone(BERLIN).date()
    start = datetime.combine(local_date, time.min, BERLIN).astimezone(timezone.utc)
    end = datetime.combine(local_date, time.max, BERLIN).astimezone(timezone.utc)
    games = list(
        db.scalars(
            select(Game).where(
                Game.team_id.in_(teams),
                Game.kickoff >= start,
                Game.kickoff <= end,
                Game.status.not_in(["cancelled", "postponed"]),
            )
        )
    )
    games = [
        item
        for item in games
        if not (item.overrides or {}).get("automation_blocked")
        and not (item.overrides or {}).get("import_suppressed")
    ]
    if len({item.team_id for item in games}) < 2:
        return [], teams
    preferred_team_id = str((team.rules or {}).get("club_matchday_primary_team_id") or "")
    games.sort(
        key=lambda item: (
            0 if item.team_id == preferred_team_id else 1,
            _utc(item.kickoff),
            teams[item.team_id].display_name,
            item.id,
        )
    )
    return games, teams


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
    mode = str((team.rules or {}).get("club_matchday_feed_mode", "separate"))
    if not _mode_groups(mode, post.post_type):
        return ClubCarouselState()
    games, teams = _candidate_games(db, post, game, team)
    if not games:
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
    primary.text = _combined_text(team.club, post.post_type, games, teams)
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
            "post_type": post.post_type,
            "primary_post_id": primary.id,
            "member_post_ids": member_ids,
            "game_ids": game_ids,
            "role": "primary" if member.id == primary.id else "member",
            "stories_remain_separate": True,
            "text_model": "deterministic-club-matchday-v1",
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
