from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.channels.api import ChannelApiError, MetaGraphClient
from app.config import Settings
from app.meta.security import TokenCipher, sanitize_platform_data
from app.models import (
    AuditLog,
    Club,
    ClubStatus,
    Game,
    LiveDeliveryAttempt,
    LiveEventDelivery,
    LiveGameState,
    MatchEvent,
    SocialChannelConnection,
    SystemSetting,
    Team,
    WhatsAppAudience,
    WhatsAppAudienceRecipient,
    WhatsAppMessageTemplate,
    WhatsAppRecipient,
)
from app.tenancy.state import system_scope, tenant_scope

log = structlog.get_logger()


class LivePublishingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LivePublishResult:
    platform_id: str
    response: dict


@dataclass
class LiveDeliveryCycleResult:
    queued: int = 0
    delivered: int = 0
    failed: int = 0
    uncertain: int = 0
    recovered: int = 0


class LiveEventPublisher(ABC):
    channel_type: str

    @abstractmethod
    def publish(
        self,
        *,
        connection: SocialChannelConnection,
        token: str,
        delivery: LiveEventDelivery,
        audience: WhatsAppAudience,
        recipient: WhatsAppRecipient | None,
        template: WhatsAppMessageTemplate | None,
    ) -> LivePublishResult: ...


class DashboardLiveEventPublisher(LiveEventPublisher):
    channel_type = "dashboard"

    def publish(self, **_kwargs) -> LivePublishResult:
        return LivePublishResult("dashboard", {})


class WhatsAppLiveEventPublisher(LiveEventPublisher):
    channel_type = "whatsapp"

    def __init__(self, settings: Settings, api: MetaGraphClient | None = None):
        self.api = api or MetaGraphClient(settings)

    def publish(
        self,
        *,
        connection: SocialChannelConnection,
        token: str,
        delivery: LiveEventDelivery,
        audience: WhatsAppAudience,
        recipient: WhatsAppRecipient | None,
        template: WhatsAppMessageTemplate | None,
    ) -> LivePublishResult:
        if audience.audience_type == "group":
            if (
                "groups" not in set(connection.capabilities or [])
                or audience.eligibility_status != "available"
                or not audience.external_group_id
            ):
                raise LivePublishingError("Offizielle WhatsApp-Gruppe ist nicht verfügbar")
            response = self.api.send_whatsapp_group_text(
                phone_number_id=connection.phone_number_id or "",
                access_token=token,
                group_id=audience.external_group_id,
                text=delivery.message_snapshot or "",
            )
        else:
            if (
                recipient is None
                or not recipient.active
                or recipient.opt_in_status != "confirmed"
                or template is None
                or template.status != "approved"
            ):
                raise LivePublishingError(
                    "Live-Versand benötigt einen aktiven Opt-in und eine genehmigte Vorlage"
                )
            response = self.api.send_whatsapp_template(
                phone_number_id=connection.phone_number_id or "",
                access_token=token,
                to=recipient.normalized_phone,
                template_name=template.name,
                language=template.language,
                components=_template_components(template, delivery.message_snapshot or ""),
            )
        messages = list(response.get("messages") or [])
        platform_id = str(messages[0].get("id") or "") if messages else ""
        if not platform_id:
            raise LivePublishingError("WhatsApp lieferte keine Nachrichten-ID")
        return LivePublishResult(platform_id, sanitize_platform_data(response))


class InstagramLiveEventPublisher(LiveEventPublisher):
    """Boundary for the existing quota- and approval-controlled story workflow.

    Live-event media must first be produced as a versioned post by the existing
    generation worker.  Directly calling Instagram from this publisher would
    bypass media validation, AI quota reservations and approvals, so this
    boundary intentionally rejects raw event deliveries.
    """

    channel_type = "instagram"

    def publish(self, **_kwargs) -> LivePublishResult:
        raise LivePublishingError(
            "Instagram-Live-Story benötigt zuerst einen versionierten Generierungsauftrag"
        )


def _template_components(
    template: WhatsAppMessageTemplate,
    message: str,
) -> list[dict] | None:
    placeholders: list[tuple[str, list[str]]] = []
    for component in template.components or []:
        component_type = str(component.get("type") or "").casefold()
        values = re.findall(r"\{\{\s*([0-9]+)\s*\}\}", str(component.get("text") or ""))
        if values:
            placeholders.append((component_type, values))
    if not placeholders:
        return None
    if placeholders != [("body", ["1"])]:
        raise LivePublishingError(
            "Die WhatsApp-Live-Vorlage benötigt genau einen Textplatzhalter {{1}}"
        )
    if not message.strip() or len(message) > 1024:
        raise LivePublishingError("Live-Nachricht fehlt oder ist für die Vorlage zu lang")
    return [{"type": "body", "parameters": [{"type": "text", "text": message}]}]


