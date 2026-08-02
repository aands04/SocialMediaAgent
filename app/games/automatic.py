"""Persistent, idempotent FUSSBALL.DE synchronization and draft planning."""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from socket import gethostname

import structlog
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.games.importer import import_snapshot
from app.games.live_test import serialize
from app.games.provider import FussballDeProvider, ProviderError
from app.jobs.generation import enqueue_create
from app.models import (
    AuditLog,
    FussballSyncState,
    Game,
    GenerationJob,
    Notification,
    Post,
    ProviderSnapshot,
    Role,
    StoryRule,
    Team,
    User,
)
from app.posts.service import story_time

log = structlog.get_logger()


@dataclass
class AutomaticFussballResult:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    created_games: int = 0
    updated_games: int = 0
    results_confirmed: int = 0
    generation_jobs: int = 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _ensure_states(db: Session, now: datetime) -> None:
    existing = set(db.scalars(select(FussballSyncState.team_id)).all())
    for team_id in db.scalars(select(Team.id).where(Team.active.is_(True))):
        if team_id not in existing:
            db.add(FussballSyncState(team_id=team_id, next_poll_at=now))
    try:
        db.commit()
    except IntegrityError:
        # Another worker may have inserted the same one-row-per-team state.
        db.rollback()


def claim_due_team(
    db: Session,
    settings: Settings,
    *,
    worker_id: str | None = None,
    now: datetime | None = None,
) -> str | None:
    now = now or _now()
    worker_id = worker_id or f"{gethostname()}-fussball"
    _ensure_states(db, now)
    query = (
        select(FussballSyncState)
        .where(
            FussballSyncState.next_poll_at <= now,
            or_(
                FussballSyncState.lease_expires_at.is_(None),
                FussballSyncState.lease_expires_at <= now,
            ),
        )
        .order_by(FussballSyncState.next_poll_at, FussballSyncState.team_id)
        .limit(max(10, settings.fussball_sync_batch_size * 5))
    )
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    for state in db.scalars(query):
        team = db.get(Team, state.team_id)
        enabled = bool(
            team
            and team.active
            and team.fussball_url
            and (team.rules or {}).get("automatic_sync_enabled")
        )
        if not enabled:
            state.status = "disabled"
            state.next_poll_at = now + timedelta(hours=24)
            state.lease_owner = None
            state.lease_expires_at = None
            continue
        state.status = "running"
        state.last_started_at = now
        state.lease_owner = worker_id
        state.lease_expires_at = now + timedelta(
            seconds=settings.fussball_sync_lease_seconds
        )
        db.commit()
        return state.team_id
    db.commit()
    return None


