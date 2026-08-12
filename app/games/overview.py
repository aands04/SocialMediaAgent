"""Tenant-safe view models for the compact games overview."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Protocol
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy.orm import Session

from app.config import Settings
from app.games.automatic import (
    automatic_generation_candidates,
    automatic_generation_is_enabled,
)
from app.models import (
    Game,
    GenerationJob,
    GenerationJobStatus,
    Post,
    PostStatus,
    StoryRule,
    Team,
)

log = structlog.get_logger()

POST_TYPE_LABELS = {
    "announcement": "Spielankündigung",
    "reminder": "Spielerinnerung",
    "result": "Ergebnismeldung",
}

RUNNING_GENERATION_STATUSES = {
    GenerationJobStatus.QUEUED,
    GenerationJobStatus.RUNNING,
    GenerationJobStatus.RETRY_WAIT,
}
FAILED_GENERATION_STATUSES = {
    GenerationJobStatus.FAILED,
    GenerationJobStatus.MANUAL_REVIEW_REQUIRED,
}
APPROVAL_POST_STATUSES = {
    PostStatus.INCOMPLETE,
    PostStatus.PENDING,
    PostStatus.REAPPROVAL,
    PostStatus.REJECTED,
}


class PublicationLike(Protocol):
    scheduled_at: datetime
    channel: object
    job: object


@dataclass(frozen=True)
class GenerationScheduleView:
    game_id: str
    team_id: str
    team_name: str
    post_type: str
    post_type_label: str
    due_at: datetime | None
    state: str
    label: str
    detail: str | None
    tone: str
    action_required: bool = False


@dataclass(frozen=True)
class GameAutomationSummary:
    game_ids: tuple[str, ...]
    bundle_id: str | None
    contribution_status: str
    contribution_label: str
    contribution_tone: str
    automation_enabled: bool
    next_generation_type: str | None
    next_generation_at: datetime | None
    next_generation_label: str
    next_generation_detail: str | None
    generation_schedule_state: str
    generation_tone: str
    generation_items: tuple[GenerationScheduleView, ...]
    generation_count: int
    additional_generation_count: int
    distinct_generation_times: int
    next_publication_at: datetime | None
    next_publication_label: str
    active_channels: tuple[str, ...]
    publication_count: int
    action_required: bool
    action_type: str


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_overview_time(
    value: datetime,
    *,
    now: datetime,
    timezone_name: str,
) -> tuple[str, str | None]:
    """Return a short relative label and an optional absolute clarification."""

    zone = ZoneInfo(timezone_name)
    local_value = _aware_utc(value).astimezone(zone)
    local_now = _aware_utc(now).astimezone(zone)
    absolute = local_value.strftime("%d.%m.%Y · %H:%M Uhr")
    delta_days = (local_value.date() - local_now.date()).days
    if delta_days == 0:
        return f"Heute · {local_value:%H:%M} Uhr", absolute
    if delta_days == 1:
        return f"Morgen · {local_value:%H:%M} Uhr", absolute
    return absolute, None


def _latest_by_type(
    rows: Iterable, *, date_attr: str = "created_at"
) -> dict[tuple[str, str], object]:
    result: dict[tuple[str, str], object] = {}
    for row in rows:
        key = (row.game_id, row.post_type)
        current = result.get(key)
        row_date = getattr(row, date_attr, None) or datetime.min.replace(tzinfo=timezone.utc)
        current_date = (
            getattr(current, date_attr, None) or datetime.min.replace(tzinfo=timezone.utc)
            if current is not None
            else datetime.min.replace(tzinfo=timezone.utc)
        )
        if current is None or _aware_utc(row_date) >= _aware_utc(current_date):
            result[key] = row
    return result


def _schedule_view(
    *,
    game: Game,
    team: Team,
    post_type: str,
    due_at: datetime | None,
    event_based: bool,
    post: Post | None,
    job: GenerationJob | None,
    now: datetime,
    timezone_name: str,
) -> GenerationScheduleView:
    type_label = POST_TYPE_LABELS.get(post_type, "Beitrag")
    if job and job.status in RUNNING_GENERATION_STATUSES:
        return GenerationScheduleView(
            game.id,
            team.id,
            team.display_name,
            post_type,
            type_label,
            due_at,
            "running",
            "Wird gerade erstellt …",
            None,
            "neutral",
        )
    if post:
        created_at = post.created_at
        label, detail = format_overview_time(
            created_at,
            now=now,
            timezone_name=timezone_name,
        )
        return GenerationScheduleView(
            game.id,
            team.id,
            team.display_name,
            post_type,
            type_label,
            due_at,
            "created",
            f"Erstellt am {label}",
            detail,
            "success",
        )
    if job and job.status in FAILED_GENERATION_STATUSES:
        return GenerationScheduleView(
            game.id,
            team.id,
            team.display_name,
            post_type,
            type_label,
            due_at,
            "failed",
            "Erstellung fehlgeschlagen",
            "Bitte den Auftrag prüfen.",
            "danger",
            True,
        )
    if event_based:
        if game.result_confirmed:
            return GenerationScheduleView(
                game.id,
                team.id,
                team.display_name,
                post_type,
                type_label,
                None,
                "overdue",
                "Bestätigtes Ergebnis wartet auf Erstellung",
                None,
                "warning",
                True,
            )
        return GenerationScheduleView(
            game.id,
            team.id,
            team.display_name,
            post_type,
            type_label,
            None,
            "event",
            "Nach bestätigtem Endergebnis",
            None,
            "neutral",
        )
    if due_at is None:
        return GenerationScheduleView(
            game.id,
            team.id,
            team.display_name,
            post_type,
            type_label,
            None,
            "unavailable",
            "Automatisierungszeit derzeit nicht bestimmbar",
            None,
            "warning",
            True,
        )
    label, detail = format_overview_time(due_at, now=now, timezone_name=timezone_name)
    if _aware_utc(due_at) < _aware_utc(now):
        return GenerationScheduleView(
            game.id,
            team.id,
            team.display_name,
            post_type,
            type_label,
            due_at,
            "overdue",
            "Erstellung überfällig",
            f"Geplant war {label}",
            "warning",
            True,
        )
    return GenerationScheduleView(
        game.id,
        team.id,
        team.display_name,
        post_type,
        type_label,
        due_at,
        "planned",
        label,
        detail,
        "neutral",
    )


def build_game_automation_summary(
    db: Session,
    *,
    club_id: str,
    games: list[Game],
    teams: dict[str, Team],
    posts: list[Post],
    generation_jobs: list[GenerationJob],
    story_rules: list[StoryRule],
    publication_rows: list[PublicationLike],
    settings: Settings,
    bundle_id: str | None = None,
    now: datetime | None = None,
) -> GameAutomationSummary:
    """Build one compact group summary without issuing dependency queries."""

    if not club_id or any(game.club_id != club_id for game in games):
        raise ValueError("Eindeutiger Vereinskontext fehlt")
    if any(team.club_id != club_id for team in teams.values()):
        raise ValueError("Mannschaft gehört nicht zum aktuellen Verein")
    if any(post.club_id != club_id for post in posts) or any(
        job.club_id != club_id for job in generation_jobs
    ):
        raise ValueError("Beitragsdaten gehören nicht zum aktuellen Verein")

    now = _aware_utc(now or datetime.now(timezone.utc))
    timezone_name = settings.timezone
    latest_posts = _latest_by_type([post for post in posts if post.active_key == "active"])
    latest_jobs = _latest_by_type(generation_jobs)
    rules_by_team: dict[str, list[StoryRule]] = {}
    for rule in story_rules:
        if rule.club_id != club_id:
            raise ValueError("Story-Regel gehört nicht zum aktuellen Verein")
        rules_by_team.setdefault(rule.team_id, []).append(rule)

    items: list[GenerationScheduleView] = []
    automation_enabled = False
    schedule_errors = False
    for game in games:
        team = teams.get(game.team_id)
        if not team:
            continue
        automation_enabled = automation_enabled or automatic_generation_is_enabled(
            team,
            game,
            settings,
        )
        try:
            candidates = automatic_generation_candidates(
                db,
                team,
                game,
                settings,
                story_rules=rules_by_team.get(team.id, []),
            )
        except Exception as exc:
            schedule_errors = True
            log.warning(
                "games_overview_generation_schedule_unavailable",
                club_id=club_id,
                game_id=game.id,
                team_id=team.id,
                error_type=type(exc).__name__,
            )
            candidates = []

        candidate_types = {candidate.post_type for candidate in candidates}
        known_types = (
            candidate_types
            | {post_type for game_id, post_type in latest_posts if game_id == game.id}
            | {post_type for game_id, post_type in latest_jobs if game_id == game.id}
        )
        candidates_by_type = {candidate.post_type: candidate for candidate in candidates}
        for post_type in sorted(
            known_types,
            key=lambda value: (
                ("announcement", "reminder", "result").index(value)
                if value in {"announcement", "reminder", "result"}
                else 99
            ),
        ):
            candidate = candidates_by_type.get(post_type)
            items.append(
                _schedule_view(
                    game=game,
                    team=team,
                    post_type=post_type,
                    due_at=candidate.due_at if candidate else None,
                    event_based=bool(candidate and candidate.event_based),
                    post=latest_posts.get((game.id, post_type)),
                    job=latest_jobs.get((game.id, post_type)),
                    now=now,
                    timezone_name=timezone_name,
                )
            )

    active_jobs = [job for job in generation_jobs if job.status in RUNNING_GENERATION_STATUSES]
    active_posts = [post for post in posts if post.active_key == "active"]
    if active_jobs:
        contribution_status, contribution_label, contribution_tone = (
            "creating",
            "Wird erstellt",
            "neutral",
        )
    elif not active_posts:
        # The post itself does not exist yet, but its production can already be
        # scheduled.  Keep that distinction visible instead of presenting a
        # scheduler-owned item as an entirely manual gap.
        pending_states = {item.state for item in items}
        if "failed" in pending_states or "overdue" in pending_states:
            contribution_status, contribution_label, contribution_tone = (
                "problem",
                "Erstellung prüfen",
                "warning",
            )
        elif pending_states & {"planned", "event"}:
            contribution_status, contribution_label, contribution_tone = (
                "planned",
                "Automatisch geplant",
                "neutral",
            )
        elif automation_enabled:
            contribution_status, contribution_label, contribution_tone = (
                "manual",
                "Noch nicht erstellt",
                "warning",
            )
        else:
            contribution_status, contribution_label, contribution_tone = (
                "missing",
                "Noch nicht erstellt",
                "neutral",
            )
    elif any(post.status in APPROVAL_POST_STATUSES for post in active_posts):
        contribution_status, contribution_label, contribution_tone = (
            "attention",
            "Freigabe ausstehend",
            "warning",
        )
    elif active_posts and all(post.status == PostStatus.PUBLISHED for post in active_posts):
        contribution_status, contribution_label, contribution_tone = (
            "published",
            "Veröffentlicht",
            "success",
        )
    else:
        contribution_status, contribution_label, contribution_tone = (
            "created",
            "Erstellt",
            "success",
        )

    state_order = {
        "failed": 0,
        "running": 1,
        "overdue": 2,
        "planned": 3,
        "event": 4,
        "unavailable": 5,
        "created": 6,
    }
    primary = min(
        items,
        key=lambda item: (
            state_order.get(item.state, 99),
            _aware_utc(item.due_at) if item.due_at else datetime.max.replace(tzinfo=timezone.utc),
            item.team_name,
            item.post_type,
        ),
        default=None,
    )
    if primary:
        generation_state = primary.state
        generation_label = (
            f"{primary.post_type_label} · {primary.label}"
            if len(items) > 1 and primary.state in {"planned", "event"}
            else primary.label
        )
        generation_detail = primary.detail
        generation_tone = primary.tone
    elif schedule_errors:
        generation_state = "unavailable"
        generation_label = "Automatisierungszeit derzeit nicht bestimmbar"
        generation_detail = None
        generation_tone = "warning"
    elif automation_enabled:
        generation_state = "no_rule"
        generation_label = "Manuelle Erstellung erforderlich"
        generation_detail = "Für dieses Spiel ist kein automatischer Beitragstyp aktiviert."
        generation_tone = "warning"
    else:
        generation_state = "disabled"
        generation_label = "Nicht automatisch geplant"
        generation_detail = "Für dieses Spiel wird aktuell kein Beitrag automatisch erstellt."
        generation_tone = "neutral"

    non_terminal_publications = [
        row
        for row in publication_rows
        if getattr(row.job.status, "value", row.job.status)
        not in {"published", "cancelled", "skipped"}
    ]
    next_publication = min(
        non_terminal_publications,
        key=lambda row: _aware_utc(row.scheduled_at),
        default=None,
    )
    if next_publication:
        next_publication_label, _ = format_overview_time(
            next_publication.scheduled_at,
            now=now,
            timezone_name=timezone_name,
        )
    else:
        next_publication_label = "Noch nicht geplant"
    channels = tuple(
        dict.fromkeys(getattr(row.channel, "label", "Kanal") for row in publication_rows)
    )
    distinct_times = len({_aware_utc(item.due_at) for item in items if item.due_at})
    action_required = any(item.action_required for item in items) or any(
        bool(getattr(row, "attention", False)) for row in publication_rows
    )
    if any(item.state == "failed" for item in items):
        action_type = "problem"
    elif any(item.state == "overdue" for item in items):
        action_type = "overdue"
    elif contribution_status == "attention":
        action_type = "review"
    elif contribution_status in {"created", "published"}:
        action_type = "view"
    elif generation_state == "planned":
        action_type = "create_early"
    else:
        action_type = "create"

    return GameAutomationSummary(
        game_ids=tuple(game.id for game in games),
        bundle_id=bundle_id,
        contribution_status=contribution_status,
        contribution_label=contribution_label,
        contribution_tone=contribution_tone,
        automation_enabled=automation_enabled,
        next_generation_type=primary.post_type if primary else None,
        next_generation_at=primary.due_at if primary else None,
        next_generation_label=generation_label,
        next_generation_detail=generation_detail,
        generation_schedule_state=generation_state,
        generation_tone=generation_tone,
        generation_items=tuple(items),
        generation_count=len(items),
        additional_generation_count=max(0, len(items) - 1),
        distinct_generation_times=distinct_times,
        next_publication_at=(next_publication.scheduled_at if next_publication else None),
        next_publication_label=next_publication_label,
        active_channels=channels,
        publication_count=len(publication_rows),
        action_required=action_required,
        action_type=action_type,
    )
