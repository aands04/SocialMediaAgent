from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.channels.api import ChannelApiError, MetaGraphClient
from app.config import Settings
from app.meta.media import create_grant, publication_media_items, revoke_grant
from app.meta.security import TokenCipher, sanitize_platform_data
from app.models import (
    AuditLog,
    ChannelDeliveryAttempt,
    Club,
    ClubStatus,
    Game,
    JobStatus,
    Post,
    PostStatus,
    PublicationJob,
    SocialChannelConnection,
    SystemSetting,
    Team,
    User,
    WhatsAppMessageTemplate,
    WhatsAppRecipient,
)
from app.tenancy.state import system_scope, tenant_scope

log = structlog.get_logger()


class ChannelDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    platform_id: str
    response: dict


@dataclass
class ChannelCycleResult:
    queued: int = 0
    delivered: int = 0
    failed: int = 0
    uncertain: int = 0


class SocialChannelProvider(ABC):
    channel_type: str
    action: str

    def __init__(self, settings: Settings, api: MetaGraphClient | None = None):
        self.settings = settings
        self.api = api or MetaGraphClient(settings)

    @abstractmethod
    def deliver(
        self,
        *,
        connection: SocialChannelConnection,
        token: str,
        job: PublicationJob,
        media_urls: list[str],
        recipient: WhatsAppRecipient | None = None,
        template: WhatsAppMessageTemplate | None = None,
    ) -> DeliveryResult: ...


class FacebookPageChannelProvider(SocialChannelProvider):
    channel_type = "facebook"
    action = "publish"

    def deliver(
        self,
        *,
        connection: SocialChannelConnection,
        token: str,
        job: PublicationJob,
        media_urls: list[str],
        recipient: WhatsAppRecipient | None = None,
        template: WhatsAppMessageTemplate | None = None,
    ) -> DeliveryResult:
        response = self.api.publish_page_post(
            page_id=connection.external_account_id or "",
            access_token=token,
            message=job.text_snapshot or "",
            image_urls=media_urls,
        )
        platform_id = str(response.get("post_id") or response.get("id") or "")
        if not platform_id:
            raise ChannelDeliveryError("Facebook lieferte keine Beitrags-ID")
        return DeliveryResult(platform_id, sanitize_platform_data(response))


class WhatsAppBusinessChannelProvider(SocialChannelProvider):
    channel_type = "whatsapp"
    action = "send"

    def deliver(
        self,
        *,
        connection: SocialChannelConnection,
        token: str,
        job: PublicationJob,
        media_urls: list[str],
        recipient: WhatsAppRecipient | None = None,
        template: WhatsAppMessageTemplate | None = None,
    ) -> DeliveryResult:
        if (
            recipient is None
            or not recipient.active
            or recipient.opt_in_status != "confirmed"
            or template is None
            or template.status != "approved"
        ):
            raise ChannelDeliveryError(
                "WhatsApp-Versand benötigt einen aktiven Opt-in und eine genehmigte Vorlage"
            )
        response = self.api.send_whatsapp_template(
            phone_number_id=connection.phone_number_id or "",
            access_token=token,
            to=recipient.normalized_phone,
            template_name=template.name,
            language=template.language,
            components=_whatsapp_components(template, job),
        )
        messages = list(response.get("messages") or [])
        platform_id = str(messages[0].get("id") or "") if messages else ""
        if not platform_id:
            raise ChannelDeliveryError("WhatsApp lieferte keine Nachrichten-ID")
        return DeliveryResult(platform_id, sanitize_platform_data(response))