def _capture_automatic_snapshot(
    db: Session, team: Team, settings: Settings
) -> ProviderSnapshot:
    provider = FussballDeProvider(
        timeout=15,
        max_attempts=2,
        decode_obfuscated_results=settings.fussball_decode_obfuscated_results,
    )
    html = provider.fetch_html(team.fussball_url)
    payload = html.encode("utf-8")
    checksum = hashlib.sha256(payload).hexdigest()
    fetched_at = _now()
    records = provider.parse(html)
    related_html: list[tuple[str, str, str]] = []
    related_warnings: list[str] = []
    previous_url = provider.ajax_resource(html, "prev")
    if previous_url:
        try:
            previous_html = provider.fetch_html(previous_url, ajax_only=True)
            previous_records = provider.parse(previous_html)
        except ProviderError as exc:
            related_warnings.append(
                f"Letzte Spiele konnten nicht gelesen werden: {exc}"
            )
        else:
            related_html.append(("previous", previous_url, previous_html))
            merged = {record.external_id: record for record in records}
            for record in previous_records:
                current = merged.get(record.external_id)
                if current is None or (
                    record.home_score is not None
                    and record.away_score is not None
                    and (current.home_score is None or current.away_score is None)
                ):
                    merged[record.external_id] = record
            records = list(merged.values())

    existing = {
        game.external_id: game
        for game in db.scalars(
            select(Game).where(Game.team_id == team.id, Game.provider == "fussball.de")
        )
    }
    needs_detail = [
        record
        for record in records
        if not existing.get(record.external_id)
        or not existing[record.external_id].venue
        or not existing[record.external_id].pitch
    ]
    enriched = {
        record.external_id: record
        for record in provider.enrich_game_details(needs_detail, delay_seconds=0.1)
    }
    records = [enriched.get(record.external_id, record) for record in records]

    root = settings.provider_snapshot_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    relative = Path(team.id) / (
        f"{fetched_at.strftime('%Y%m%dT%H%M%S%fZ')}-{checksum[:12]}.html"
    )
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ProviderError("Ungültiger Snapshot-Pfad")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    related_sources = []
    for label, source_url, related_payload in related_html:
        related_relative = relative.with_name(f"{relative.stem}-{label}.html")
        related_target = (root / related_relative).resolve()
        if not related_target.is_relative_to(root):
            raise ProviderError("Ungültiger Snapshot-Pfad")
        related_target.write_text(related_payload, encoding="utf-8")
        related_sources.append(
            {
                "kind": label,
                "source_url": source_url,
                "relative_path": str(related_relative),
                "checksum": hashlib.sha256(
                    related_payload.encode("utf-8")
                ).hexdigest(),
            }
        )
    parsed = [serialize(record) for record in records]
    warnings = sorted(
        {warning for record in records for warning in record.warnings}
        | set(related_warnings)
    )
    snapshot = ProviderSnapshot(
        team_id=team.id,
        source_url=team.fussball_url,
        fetched_at=fetched_at,
        status_code=200,
        checksum=checksum,
        relative_path=str(relative),
        parser_result={
            "team_name": team.display_name,
            "games": parsed,
            "parser_warnings": warnings,
            "related_sources": related_sources,
            "read_only": False,
            "automatic": True,
        },
    )
    db.add(snapshot)
    db.commit()
    return snapshot


def _observe_results(
    db: Session,
    team: Team,
    snapshot: ProviderSnapshot,
    settings: Settings,
) -> int:
    confirmed = 0
    now = _utc(snapshot.fetched_at)
    for item in (snapshot.parser_result or {}).get("games", []):
        game = db.scalar(
            select(Game).where(
                Game.team_id == team.id,
                Game.provider == "fussball.de",
                Game.external_id == item.get("external_id"),
            )
        )
        if not game or (game.overrides or {}).get("import_suppressed"):
            continue
        if now > _utc(game.kickoff) + timedelta(
            hours=settings.fussball_result_max_age_hours
        ):
            continue
        home_score = item.get("home_score")
        away_score = item.get("away_score")
        overrides = dict(game.overrides or {})
        if home_score is None or away_score is None:
            old_enough = now >= _utc(game.kickoff) + timedelta(
                minutes=settings.fussball_result_min_age_minutes
            )
            obfuscated = any(
                "Symbolschrift" in warning for warning in item.get("warnings", [])
            )
            if old_enough and obfuscated and not overrides.get(
                "result_review_notified_at"
            ):
                overrides["result_review_notified_at"] = now.isoformat()
                game.overrides = overrides
                db.add(
                    Notification(
                        team_id=team.id,
                        kind="fussball_result_manual_review",
                        message=(
                            f"Ergebnis für {game.home_team} – {game.away_team} "
                            "konnte nicht sicher gelesen werden."
                        ),
                    )
                )
            continue

        candidate = f"{int(home_score)}:{int(away_score)}"
        previous = overrides.get("provider_score_candidate")
        if previous != candidate:
            overrides["provider_score_candidate"] = candidate
            overrides["provider_score_first_seen_at"] = now.isoformat()
            overrides["provider_score_observations"] = 1
            overrides["result_detected_at"] = now.isoformat()
            game.result_confirmed = False
        elif overrides.get("provider_score_last_snapshot_id") != snapshot.id:
            overrides["provider_score_observations"] = int(
                overrides.get("provider_score_observations", 1)
            ) + 1
        overrides["provider_score_last_snapshot_id"] = snapshot.id
        overrides["provider_score_last_seen_at"] = now.isoformat()
        overrides.setdefault("result_detected_at", now.isoformat())
        game.overrides = overrides

        first_seen = datetime.fromisoformat(overrides["provider_score_first_seen_at"])
        stable = (now - _utc(first_seen)).total_seconds() >= (
            settings.fussball_result_stability_seconds
        )
        match_old_enough = now >= _utc(game.kickoff) + timedelta(
            minutes=settings.fussball_result_min_age_minutes
        )
        observations = int(overrides.get("provider_score_observations", 0))
        if (
            not game.result_confirmed
            and observations >= 2
            and stable
            and match_old_enough
            and game.status not in {"cancelled", "postponed"}
        ):
            game.result_confirmed = True
            game.status = "finished"
            overrides["result_confirmed_at"] = now.isoformat()
            overrides["result_confirmation_source"] = "fussball.de_stable"
            game.overrides = overrides
            db.add(
                AuditLog(
                    user_id=None,
                    team_id=team.id,
                    action="provider_result.confirmed_automatically",
                    entity_type="game",
                    entity_id=game.id,
                    details={
                        "score": candidate,
                        "observations": observations,
                        "first_seen_at": overrides["provider_score_first_seen_at"],
                        "snapshot_id": snapshot.id,
                    },
                )
            )
            confirmed += 1
    db.commit()
    return confirmed


