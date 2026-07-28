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

BLOCKED_STATUSES = {"cancelled", "postponed", "provisional"}
RELEVANT_FIELDS = {"kickoff", "home_team", "away_team", "status", "home_score", "away_score"}


class SnapshotImportError(ValueError):
    pass


def preview_snapshot(snapshot: ProviderSnapshot) -> list[dict]:
    games = snapshot.parser_result.get("games", []) if snapshot.parser_result else []
    return [game for game in games if all(game.get(key) for key in ("external_id", "home_team", "away_team", "kickoff"))]


def utc(value: datetime) -> datetime:
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).astimezone(timezone.utc)


def invalidate_publications(db: Session, game: Game, changed_fields: set[str], old_kickoff: datetime) -> None:
    """Withdraw approvals and adjust schedules before changed game data is persisted."""
    if not changed_fields & RELEVANT_FIELDS:
        return
    kickoff_changed = "kickoff" in changed_fields
    delta = utc(game.kickoff) - utc(old_kickoff) if kickoff_changed else None
    posts = list(db.scalars(select(Post).where(Post.game_id == game.id).with_for_update()))
    for post in posts:
        if post.status in {PostStatus.APPROVED, PostStatus.SCHEDULED, PostStatus.PARTIAL}:
            post.status = PostStatus.REAPPROVAL
            post.version += 1
        for job in db.scalars(select(PublicationJob).where(PublicationJob.post_id == post.id, PublicationJob.status != JobStatus.PUBLISHED).with_for_update()):
            if kickoff_changed:
                if job.absolute_time:
                    job.stale_time = True
                else:
                    job.scheduled_at = utc(job.scheduled_at) + delta
            job.approval_status = "reapproval_required"
            job.status = JobStatus.UNAPPROVED
            job.error = "Spieldaten wurden nach Freigabe geändert"


def import_snapshot(db: Session, snapshot: ProviderSnapshot, user: User) -> dict:
    team = db.get(Team, snapshot.team_id)
    if not team:
        raise SnapshotImportError("Snapshot hat keine gültige Mannschaft")
    if snapshot.error:
        raise SnapshotImportError("Snapshot enthält einen Parserfehler")
    created = updated = unchanged = 0
    ids: list[str] = []
    changes: dict[str, list[str]] = {}
    for item in preview_snapshot(snapshot):
        kickoff = datetime.fromisoformat(item["kickoff"])
        if kickoff.tzinfo is None:
            raise SnapshotImportError("Anpfiff ohne Zeitzone wird nicht übernommen")
        kickoff = utc(kickoff)
        external_id = item["external_id"]
        game = db.scalar(select(Game).where(Game.team_id == team.id, Game.provider == "fussball.de", Game.external_id == external_id).with_for_update())
        incoming_status = item.get("status") or "scheduled"
        old_overrides = dict(game.overrides or {}) if game else {}
        manually_confirmed = bool(old_overrides.get("provisional_confirmed_by"))
        effective_status = game.status if game and incoming_status == "provisional" and manually_confirmed else incoming_status
        provider_overrides = {"game_number": item.get("game_number"), "snapshot_id": snapshot.id, "provider_status": incoming_status, "automation_blocked": effective_status in BLOCKED_STATUSES}
        merged_overrides = {**old_overrides, **provider_overrides}
        scores_changed = bool(game and (game.home_score != item.get("home_score") or game.away_score != item.get("away_score")))
        values = {"home_team": item["home_team"], "away_team": item["away_team"], "kickoff": kickoff, "competition": item.get("competition"), "status": effective_status, "home_score": item.get("home_score"), "away_score": item.get("away_score"), "source_url": item.get("source_url") or snapshot.source_url, "checked_at": snapshot.fetched_at, "result_confirmed": game.result_confirmed if game and not scores_changed else False, "overrides": merged_overrides}
        if game is None:
            game = Game(team_id=team.id, provider="fussball.de", external_id=external_id, **values)
            db.add(game)
            db.flush()
            created += 1
        else:
            changed_fields = {key for key, value in values.items() if key != "overrides" and (utc(getattr(game, key)) if isinstance(getattr(game, key), datetime) else getattr(game, key)) != (utc(value) if isinstance(value, datetime) else value)}
            if game.overrides != merged_overrides:
                changed_fields.add("overrides")
            if changed_fields:
                old_kickoff = game.kickoff
                if "kickoff" in changed_fields:
                    game.original_kickoff = game.original_kickoff or old_kickoff
                game.kickoff = kickoff
                invalidate_publications(db, game, changed_fields, old_kickoff)
                for key, value in values.items():
                    setattr(game, key, value)
                game.version += 1
                updated += 1
                changes[external_id] = sorted(changed_fields)
            else:
                unchanged += 1
        ids.append(game.id)
    if not ids:
        raise SnapshotImportError("Snapshot enthält keine vollständig parsebaren Spiele")
    db.add(AuditLog(user_id=user.id, team_id=team.id, action="provider_snapshot.games_imported", entity_type="provider_snapshot", entity_id=snapshot.id, details={"created":created, "updated":updated, "unchanged":unchanged, "game_ids":ids, "changed_fields":changes, "posts_created":False}))
    db.commit()
    return {"created":created, "updated":updated, "unchanged":unchanged, "game_ids":ids}