def _whatsapp_components(
    template: WhatsAppMessageTemplate,
    job: PublicationJob,
) -> list[dict] | None:
    """Bindet in der ersten Ausbaustufe genau einen BODY-Parameter sicher.

    Freie oder mehrdeutige Variablenbelegungen werden bewusst nicht geraten.
    Dadurch kann ein PlatformAdmin in Meta eine Vorlage mit ``{{1}}`` im
    Nachrichtentext anlegen; der freigegebene kanalspezifische Textsnapshot
    wird unverändert eingesetzt.
    """

    placeholders: list[tuple[str, list[str]]] = []
    for component in template.components or []:
        component_type = str(component.get("type") or "").casefold()
        values = re.findall(r"\{\{\s*([0-9]+)\s*\}\}", str(component.get("text") or ""))
        if values:
            placeholders.append((component_type, values))
    if not placeholders:
        return None
    if placeholders != [("body", ["1"])]:
        raise ChannelDeliveryError(
            "Die WhatsApp-Vorlage benötigt eine noch nicht konfigurierte Variablenbelegung"
        )
    text = job.text_snapshot or ""
    if not text.strip():
        raise ChannelDeliveryError("Der freigegebene WhatsApp-Text fehlt")
    if len(text) > 1024:
        raise ChannelDeliveryError("Der WhatsApp-Text ist für die Vorlage zu lang")
    return [
        {
            "type": "body",
            "parameters": [{"type": "text", "text": text}],
        }
    ]


def provider_for(
    connection: SocialChannelConnection,
    settings: Settings,
    api: MetaGraphClient | None = None,
) -> SocialChannelProvider:
    if connection.channel_type == "facebook":
        return FacebookPageChannelProvider(settings, api)
    if connection.channel_type == "whatsapp":
        return WhatsAppBusinessChannelProvider(settings, api)
    raise ChannelDeliveryError("Dieser Kanal verwendet einen eigenen Publishing-Ablauf")


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _assert_gates(
    db: Session,
    settings: Settings,
    *,
    job: PublicationJob,
    post: Post,
    team: Team,
    connection: SocialChannelConnection,
) -> None:
    stop = db.get(SystemSetting, "emergency_stop")
    club = db.get(Club, job.club_id)
    now = datetime.now(timezone.utc)
    checks = [
        (settings.environment == "production", "Mehrkanal-Automatik läuft nur in Produktion"),
        (settings.meta_production_enabled, "Meta-Produktion ist nicht aktiviert"),
        (settings.global_publish_enabled, "Automatische Verteilung ist global deaktiviert"),
        (settings.meta_scheduler_enabled, "Scheduler ist deaktiviert"),
        (settings.meta_automatic_publish_enabled, "Automatische Verteilung ist deaktiviert"),
        (stop is not None and stop.value.get("enabled") is False, "Alle Vorgänge sind pausiert"),
        (
            club is not None and club.status in {ClubStatus.ACTIVE, ClubStatus.TRIAL},
            "Verein ist gesperrt oder archiviert",
        ),
        (
            len({job.club_id, post.club_id, team.club_id, connection.club_id}) == 1,
            "Vereinszuordnung ist widersprüchlich",
        ),
        (connection.active and connection.status == "connected", "Verbindung ist nicht bereit"),
        (connection.publishing_enabled, "Kanal ist deaktiviert"),
        (connection.automatic_delivery_enabled, "Automatik ist für den Kanal deaktiviert"),
        (
            post.status in {PostStatus.APPROVED, PostStatus.SCHEDULED, PostStatus.PARTIAL},
            "Beitrag ist nicht freigegeben",
        ),
        (
            post.approved_version is not None
            and post.approved_version == post.version
            and job.approved_post_version == post.version,
            "Freigegebene Beitragsversion stimmt nicht",
        ),
        (job.approval_status == "approved", "Kanalauftrag ist nicht freigegeben"),
        (job.status in {JobStatus.SCHEDULED, JobStatus.RETRY}, "Auftrag ist nicht fällig"),
        (_utc(job.scheduled_at) <= now, "Veröffentlichungszeitpunkt ist noch nicht erreicht"),
        (
            job.next_attempt_at is None or _utc(job.next_attempt_at) <= now,
            "Wartezeit ist noch nicht abgelaufen",
        ),
        (post.publishing_enabled and team.publishing_enabled, "Verteilung wurde deaktiviert"),
        (not post.critical_warnings, "Beitrag enthält ungeklärte Warnungen"),
        (job.platform_id is None, "Auftrag besitzt bereits eine Plattform-ID"),
    ]
    if job.game_id:
        game = db.get(Game, job.game_id)
        checks.extend(
            [
                (game is not None, "Spiel fehlt"),
                (
                    game is not None
                    and game.status not in {"cancelled", "postponed", "provisional"},
                    "Spielstatus sperrt die Verteilung",
                ),
            ]
        )
    if connection.channel_type == "facebook":
        checks.append(
            (settings.facebook_channel_enabled, "Facebook ist plattformweit pausiert")
        )
    if connection.channel_type == "whatsapp":
        checks.append(
            (settings.whatsapp_channel_enabled, "WhatsApp ist plattformweit pausiert")
        )
    for ok, message in checks:
        if not ok:
            raise ChannelDeliveryError(message)