def _automatic_actor(db: Session) -> User | None:
    return db.scalar(
        select(User)
        .where(
            User.active.is_(True),
            User.archived_at.is_(None),
            User.role == Role.ADMIN,
            User.all_teams.is_(True),
        )
        .order_by(User.email)
    )


def _earliest_publication(
    db: Session, team: Team, game: Game, post_type: str
) -> datetime:
    if post_type == "result":
        detected = (game.overrides or {}).get("result_detected_at")
        base = datetime.fromisoformat(detected) if detected else game.checked_at
        feed_at = _utc(base) + timedelta(
            minutes=int((team.rules or {}).get("result_wait_minutes", 15))
        )
    elif post_type == "reminder":
        feed_at = _utc(game.kickoff) - timedelta(
            minutes=int((team.rules or {}).get("reminder_feed_before_minutes", 360))
        )
    else:
        feed_at = _utc(game.kickoff) - timedelta(
            minutes=int((team.rules or {}).get("feed_before_minutes", 1440))
        )
    times = [feed_at]
    for rule in db.scalars(
        select(StoryRule).where(
            StoryRule.team_id == team.id,
            StoryRule.post_type == post_type,
            StoryRule.active.is_(True),
        )
    ):
        times.append(_utc(story_time(rule, game)))
    return min(times)


def plan_generation_jobs(
    db: Session, team: Team, settings: Settings, *, now: datetime | None = None
) -> int:
    if not settings.automatic_post_generation_enabled or not (
        team.rules or {}
    ).get("automatic_generation_enabled"):
        return 0
    actor = _automatic_actor(db)
    if not actor:
        return 0
    now = now or _now()
    lead = timedelta(
        minutes=int((team.rules or {}).get("generation_lead_minutes", 120))
    )
    queued = 0
    games = db.scalars(
        select(Game).where(
            Game.team_id == team.id,
            Game.provider == "fussball.de",
            Game.status.not_in(["cancelled", "postponed"]),
        )
    ).all()
    for game in games:
        if (game.overrides or {}).get("automation_blocked") or (
            game.overrides or {}
        ).get("import_suppressed"):
            continue
        post_types: list[str] = []
        if (
            (team.rules or {}).get("announcement_enabled")
            and now <= _utc(game.kickoff)
            and now >= _earliest_publication(db, team, game, "announcement") - lead
        ):
            post_types.append("announcement")
        if (
            (team.rules or {}).get("reminder_enabled")
            and now <= _utc(game.kickoff)
            and now >= _earliest_publication(db, team, game, "reminder") - lead
        ):
            post_types.append("reminder")
        if (
            (team.rules or {}).get("result_enabled")
            and game.result_confirmed
            and now
            <= _utc(game.kickoff)
            + timedelta(hours=settings.fussball_result_max_age_hours)
            and now >= _earliest_publication(db, team, game, "result") - lead
        ):
            post_types.append("result")
        for post_type in post_types:
            if db.scalar(
                select(Post.id).where(
                    Post.game_id == game.id,
                    Post.post_type == post_type,
                    Post.active_key == "active",
                )
            ):
                continue
            existing_job = db.scalar(
                select(GenerationJob.id).where(
                    GenerationJob.idempotency_key
                    == f"create:{game.id}:{post_type}"
                )
            )
            if existing_job:
                continue
            job, _ = enqueue_create(db, game, team, actor, post_type)
            if job:
                job.parameters = {
                    **(job.parameters or {}),
                    "trigger_mode": "automatic_fussball",
                    "provider_sync_at": now.isoformat(),
                }
                db.add(
                    AuditLog(
                        user_id=None,
                        team_id=team.id,
                        action="generation.queued_automatically",
                        entity_type="generation_job",
                        entity_id=job.id,
                        details={"game_id": game.id, "post_type": post_type},
                    )
                )
                db.commit()
                queued += 1
    return queued