def _assert_delivery_gates(
    db: Session,
    settings: Settings,
    *,
    delivery: LiveEventDelivery,
    event: MatchEvent,
    game: Game,
    team: Team,
    club: Club,
    connection: SocialChannelConnection,
    audience: WhatsAppAudience,
) -> None:
    stop = db.get(SystemSetting, "emergency_stop")
    state = db.scalar(select(LiveGameState).where(LiveGameState.game_id == game.id))
    checks = [
        (settings.environment == "production", "Live-Versand läuft nur in Produktion"),
        (settings.meta_production_enabled, "Meta-Produktion ist nicht aktiviert"),
        (settings.global_publish_enabled, "Automatische Verteilung ist global deaktiviert"),
        (settings.meta_scheduler_enabled, "Scheduler ist deaktiviert"),
        (
            settings.meta_automatic_publish_enabled,
            "Automatische Verteilung ist deaktiviert",
        ),
        (settings.whatsapp_channel_enabled, "WhatsApp ist plattformweit pausiert"),
        (stop is not None and stop.value.get("enabled") is False, "Alle Vorgänge sind pausiert"),
        (
            club.status in {ClubStatus.ACTIVE, ClubStatus.TRIAL},
            "Verein ist gesperrt oder archiviert",
        ),
        (
            len(
                {
                    delivery.club_id,
                    event.club_id,
                    game.club_id,
                    team.club_id,
                    club.id,
                    connection.club_id,
                    audience.club_id,
                }
            )
            == 1,
            "Vereinszuordnung ist widersprüchlich",
        ),
        (event.status == "confirmed", "Live-Ereignis ist nicht bestätigt"),
        (delivery.status == "queued", "Live-Auslieferung ist nicht freigegeben"),
        (delivery.channel_type == "whatsapp", "Live-Auslieferung ist kein WhatsApp-Versand"),
        (
            connection.active
            and connection.status == "connected"
            and connection.publishing_enabled
            and connection.automatic_delivery_enabled,
            "WhatsApp-Verbindung ist nicht versandbereit",
        ),
        (
            bool((connection.settings or {}).get("phone_registered")),
            "WhatsApp-Telefonnummer ist nicht für die Cloud API aktiviert",
        ),
        (
            audience.active and audience.channel_connection_id == connection.id,
            "WhatsApp-Ziel gehört nicht zur Verbindung",
        ),
        (
            not bool((club.technical_settings or {}).get("live_center_paused")),
            "Live-Verteilung des Vereins ist pausiert",
        ),
        (
            state is None or not state.live_publishing_paused,
            "Live-Verteilung für dieses Spiel ist pausiert",
        ),
    ]
    for valid, message in checks:
        if not valid:
            raise LivePublishingError(message)


def _approved_live_template(
    db: Session,
    connection: SocialChannelConnection,
) -> WhatsAppMessageTemplate | None:
    return db.scalar(
        select(WhatsAppMessageTemplate).where(
            WhatsAppMessageTemplate.channel_connection_id == connection.id,
            WhatsAppMessageTemplate.message_type == "live_event",
            WhatsAppMessageTemplate.status == "approved",
        )
    )


def _recipients(
    db: Session,
    audience: WhatsAppAudience,
) -> list[WhatsAppRecipient | None]:
    if audience.audience_type == "group":
        return [None]
    return list(
        db.scalars(
            select(WhatsAppRecipient)
            .join(
                WhatsAppAudienceRecipient,
                WhatsAppAudienceRecipient.recipient_id == WhatsAppRecipient.id,
            )
            .where(
                WhatsAppAudienceRecipient.audience_id == audience.id,
                WhatsAppRecipient.active.is_(True),
                WhatsAppRecipient.opt_in_status == "confirmed",
            )
            .order_by(WhatsAppRecipient.id)
        )
    )


def _claim_attempt(
    db: Session,
    delivery: LiveEventDelivery,
    recipient: WhatsAppRecipient | None,
    template: WhatsAppMessageTemplate | None,
) -> LiveDeliveryAttempt | None:
    target = recipient.id if recipient else "group"
    key = f"{delivery.id}:{target}:v1"
    existing = db.scalar(
        select(LiveDeliveryAttempt).where(LiveDeliveryAttempt.idempotency_key == key)
    )
    if existing and existing.status in {
        "processing",
        "sent",
        "delivered",
        "read",
        "uncertain",
    }:
        return None
    attempt = existing or LiveDeliveryAttempt(
        delivery_id=delivery.id,
        recipient_id=recipient.id if recipient else None,
        template_id=template.id if template else None,
        idempotency_key=key,
    )
    attempt.status = "processing"
    attempt.error_category = None
    attempt.error_message = None
    db.add(attempt)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    return attempt


