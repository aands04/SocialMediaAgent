"""Tenant-bound view models for the operational publishing workspace.

The publishing domain stores one job per concrete destination.  This module
translates those technical records into the same user-facing vocabulary for
the dashboard, match list, contribution overview and contribution detail.
It deliberately receives an explicit ``club_id`` and never falls back to a
default tenant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.capabilities import CHANNEL_CAPABILITIES, CHANNEL_LABELS, status_label
from app.channels.service import ensure_instagram_channels
from app.models import (
    Game,
    JobStatus,
    Post,
    PublicationJob,
    PublicationMediaItem,
    SocialChannelConnection,
    Team,
)
from app.posts.club_carousel import is_redundant_matchday_bundle_feed

TERMINAL_JOB_STATUSES = {JobStatus.PUBLISHED, JobStatus.CANCELLED, JobStatus.SKIPPED}
ATTENTION_JOB_STATUSES = {
    JobStatus.DRAFT,
    JobStatus.UNAPPROVED,
    JobStatus.FAILED,
    JobStatus.UNCERTAIN,
}

CONTENT_LABELS = {
    "feed": "Feed-Beitrag",
    "carousel": "Karussell",
    "story": "Story",
    "page_post": "Facebook-Beitrag",
    "image_post": "Facebook-Bildbeitrag",
    "multi_image": "Facebook-Beitrag mit mehreren Bildern",
    "template_message": "WhatsApp-Nachricht",
    "direct_message": "WhatsApp-Nachricht",
}

POST_TYPE_LABELS = {
    "announcement": "Spielankündigung",
    "reminder": "Spielerinnerung",
    "result": "Ergebnismeldung",
    "manual": "Manueller Beitrag",
}

APPROVAL_LABELS = {
    "approved": "Freigegeben",
    "unapproved": "Nicht freigegeben",
    "reapproval_required": "Erneute Freigabe erforderlich",
    "rejected": "Abgelehnt",
    "bundle_wait": "Wartet auf gemeinsamen Spieltag",
    "manual_schedule_required": "Veröffentlichungszeit festlegen",
}

JOB_STATUS_LABELS = {
    JobStatus.DRAFT: "Entwurf",
    JobStatus.UNAPPROVED: "Nicht freigegeben",
    JobStatus.APPROVED: "Freigegeben",
    JobStatus.SCHEDULED: "Geplant",
    JobStatus.WAITING: "Wartet auf Voraussetzung",
    JobStatus.PUBLISHING: "Wird verarbeitet",
    JobStatus.PUBLISHED: "Veröffentlicht",
    JobStatus.RETRY: "Wiederholung geplant",
    JobStatus.FAILED: "Fehlgeschlagen",
    JobStatus.CANCELLED: "Abgebrochen",
    JobStatus.SKIPPED: "Übersprungen",
    JobStatus.UNCERTAIN: "Prüfung erforderlich",
}


@dataclass(frozen=True, slots=True)
class OperationalChannel:
    connection_id: str
    channel_type: str
    label: str
    display_name: str
    concrete_target: str
    connection_status: str
    connection_status_label: str
    active: bool
    publishing_enabled: bool
    automatic_delivery_enabled: bool
    supported_content_types: tuple[str, ...]
    legacy_instagram_page_id: str | None

    @property
    def ready(self) -> bool:
        return self.active and self.publishing_enabled and self.connection_status == "connected"


@dataclass(frozen=True, slots=True)
class PublicationView:
    job: PublicationJob
    post: Post | None
    game: Game | None
    team: Team | None
    channel: OperationalChannel
    target: str
    content_label: str
    contribution_label: str
    title: str
    subtitle: str
    status_label: str
    status_detail: str | None
    status_tone: str
    approval_label: str
    attention: bool
    overdue: bool
    action_label: str
    media_items: tuple[PublicationMediaItem, ...]

    @property
    def delivery_label(self) -> str:
        return "Versand" if self.job.delivery_action == "send" else "Veröffentlichung"

    @property
    def event_at(self) -> datetime:
        value = self.job.published_at or self.job.scheduled_at
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    @property
    def scheduled_at(self) -> datetime:
        value = self.job.scheduled_at
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _connection_target(connection: SocialChannelConnection) -> str:
    if connection.channel_type == "instagram" and connection.username:
        return f"@{connection.username.lstrip('@')}"
    if connection.channel_type == "whatsapp":
        return connection.display_name or connection.display_phone_number or "WhatsApp-Empfänger"
    return connection.display_name or "Eingerichtetes Ziel"


def operational_channels(db: Session, club_id: str) -> list[OperationalChannel]:
    """Return only channels that have a tenant-owned configuration record.

    An account remains operationally visible while it is paused or disrupted,
    so users can understand why an existing job cannot run.  Empty setup rows
    without an account identity are not part of publishing filters.
    """

    if not club_id:
        raise ValueError("Eindeutiger Vereinskontext fehlt")
    ensure_instagram_channels(db)
    rows = list(
        db.scalars(
            select(SocialChannelConnection)
            .where(SocialChannelConnection.club_id == club_id)
            .order_by(
                SocialChannelConnection.channel_type,
                SocialChannelConnection.display_name,
            )
        )
    )
    result = []
    for connection in rows:
        configured = bool(
            connection.active
            or connection.external_account_id
            or connection.legacy_instagram_page_id
            or connection.phone_number_id
        )
        if not configured:
            continue
        capabilities = tuple(
            capability.key
            for capability in CHANNEL_CAPABILITIES.get(connection.channel_type, ())
            if capability.key in set(connection.capabilities or ())
            or connection.channel_type == "instagram"
        )
        result.append(
            OperationalChannel(
                connection_id=connection.id,
                channel_type=connection.channel_type,
                label=CHANNEL_LABELS.get(connection.channel_type, "Kanal"),
                display_name=connection.display_name,
                concrete_target=_connection_target(connection),
                connection_status=connection.status,
                connection_status_label=status_label(connection.status),
                active=connection.active,
                publishing_enabled=connection.publishing_enabled,
                automatic_delivery_enabled=connection.automatic_delivery_enabled,
                supported_content_types=capabilities,
                legacy_instagram_page_id=connection.legacy_instagram_page_id,
            )
        )
    return result


def _channel_maps(channels: Iterable[OperationalChannel]):
    by_id: dict[str, OperationalChannel] = {}
    by_page: dict[str, OperationalChannel] = {}
    by_type: dict[str, list[OperationalChannel]] = {}
    for channel in channels:
        by_id[channel.connection_id] = channel
        if channel.legacy_instagram_page_id:
            by_page[channel.legacy_instagram_page_id] = channel
        by_type.setdefault(channel.channel_type, []).append(channel)
    return by_id, by_page, by_type


def channel_for_job(
    job: PublicationJob,
    channels: Iterable[OperationalChannel],
) -> OperationalChannel | None:
    by_id, by_page, by_type = _channel_maps(channels)
    if job.channel_connection_id:
        return by_id.get(job.channel_connection_id)
    if job.instagram_page_id:
        return by_page.get(job.instagram_page_id)
    candidates = by_type.get(job.channel_type, [])
    return candidates[0] if len(candidates) == 1 else None


def _status_view(
    job: PublicationJob,
    channel: OperationalChannel,
    now: datetime,
) -> tuple[str, str | None, str, bool, bool]:
    scheduled = job.scheduled_at
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=timezone.utc)
    overdue = scheduled < now and job.status not in TERMINAL_JOB_STATUSES
    approval_label = APPROVAL_LABELS.get(job.approval_status, "Freigabe prüfen")

    if job.status == JobStatus.PUBLISHED:
        return "Veröffentlicht", None, "success", False, False
    if job.status == JobStatus.FAILED:
        return (
            "Fehlgeschlagen",
            (job.error or "Technischen Fehler prüfen")[:180],
            "danger",
            True,
            overdue,
        )
    if job.status == JobStatus.UNCERTAIN:
        return (
            "Prüfung erforderlich",
            (job.error or "Plattformstatus ist unklar")[:180],
            "danger",
            True,
            overdue,
        )
    if job.stale_time:
        return (
            "Zeitpunkt prüfen",
            "Der geplante Zeitpunkt ist nicht mehr aktuell.",
            "warning",
            True,
            overdue,
        )
    if job.approval_status != "approved":
        return (
            approval_label,
            "Vor der Ausführung ist eine Freigabe erforderlich.",
            "warning",
            True,
            overdue,
        )
    if overdue:
        return "Überfällig", "Der Auftrag wurde noch nicht abgeschlossen.", "warning", True, True
    if channel.connection_status != "connected":
        return "Kanal prüfen", channel.connection_status_label, "warning", True, overdue
    if not channel.active or not channel.publishing_enabled:
        return (
            "Kanal pausiert",
            "Die automatische Ausführung ist für dieses Ziel deaktiviert.",
            "warning",
            True,
            overdue,
        )
    if job.status == JobStatus.WAITING:
        return (
            "Wartet auf Voraussetzung",
            (job.error or "Ein vorheriger Schritt ist noch offen")[:180],
            "warning",
            True,
            False,
        )
    if job.status == JobStatus.RETRY:
        return (
            "Wiederholung geplant",
            (job.error or "Der nächste Versuch wird automatisch gestartet")[:180],
            "warning",
            True,
            False,
        )
    label = JOB_STATUS_LABELS.get(job.status, "Status prüfen")
    tone = "success" if job.status == JobStatus.PUBLISHED else "neutral"
    return label, None, tone, job.status in ATTENTION_JOB_STATUSES, False


def publication_views(
    db: Session,
    jobs: Iterable[PublicationJob],
    *,
    club_id: str,
    channels: list[OperationalChannel] | None = None,
    now: datetime | None = None,
) -> list[PublicationView]:
    """Bulk-load all display dependencies and hide non-configured channels."""

    if not club_id:
        raise ValueError("Eindeutiger Vereinskontext fehlt")
    job_rows = [job for job in jobs if job.club_id == club_id]
    if not job_rows:
        return []
    channels = channels if channels is not None else operational_channels(db, club_id)
    now = now or datetime.now(timezone.utc)
    post_ids = {job.post_id for job in job_rows}
    game_ids = {job.game_id for job in job_rows if job.game_id}
    team_ids = {job.team_id for job in job_rows}
    job_ids = {job.id for job in job_rows}
    posts = {
        row.id: row
        for row in db.scalars(select(Post).where(Post.club_id == club_id, Post.id.in_(post_ids)))
    }
    games = (
        {
            row.id: row
            for row in db.scalars(
                select(Game).where(Game.club_id == club_id, Game.id.in_(game_ids))
            )
        }
        if game_ids
        else {}
    )
    teams = {
        row.id: row
        for row in db.scalars(select(Team).where(Team.club_id == club_id, Team.id.in_(team_ids)))
    }
    media: dict[str, list[PublicationMediaItem]] = {job_id: [] for job_id in job_ids}
    for row in db.scalars(
        select(PublicationMediaItem)
        .where(
            PublicationMediaItem.club_id == club_id,
            PublicationMediaItem.publication_job_id.in_(job_ids),
        )
        .order_by(PublicationMediaItem.publication_job_id, PublicationMediaItem.position)
    ):
        media[row.publication_job_id].append(row)

    result = []
    by_id, by_page, by_type = _channel_maps(channels)
    for job in job_rows:
        if job.channel_connection_id:
            channel = by_id.get(job.channel_connection_id)
        elif job.instagram_page_id:
            channel = by_page.get(job.instagram_page_id)
        else:
            candidates = by_type.get(job.channel_type, [])
            channel = candidates[0] if len(candidates) == 1 else None
        if channel is None:
            continue
        post = posts.get(job.post_id)
        if post and is_redundant_matchday_bundle_feed(post, job):
            continue
        game = games.get(job.game_id) if job.game_id else None
        team = teams.get(job.team_id)
        status, detail, tone, attention, overdue = _status_view(job, channel, now)
        if game:
            title = f"{game.home_team} – {game.away_team}"
            game_context = game.competition or POST_TYPE_LABELS.get(
                post.post_type if post else "", "Spielbeitrag"
            )
            text_preview = (post.text or "").strip()[:140] if post else ""
            subtitle = f"{game_context} · {text_preview}" if text_preview else game_context
        else:
            title = POST_TYPE_LABELS.get(post.post_type if post else "manual", "Beitrag")
            subtitle = (post.text or "")[:140] if post else ""
        kind = job.content_type or job.kind
        result.append(
            PublicationView(
                job=job,
                post=post,
                game=game,
                team=team,
                channel=channel,
                target=job.target or channel.concrete_target,
                content_label=CONTENT_LABELS.get(kind, CONTENT_LABELS.get(job.kind, "Beitrag")),
                contribution_label=POST_TYPE_LABELS.get(post.post_type if post else "", "Beitrag"),
                title=title,
                subtitle=subtitle,
                status_label=status,
                status_detail=detail,
                status_tone=tone,
                approval_label=APPROVAL_LABELS.get(job.approval_status, "Freigabe prüfen"),
                attention=attention,
                overdue=overdue,
                action_label="Beitrag prüfen" if attention else "Beitrag öffnen",
                media_items=tuple(media.get(job.id, ())),
            )
        )
    return result


def group_views_by_channel(
    views: Iterable[PublicationView],
) -> list[tuple[OperationalChannel, list[PublicationView]]]:
    grouped: dict[str, tuple[OperationalChannel, list[PublicationView]]] = {}
    for view in views:
        grouped.setdefault(view.channel.connection_id, (view.channel, []))[1].append(view)
    return list(grouped.values())