def _candidate_ids(db: Session, settings: Settings) -> list[tuple[str, str]]:
    now = datetime.now(timezone.utc)
    return [
        (row.id, row.club_id)
        for row in db.execute(
            select(PublicationJob.id, PublicationJob.club_id)
            .join(
                SocialChannelConnection,
                SocialChannelConnection.id == PublicationJob.channel_connection_id,
            )
            .join(Club, Club.id == PublicationJob.club_id)
            .where(
                PublicationJob.channel_type.in_(["facebook", "whatsapp"]),
                PublicationJob.status.in_([JobStatus.SCHEDULED, JobStatus.RETRY]),
                PublicationJob.approval_status == "approved",
                PublicationJob.platform_id.is_(None),
                PublicationJob.scheduled_at <= now,
                or_(
                    PublicationJob.next_attempt_at.is_(None),
                    PublicationJob.next_attempt_at <= now,
                ),
                SocialChannelConnection.active.is_(True),
                SocialChannelConnection.status == "connected",
                SocialChannelConnection.publishing_enabled.is_(True),
                SocialChannelConnection.automatic_delivery_enabled.is_(True),
                Club.status.in_([ClubStatus.ACTIVE, ClubStatus.TRIAL]),
            )
            .order_by(PublicationJob.scheduled_at, PublicationJob.id)
            .limit(settings.meta_scheduler_batch_size)
        )
    ]


def _media_grants(
    db: Session,
    settings: Settings,
    job: PublicationJob,
    user: User,
) -> tuple[list, list[str]]:
    grants = []
    urls = []
    if job.channel_type != "facebook":
        return grants, urls
    items = publication_media_items(db, job)
    if job.kind == "carousel":
        for media_item in items:
            grant, _token, url = create_grant(db, settings, job, user, media_item)
            grants.append(grant)
            urls.append(url)
    else:
        grant, _token, url = create_grant(db, settings, job, user)
        grants.append(grant)
        urls.append(url)
    return grants, urls


def _attempt(
    db: Session,
    *,
    job: PublicationJob,
    connection: SocialChannelConnection,
    recipient: WhatsAppRecipient | None,
    template: WhatsAppMessageTemplate | None,
) -> ChannelDeliveryAttempt | None:
    suffix = recipient.id if recipient else "channel"
    key = f"{job.id}:{connection.id}:{suffix}:v1"
    existing = db.scalar(
        select(ChannelDeliveryAttempt).where(ChannelDeliveryAttempt.idempotency_key == key)
    )
    if existing and existing.status in {"processing", "published", "sent", "uncertain"}:
        return None
    item = existing or ChannelDeliveryAttempt(
        publication_job_id=job.id,
        channel_connection_id=connection.id,
        recipient_id=recipient.id if recipient else None,
        template_id=template.id if template else None,
        action="send" if connection.channel_type == "whatsapp" else "publish",
        idempotency_key=key,
    )
    db.add(item)
    item.status = "processing"
    item.error_category = None
    item.error_message = None
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    return item