def deliver_live_whatsapp(
    db: Session,
    settings: Settings,
    delivery_id: str,
    *,
    api: MetaGraphClient | None = None,
) -> int:
    delivery = db.scalar(
        select(LiveEventDelivery)
        .where(LiveEventDelivery.id == delivery_id)
        .with_for_update(skip_locked=True)
    )
    if delivery is None:
        return 0
    event = db.get(MatchEvent, delivery.event_id)
    game = db.get(Game, event.game_id) if event else None
    team = db.get(Team, event.team_id) if event else None
    club = db.get(Club, delivery.club_id)
    connection = db.get(SocialChannelConnection, delivery.channel_connection_id)
    audience = db.get(WhatsAppAudience, delivery.whatsapp_audience_id)
    if not all((event, game, team, club, connection, audience)):
        raise LivePublishingError("Live-Auslieferung ist unvollständig")
    _assert_delivery_gates(
        db,
        settings,
        delivery=delivery,
        event=event,
        game=game,
        team=team,
        club=club,
        connection=connection,
        audience=audience,
    )
    template = (
        None if audience.audience_type == "group" else _approved_live_template(db, connection)
    )
    if audience.audience_type == "recipient_list" and template is None:
        raise LivePublishingError("Keine genehmigte WhatsApp-Vorlage für Live-Meldungen")
    recipients = _recipients(db, audience)
    if not recipients:
        raise LivePublishingError("WhatsApp-Ziel enthält keine aktiven Opt-in-Empfänger")
    token = TokenCipher(settings.meta_token_encryption_key).decrypt(connection.encrypted_token)
    publisher = WhatsAppLiveEventPublisher(settings, api)
    sent = 0
    delivery.status = "processing"
    db.commit()
    for recipient in recipients:
        attempt = _claim_attempt(db, delivery, recipient, template)
        if attempt is None:
            continue
        try:
            result = publisher.publish(
                connection=connection,
                token=token,
                delivery=delivery,
                audience=audience,
                recipient=recipient,
                template=template,
            )
            attempt = db.get(LiveDeliveryAttempt, attempt.id)
            attempt.status = "sent"
            attempt.platform_id = result.platform_id
            attempt.sanitized_response = result.response
            attempt.sent_at = datetime.now(timezone.utc)
            db.commit()
            sent += 1
        except (ChannelApiError, LivePublishingError, ValueError) as exc:
            db.rollback()
            attempt = db.get(LiveDeliveryAttempt, attempt.id)
            if attempt:
                uncertain = isinstance(exc, ChannelApiError) and exc.uncertain
                attempt.status = "uncertain" if uncertain else "failed"
                attempt.error_category = (
                    "uncertain_external_call" if uncertain else "delivery_failed"
                )
                attempt.error_message = str(exc)[:500]
                attempt.sanitized_response = (
                    exc.response if isinstance(exc, ChannelApiError) else {}
                )
                db.commit()
            raise
    attempts = list(
        db.scalars(
            select(LiveDeliveryAttempt).where(LiveDeliveryAttempt.delivery_id == delivery.id)
        )
    )
    if attempts and all(item.status in {"sent", "delivered", "read"} for item in attempts):
        fully_delivered = all(item.status in {"delivered", "read"} for item in attempts)
        delivery.status = "delivered" if fully_delivered else "sent"
        delivery.platform_id = attempts[0].platform_id
        if fully_delivered:
            delivery.delivered_at = datetime.now(timezone.utc)
        delivery.last_error = None
        db.add(
            AuditLog(
                user_id=None,
                team_id=event.team_id,
                action="live.whatsapp_sent",
                entity_type="live_event_delivery",
                entity_id=delivery.id,
                details={
                    "audience_id": audience.id,
                    "message_count": len(attempts),
                },
            )
        )
        db.commit()
    return sent


def _candidate_ids(db: Session, settings: Settings) -> list[tuple[str, str]]:
    return list(
        db.execute(
            select(LiveEventDelivery.id, LiveEventDelivery.club_id)
            .join(Club, Club.id == LiveEventDelivery.club_id)
            .where(
                LiveEventDelivery.channel_type == "whatsapp",
                LiveEventDelivery.status == "queued",
                Club.status.in_([ClubStatus.ACTIVE, ClubStatus.TRIAL]),
            )
            .order_by(LiveEventDelivery.created_at, LiveEventDelivery.id)
            .limit(settings.meta_scheduler_batch_size)
        )
    )


