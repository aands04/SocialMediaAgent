"""Explicit, audited and idempotent snapshot-to-game import; never creates posts."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Game,
    JobStatus,
    Post,
    PostStatus,
    ProviderSnapshot,
    PublicationJob,
    Team,
    User,
)


class SnapshotImportError(ValueError):
    pass


BLOCKING_PROVIDER_STATUSES = {"cancelled", "postponed", "provisional"}
REAPPROVAL_POST_STATUSES = {
    PostStatus.APPROVED,
    PostStatus.SCHEDULED,
    PostStatus.PARTIAL,
}
OPEN_JOB_STATUSES = {
    JobStatus.DRAFT,
    JobStatus.UNAPPROVED,
    JobStatus.APPROVED,
    JobStatus.SCHEDULED,
    JobStatus.WAITING,
    JobStatus.RETRY,
    JobStatus.FAILED,
}


def preview_snapshot(snapshot: ProviderSnapshot) -> list[dict]:
    games = snapshot.parser_result.get("games", []) if snapshot.parser_result else []
    return [
        game
        for game in games
        if all(game.get(key) for key in ("external_id", "home_team", "away_team", "kickoff"))
    ]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _different(current, value) -> bool:
    if isinstance(current, datetime) and isinstance(value, datetime):
        return _utc(current) != _utc(value)
    return current != value


def _invalidate_publications(db: Session, game: Game, kickoff_delta) -> None:
    open_jobs = db.scalars(
        select(PublicationJob).where(
            PublicationJob.game_id == game.id,
            PublicationJob.status.in_(OPEN_JOB_STATUSES),
        )
    ).all()
    if kickoff_delta:
        for job in open_jobs:
            if job.absolute_time:
                job.stale_time = True
            else:
                job.scheduled_at = _utc(job.scheduled_at) + kickoff_delta

    posts = db.scalars(
        select(Post).where(Post.game_id == game.id, Post.status.in_(REAPPROVAL_POST_STATUSES))
    ).all()
    post_ids = []
    for post in posts:
        post.status = PostStatus.REAPPROVAL
        post.version += 1
        post.approved_version = None
        post_ids.append(post.id)

    if not post_ids:
        return
    for job in (job for job in open_jobs if job.post_id in post_ids):
        job.status = JobStatus.UNAPPROVED
        job.approval_status = "reapproval_required"
        job.approved_post_version = None
        job.error = "Spieldaten wurden geändert; erneute Freigabe erforderlich"


def import_snapshot(db: Session, snapshot: ProviderSnapshot, user: User) -> dict:
    team = db.get(Team, snapshot.team_id)
    if not team:
        raise SnapshotImportError("Snapshot hat keine gültige Mannschaft")
    if snapshot.error:
        raise SnapshotImportError("Snapshot enthält einen Parserfehler")
    created = updated = unchanged = 0
    ids = []
    for item in preview_snapshot(snapshot):
        kickoff = datetime.fromisoformat(item["kickoff"])
        if kickoff.tzinfo is None:
            raise SnapshotImportError("Anpfiff ohne Zeitzone wird nicht übernommen")
        kickoff = kickoff.astimezone(timezone.utc)
        external_id = item["external_id"]
        game = db.scalar(
            select(Game)
            .where(
                Game.team_id == team.id,
                Game.provider == "fussball.de",
                Game.external_id == external_id,
            )
            .with_for_update()
        )
        provider_status = item.get("status") or "scheduled"
        incoming_scores = (item.get("home_score"), item.get("away_score"))
        existing_overrides = dict(game.overrides or {}) if game else {}
        provisional_confirmed = bool(existing_overrides.get("provisional_confirmed_by"))
        effective_status = (
            game.status
            if game and provider_status == "provisional" and provisional_confirmed
            else provider_status
        )
        provider_overrides = {
            "game_number": item.get("game_number"),
            "snapshot_id": snapshot.id,
            "provider_status": provider_status,
            "automation_blocked": provider_status in BLOCKING_PROVIDER_STATUSES
            and not (provider_status == "provisional" and provisional_confirmed),
        }
        merged_overrides = {**existing_overrides, **provider_overrides}
        values = {
            "home_team": item["home_team"],
            "away_team": item["away_team"],
            "kickoff": kickoff,
            "competition": item.get("competition"),
            "status": effective_status,
            "home_score": incoming_scores[0],
            "away_score": incoming_scores[1],
            "source_url": item.get("source_url") or snapshot.source_url,
            "checked_at": snapshot.fetched_at,
            "overrides": merged_overrides,
        }
        if game is None:
            game = Game(
                team_id=team.id,
                provider="fussball.de",
                external_id=external_id,
                result_confirmed=False,
                **values,
            )
            db.add(game)
            db.flush()
            created += 1
        else:
            old_kickoff = _utc(game.kickoff)
            old_scores = (game.home_score, game.away_score)
            relevant_before = (
                game.home_team,
                game.away_team,
                old_kickoff,
                existing_overrides.get("provider_status", game.status),
                *old_scores,
            )
            relevant_after = (
                values["home_team"],
                values["away_team"],
                kickoff,
                provider_status,
                *incoming_scores,
            )
            changed = any(_different(getattr(game, key), value) for key, value in values.items())
            if changed:
                kickoff_delta = kickoff - old_kickoff if old_kickoff != kickoff else None
                if kickoff_delta:
                    game.original_kickoff = game.original_kickoff or game.kickoff
                if old_scores != incoming_scores:
                    game.result_confirmed = False
                if relevant_before != relevant_after:
                    _invalidate_publications(db, game, kickoff_delta)
                for key, value in values.items():
                    setattr(game, key, value)
                game.version += 1
                updated += 1
            else:
                unchanged += 1
        ids.append(game.id)
    if not ids:
        raise SnapshotImportError("Snapshot enthält keine vollständig parsebaren Spiele")
    db.add(
        AuditLog(
            user_id=user.id,
            team_id=team.id,
            action="provider_snapshot.games_imported",
            entity_type="provider_snapshot",
            entity_id=snapshot.id,
            details={
                "created": created,
                "updated": updated,
                "unchanged": unchanged,
                "game_ids": ids,
                "posts_created": False,
            },
        )
    )
    db.commit()
    return {"created": created, "updated": updated, "unchanged": unchanged, "game_ids": ids}