def _next_interval(db: Session, team_id: str, settings: Settings, now: datetime) -> int:
    nearby = db.scalar(
        select(Game.id).where(
            Game.team_id == team_id,
            Game.kickoff >= now - timedelta(hours=5),
            Game.kickoff <= now + timedelta(hours=2),
        )
    )
    return (
        settings.fussball_result_poll_interval_seconds
        if nearby
        else settings.fussball_sync_interval_seconds
    )


def process_claimed_team(
    db: Session, team_id: str, settings: Settings
) -> dict[str, int]:
    state = db.get(FussballSyncState, team_id)
    team = db.get(Team, team_id)
    if not state or state.status != "running" or not team:
        raise ValueError("FUSSBALL.DE-Synchronisation wurde nicht beansprucht")
    try:
        snapshot = _capture_automatic_snapshot(db, team, settings)
        imported = import_snapshot(db, snapshot, None)
        confirmed = _observe_results(db, team, snapshot, settings)
        generated = plan_generation_jobs(db, team, settings)
        now = _now()
        state = db.get(FussballSyncState, team_id)
        state.status = "idle"
        state.next_poll_at = now + timedelta(
            seconds=_next_interval(db, team.id, settings, now)
        )
        state.lease_owner = None
        state.lease_expires_at = None
        state.last_completed_at = now
        state.last_success_at = now
        state.last_snapshot_id = snapshot.id
        state.last_result_scan_at = now
        state.consecutive_failures = 0
        state.last_error = None
        team.last_sync_at = now
        team.last_error = None
        db.commit()
        return {
            "created": imported["created"],
            "updated": imported["updated"],
            "confirmed": confirmed,
            "generated": generated,
        }
    except Exception as exc:
        db.rollback()
        now = _now()
        state = db.get(FussballSyncState, team_id)
        team = db.get(Team, team_id)
        if state:
            state.status = "error"
            state.consecutive_failures += 1
            delay = min(
                settings.fussball_sync_interval_seconds,
                settings.fussball_sync_error_backoff_seconds
                * (2 ** min(5, state.consecutive_failures - 1)),
            )
            state.next_poll_at = now + timedelta(seconds=delay)
            state.lease_owner = None
            state.lease_expires_at = None
            state.last_completed_at = now
            state.last_error = str(exc)[:2000]
        if team:
            team.last_error_at = now
            team.last_error = str(exc)[:2000]
        db.add(
            AuditLog(
                user_id=None,
                team_id=team_id,
                action="provider_sync.failed",
                entity_type="team",
                entity_id=team_id,
                details={"error": str(exc)[:500]},
            )
        )
        db.commit()
        raise


def run_automatic_fussball_cycle(
    db: Session, settings: Settings
) -> AutomaticFussballResult:
    result = AutomaticFussballResult()
    for _ in range(settings.fussball_sync_batch_size):
        team_id = claim_due_team(db, settings)
        if not team_id:
            break
        result.claimed += 1
        try:
            item = process_claimed_team(db, team_id, settings)
        except Exception as exc:
            result.failed += 1
            log.warning("automatic_fussball_sync_failed", team_id=team_id, error=str(exc))
            continue
        result.succeeded += 1
        result.created_games += item["created"]
        result.updated_games += item["updated"]
        result.results_confirmed += item["confirmed"]
        result.generation_jobs += item["generated"]
    return result