def _deliver_one(
    db: Session,
    settings: Settings,
    *,
    job: PublicationJob,
    connection: SocialChannelConnection,
    user: User,
    recipient: WhatsAppRecipient | None = None,
    template: WhatsAppMessageTemplate | None = None,
    api: MetaGraphClient | None = None,
) -> bool:
    attempt = _attempt(
        db,
        job=job,
        connection=connection,
        recipient=recipient,
        template=template,
    )
    if attempt is None:
        return False
    grants = []
    try:
        token = TokenCipher(settings.meta_token_encryption_key).decrypt(connection.encrypted_token)
        grants, urls = _media_grants(db, settings, job, user)
        db.commit()
        result = provider_for(connection, settings, api).deliver(
            connection=connection,
            token=token,
            job=job,
            media_urls=urls,
            recipient=recipient,
            template=template,
        )
        attempt = db.get(ChannelDeliveryAttempt, attempt.id)
        attempt.status = "sent" if connection.channel_type == "whatsapp" else "published"
        attempt.platform_id = result.platform_id
        attempt.sanitized_response = result.response
        attempt.sent_at = datetime.now(timezone.utc)
        for grant in grants:
            revoke_grant(db, grant, user, reason="Kanalauslieferung abgeschlossen")
        db.commit()
        return True
    except (ChannelApiError, ChannelDeliveryError, ValueError) as exc:
        db.rollback()
        attempt = db.get(ChannelDeliveryAttempt, attempt.id)
        if attempt:
            uncertain = isinstance(exc, ChannelApiError) and exc.uncertain
            attempt.status = "uncertain" if uncertain else "failed"
            attempt.error_category = "uncertain_external_call" if uncertain else "delivery_failed"
            attempt.error_message = str(exc)[:500]
            attempt.sanitized_response = exc.response if isinstance(exc, ChannelApiError) else {}
            if not uncertain:
                for grant in grants:
                    current_grant = db.get(type(grant), grant.id)
                    if current_grant and not current_grant.revoked_at:
                        revoke_grant(
                            db,
                            current_grant,
                            user,
                            reason="Kanalauslieferung eindeutig fehlgeschlagen",
                        )
            db.commit()
        raise