def recover_stale_live_deliveries(
    db: Session,
    *,
    stale_after: timedelta = timedelta(minutes=15),
) -> int:
    """Recover interrupted claims without risking a duplicate external send.

    A delivery without a started external attempt can safely be queued again.
    Once an attempt was processing, its provider outcome is unknown after a
    crash, so it is marked for manual reconciliation instead of being resent.
    """

    cutoff = datetime.now(timezone.utc) - stale_after
    with system_scope("Unterbrochene Live-Auslieferungen bestimmen"):
        candidates = list(
            db.execute(
                select(LiveEventDelivery.id, LiveEventDelivery.club_id)
                .where(
                    LiveEventDelivery.status == "processing",
                    LiveEventDelivery.updated_at <= cutoff,
                )
                .order_by(LiveEventDelivery.updated_at, LiveEventDelivery.id)
                .limit(100)
            )
        )

    recovered = 0
    for delivery_id, club_id in candidates:
        with tenant_scope(club_id, "system:live-delivery-recovery"):
            delivery = db.scalar(
                select(LiveEventDelivery)
                .where(LiveEventDelivery.id == delivery_id)
                .with_for_update(skip_locked=True)
            )
            if delivery is None or delivery.status != "processing":
                continue
            attempts = list(
                db.scalars(
                    select(LiveDeliveryAttempt).where(
                        LiveDeliveryAttempt.delivery_id == delivery.id
                    )
                )
            )
            in_flight = [item for item in attempts if item.status == "processing"]
            if in_flight:
                for attempt in in_flight:
                    attempt.status = "uncertain"
                    attempt.error_category = "worker_interrupted"
                    attempt.error_message = (
                        "Worker wurde während eines externen Versands unterbrochen; "
                        "vor einem erneuten Versand ist eine manuelle Prüfung erforderlich"
                    )
                delivery.status = "failed"
                delivery.last_error = (
                    "Versandstatus nach Worker-Neustart unklar; manuelle Prüfung erforderlich"
                )
            elif attempts and all(
                item.status in {"sent", "delivered", "read"} for item in attempts
            ):
                fully_delivered = all(item.status in {"delivered", "read"} for item in attempts)
                delivery.status = "delivered" if fully_delivered else "sent"
                delivery.platform_id = attempts[0].platform_id
                if fully_delivered:
                    delivery.delivered_at = datetime.now(timezone.utc)
                delivery.last_error = None
            elif any(item.status in {"failed", "uncertain"} for item in attempts):
                delivery.status = "failed"
                delivery.last_error = "Mindestens ein Versandversuch muss manuell geprüft werden"
            else:
                delivery.status = "queued"
                delivery.last_error = None
            db.add(
                AuditLog(
                    user_id=None,
                    action="live.delivery_recovered_after_worker_restart",
                    entity_type="live_event_delivery",
                    entity_id=delivery.id,
                    details={"result_status": delivery.status},
                )
            )
            db.commit()
            recovered += 1
    return recovered


def _mark_failed(
    db: Session,
    delivery_id: str,
    *,
    error: Exception,
) -> None:
    delivery = db.get(LiveEventDelivery, delivery_id)
    if delivery is None:
        return
    delivery.attempt_count += 1
    delivery.last_error = str(error)[:500]
    delivery.status = "blocked" if isinstance(error, LivePublishingError) else "failed"
    db.commit()


def run_live_delivery_cycle(
    db: Session,
    settings: Settings,
    *,
    api: MetaGraphClient | None = None,
) -> LiveDeliveryCycleResult:
    result = LiveDeliveryCycleResult()
    result.recovered = recover_stale_live_deliveries(db)
    with system_scope("Fällige Live-Auslieferungen bestimmen"):
        candidates = _candidate_ids(db, settings)
    result.queued = len(candidates)
    for delivery_id, club_id in candidates:
        try:
            with tenant_scope(club_id, "system:live-delivery-worker"):
                result.delivered += deliver_live_whatsapp(db, settings, delivery_id, api=api)
        except ChannelApiError as exc:
            db.rollback()
            result.uncertain += int(exc.uncertain)
            result.failed += int(not exc.uncertain)
            with tenant_scope(club_id, "system:live-delivery-error"):
                _mark_failed(db, delivery_id, error=exc)
            log.warning("live_delivery_api_failed", delivery_id=delivery_id)
        except (LivePublishingError, ValueError) as exc:
            db.rollback()
            result.failed += 1
            with tenant_scope(club_id, "system:live-delivery-error"):
                _mark_failed(db, delivery_id, error=exc)
            log.warning("live_delivery_blocked", delivery_id=delivery_id, error=str(exc))
    return result