def _deliver_job(
    db: Session,
    settings: Settings,
    job_id: str,
    *,
    api: MetaGraphClient | None = None,
) -> tuple[int, bool]:
    job = db.scalar(
        select(PublicationJob).where(PublicationJob.id == job_id).with_for_update(skip_locked=True)
    )
    if job is None:
        return 0, False
    post = db.get(Post, job.post_id)
    team = db.get(Team, job.team_id)
    connection = db.get(SocialChannelConnection, job.channel_connection_id)
    user = db.get(User, post.approved_by) if post and post.approved_by else None
    if not all((post, team, connection, user)):
        raise ChannelDeliveryError("Kanalauftrag ist unvollständig")
    _assert_gates(
        db,
        settings,
        job=job,
        post=post,
        team=team,
        connection=connection,
    )
    delivered = 0
    if connection.channel_type == "facebook":
        delivered += int(
            _deliver_one(
                db,
                settings,
                job=job,
                connection=connection,
                user=user,
                api=api,
            )
        )
    else:
        message_type = "result" if post.post_type == "result" else "announcement"
        template = db.scalar(
            select(WhatsAppMessageTemplate).where(
                WhatsAppMessageTemplate.channel_connection_id == connection.id,
                WhatsAppMessageTemplate.message_type == message_type,
                WhatsAppMessageTemplate.status == "approved",
            )
        )
        if template is None:
            raise ChannelDeliveryError("Keine genehmigte WhatsApp-Vorlage für diesen Inhalt")
        recipients = list(
            db.scalars(
                select(WhatsAppRecipient).where(
                    WhatsAppRecipient.channel_connection_id == connection.id,
                    WhatsAppRecipient.active.is_(True),
                    WhatsAppRecipient.opt_in_status == "confirmed",
                )
            )
        )
        recipients = [
            item for item in recipients if message_type in set(item.preferred_message_types or [])
        ]
        if not recipients:
            raise ChannelDeliveryError("Keine zulässigen WhatsApp-Empfänger ausgewählt")
        for recipient in recipients:
            delivered += int(
                _deliver_one(
                    db,
                    settings,
                    job=job,
                    connection=connection,
                    user=user,
                    recipient=recipient,
                    template=template,
                    api=api,
                )
            )
    attempts = list(
        db.scalars(
            select(ChannelDeliveryAttempt).where(
                ChannelDeliveryAttempt.publication_job_id == job.id
            )
        )
    )
    if any(item.status in {"processing", "uncertain"} for item in attempts):
        job.status = JobStatus.UNCERTAIN
        job.error = (
            "Der externe Schreibaufruf hat einen unklaren Zustand. "
            "Vor einer Wiederholung ist eine manuelle Prüfung erforderlich."
        )
        db.commit()
        return delivered, False
    if attempts and all(item.status in {"published", "sent"} for item in attempts):
        job.status = JobStatus.PUBLISHED
        job.platform_id = attempts[0].platform_id
        job.published_at = datetime.now(timezone.utc)
        job.error = None
        db.add(
            AuditLog(
                user_id=user.id,
                team_id=job.team_id,
                action=(
                    "channel.whatsapp.sent"
                    if connection.channel_type == "whatsapp"
                    else "channel.facebook.published"
                ),
                entity_type="publication_job",
                entity_id=job.id,
                details={"connection_id": connection.id, "deliveries": len(attempts)},
            )
        )
        db.commit()
        return delivered, True
    return delivered, False


def _mark_job_error(
    db: Session,
    settings: Settings,
    *,
    job_id: str,
    uncertain: bool,
    message: str,
) -> None:
    job = db.get(PublicationJob, job_id)
    if job is None:
        return
    job.attempts += 1
    job.error = message[:500]
    if uncertain:
        job.status = JobStatus.UNCERTAIN
        job.next_attempt_at = None
    elif job.attempts >= settings.max_publish_attempts:
        job.status = JobStatus.FAILED
        job.next_attempt_at = None
    else:
        job.status = JobStatus.RETRY
        job.next_attempt_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    db.commit()


def run_cross_channel_delivery_cycle(
    db: Session,
    settings: Settings,
    *,
    api: MetaGraphClient | None = None,
) -> ChannelCycleResult:
    result = ChannelCycleResult()
    with system_scope("Fällige Facebook- und WhatsApp-Aufträge ermitteln"):
        candidates = _candidate_ids(db, settings)
    result.queued = len(candidates)
    for job_id, club_id in candidates:
        try:
            with tenant_scope(club_id, "system:channel-scheduler"):
                delivered, complete = _deliver_job(db, settings, job_id, api=api)
                result.delivered += delivered
                if not complete and delivered == 0:
                    result.failed += 1
        except ChannelApiError as exc:
            db.rollback()
            if exc.uncertain:
                result.uncertain += 1
            else:
                result.failed += 1
            with tenant_scope(club_id, "system:channel-scheduler-error"):
                _mark_job_error(
                    db,
                    settings,
                    job_id=job_id,
                    uncertain=exc.uncertain,
                    message=str(exc),
                )
            log.warning("channel_delivery_failed", job_id=job_id, error=str(exc))
        except ChannelDeliveryError as exc:
            db.rollback()
            result.failed += 1
            with tenant_scope(club_id, "system:channel-scheduler-error"):
                _mark_job_error(
                    db,
                    settings,
                    job_id=job_id,
                    uncertain=False,
                    message=str(exc),
                )
            log.warning("channel_delivery_blocked", job_id=job_id, error=str(exc))
    return result
