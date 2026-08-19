from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.channels.api import ChannelApiError
from app.match_reports.feedback_providers import (
    PROVIDER_ERRORS,
    PROVIDERS,
    FeedbackTarget,
)
from app.match_reports.telegram import TelegramApiError, feedback_provider_enabled
from app.models import (
    AuditLog,
    Club,
    Game,
    MatchFeedbackContact,
    MatchFeedbackEndpoint,
    MatchFeedbackRequest,
    MatchFeedbackResponse,
    SocialChannelConnection,
    Team,
    WhatsAppRecipient,
)

SUPPORTED_FEEDBACK_PROVIDERS = {"whatsapp", "telegram"}


def _feedback_wait_minutes(settings) -> int:
    """Honor the former WhatsApp env value during the provider migration."""

    provider_neutral = int(getattr(settings, "fupa_report_feedback_wait_minutes", 30) or 30)
    legacy = int(getattr(settings, "fupa_report_whatsapp_wait_minutes", 30) or 30)
    if provider_neutral == 30 and legacy != 30:
        return legacy
    return provider_neutral


def _safe_source_role(contact: MatchFeedbackContact | None) -> str:
    value = ((contact.role_label if contact else None) or "Rückmeldekontakt").strip()
    return value[:40]


def _safe_provider_failure(exc: Exception) -> tuple[str, dict]:
    """Return an operator-readable error without provider payloads or secrets."""

    details: dict[str, str | int | bool] = {"error_kind": type(exc).__name__}
    if isinstance(exc, TelegramApiError):
        if exc.status_code is not None:
            details["status_code"] = exc.status_code
        if exc.retry_after is not None:
            details["retry_after_seconds"] = exc.retry_after
        details["permanent"] = exc.permanent
        if exc.status_code == 401:
            return "Telegram-Zugriff ist ungültig; Verbindung erneuern", details
        if exc.status_code in {403, 404}:
            return "Telegram-Kontakt ist nicht mehr erreichbar", details
        return "Telegram konnte die Rückfrage nicht zustellen", details
    if isinstance(exc, ChannelApiError):
        details["retryable"] = exc.retryable
        details["uncertain"] = exc.uncertain
        return "WhatsApp konnte die Rückfrage nicht zustellen", details
    return "Der Rückfragekanal ist unvollständig konfiguriert", details


def _record_provider_failure(
    db: Session,
    *,
    game: Game,
    contact: MatchFeedbackContact,
    target: FeedbackTarget,
    request: MatchFeedbackRequest,
    provider: str,
    exc: Exception,
) -> None:
    message, safe_details = _safe_provider_failure(exc)
    request.status = "failed"
    request.delivery_status = "failed"
    request.last_error = message
    if isinstance(exc, TelegramApiError):
        if exc.status_code == 401:
            target.connection.status = "disrupted"
            target.connection.last_error = "Telegram-Zugriff muss erneuert werden"
        elif exc.status_code in {403, 404}:
            target.endpoint.status = "error"
            target.endpoint.disabled_at = datetime.now(timezone.utc)
    db.add(
        AuditLog(
            club_id=game.club_id,
            user_id=None,
            action="match_report.feedback_send_failed",
            entity_type="match_feedback_request",
            entity_id=request.id,
            team_id=game.team_id,
            details={
                "game_id": game.id,
                "contact_id": contact.id,
                "provider": provider,
                **safe_details,
            },
        )
    )


def resolve_feedback_providers(
    db: Session, *, contact: MatchFeedbackContact, game: Game
) -> tuple[str | None, str | None]:
    """Resolve explicit contact, team and club messenger defaults in that order."""

    team = db.scalar(select(Team).where(Team.id == game.team_id, Team.club_id == game.club_id))
    club = db.scalar(select(Club).where(Club.id == game.club_id))
    team_defaults = ((team.rules or {}).get("match_feedback_messenger") or {}) if team else {}
    club_defaults = (
        ((club.technical_settings or {}).get("match_feedback_messenger") or {}) if club else {}
    )

    explicit_preferred = (
        contact.preferred_provider
        or team_defaults.get("preferred_provider")
        or club_defaults.get("preferred_provider")
    )
    preferred = explicit_preferred or "whatsapp"
    fallback = (
        contact.fallback_provider
        or team_defaults.get("fallback_provider")
        or club_defaults.get("fallback_provider")
    )
    # Invalid explicit configuration must fail closed. Falling back to WhatsApp
    # here would silently contact somebody through a channel they did not choose.
    if preferred not in SUPPORTED_FEEDBACK_PROVIDERS:
        preferred = None
    if fallback not in SUPPORTED_FEEDBACK_PROVIDERS or fallback == preferred:
        fallback = None
    return preferred, fallback


def expire_feedback_requests(db: Session, *, at: datetime | None = None) -> int:
    at = at or datetime.now(timezone.utc)
    items = list(
        db.scalars(
            select(MatchFeedbackRequest).where(
                MatchFeedbackRequest.status.in_(["pending", "sent"]),
                MatchFeedbackRequest.deadline_at <= at,
            )
        )
    )
    for item in items:
        item.status = "expired"
        item.delivery_status = "expired"
    return len(items)


def _legacy_whatsapp_endpoint(
    db: Session, *, contact: MatchFeedbackContact, game: Game
) -> MatchFeedbackEndpoint | None:
    """Expose the existing WhatsApp address through the provider-neutral model."""

    if not contact.recipient_id:
        return None
    recipient = db.scalar(
        select(WhatsAppRecipient).where(
            WhatsAppRecipient.id == contact.recipient_id,
            WhatsAppRecipient.club_id == game.club_id,
            WhatsAppRecipient.active.is_(True),
            WhatsAppRecipient.opt_in_status == "confirmed",
        )
    )
    if recipient is None:
        return None
    connection = db.scalar(
        select(SocialChannelConnection).where(
            SocialChannelConnection.id == recipient.channel_connection_id,
            SocialChannelConnection.club_id == game.club_id,
            SocialChannelConnection.channel_type == "whatsapp",
            SocialChannelConnection.active.is_(True),
            SocialChannelConnection.status == "connected",
        )
    )
    if connection is None:
        return None
    endpoint = db.scalar(
        select(MatchFeedbackEndpoint).where(
            MatchFeedbackEndpoint.club_id == game.club_id,
            MatchFeedbackEndpoint.contact_id == contact.id,
            MatchFeedbackEndpoint.provider == "whatsapp",
            MatchFeedbackEndpoint.connection_id == connection.id,
        )
    )
    if endpoint is None:
        endpoint = MatchFeedbackEndpoint(
            club_id=game.club_id,
            contact_id=contact.id,
            provider="whatsapp",
            connection_id=connection.id,
            external_user_id=recipient.normalized_phone,
            external_chat_id=recipient.normalized_phone,
            status="connected",
            is_primary=contact.preferred_provider == "whatsapp",
            linked_at=datetime.now(timezone.utc),
        )
        db.add(endpoint)
        db.flush()
    return endpoint


def _target_for_provider(
    db: Session,
    *,
    contact: MatchFeedbackContact,
    game: Game,
    provider: str,
) -> FeedbackTarget | None:
    endpoint = db.scalar(
        select(MatchFeedbackEndpoint).where(
            MatchFeedbackEndpoint.club_id == game.club_id,
            MatchFeedbackEndpoint.contact_id == contact.id,
            MatchFeedbackEndpoint.provider == provider,
            MatchFeedbackEndpoint.status == "connected",
        )
    )
    if endpoint is None and provider == "whatsapp":
        endpoint = _legacy_whatsapp_endpoint(db, contact=contact, game=game)
    if endpoint is None:
        return None
    connection = db.scalar(
        select(SocialChannelConnection).where(
            SocialChannelConnection.id == endpoint.connection_id,
            SocialChannelConnection.club_id == game.club_id,
            SocialChannelConnection.channel_type == provider,
            SocialChannelConnection.active.is_(True),
            SocialChannelConnection.status == "connected",
            SocialChannelConnection.encrypted_token.is_not(None),
        )
    )
    if connection is None:
        return None
    return FeedbackTarget(contact=contact, endpoint=endpoint, connection=connection)


def request_match_feedback(db: Session, game: Game, settings) -> int:
    """Ask every configured contact through its explicit preferred provider."""

    contacts = list(
        db.scalars(
            select(MatchFeedbackContact)
            .where(
                MatchFeedbackContact.club_id == game.club_id,
                MatchFeedbackContact.team_id == game.team_id,
                MatchFeedbackContact.active.is_(True),
                MatchFeedbackContact.request_match_reports.is_(True),
            )
            .order_by(MatchFeedbackContact.priority, MatchFeedbackContact.created_at)
        )
    )
    sent = 0
    deadline = datetime.now(timezone.utc) + timedelta(minutes=_feedback_wait_minutes(settings))
    for contact in contacts:
        preferred, fallback = resolve_feedback_providers(db, contact=contact, game=game)
        providers = [preferred] if preferred else []
        if fallback:
            providers.append(fallback)
        for provider in providers:
            if provider not in PROVIDERS or not feedback_provider_enabled(
                db, club_id=game.club_id, provider=provider
            ):
                continue
            target = _target_for_provider(db, contact=contact, game=game, provider=provider)
            if target is None:
                continue
            key = hashlib.sha256(
                f"fupa-report:{game.id}:{contact.id}:{provider}".encode()
            ).hexdigest()
            legacy_key = hashlib.sha256(f"fupa-report:{game.id}:{contact.id}".encode()).hexdigest()
            existing = db.scalar(
                select(MatchFeedbackRequest).where(
                    MatchFeedbackRequest.club_id == game.club_id,
                    MatchFeedbackRequest.idempotency_key.in_([key, legacy_key]),
                )
            )
            if existing is not None:
                if existing.status in {"pending", "sent", "answered"}:
                    break
                # A failed primary request must not suppress an explicitly
                # configured fallback. It is not sent again under the same key.
                continue
            item = MatchFeedbackRequest(
                club_id=game.club_id,
                game_id=game.id,
                team_id=game.team_id,
                contact_id=contact.id,
                channel_connection_id=target.connection.id,
                provider=provider,
                external_chat_id=target.endpoint.external_chat_id,
                idempotency_key=key,
                status="pending",
                delivery_status="queued",
                deadline_at=deadline,
            )
            db.add(item)
            db.flush()
            try:
                result = PROVIDERS[provider].send(
                    db,
                    target=target,
                    game=game,
                    request_id=item.id,
                    settings=settings,
                )
            except PROVIDER_ERRORS as exc:
                _record_provider_failure(
                    db,
                    game=game,
                    contact=contact,
                    target=target,
                    request=item,
                    provider=provider,
                    exc=exc,
                )
                continue
            now = datetime.now(timezone.utc)
            item.status = "sent"
            item.delivery_status = "sent"
            item.requested_at = now
            item.sent_at = now
            item.external_chat_id = result.external_chat_id
            item.external_message_id = result.external_message_id or None
            item.provider_message_id = result.external_message_id or None
            item.template_id = result.template_id
            sent += 1
            db.add(
                AuditLog(
                    club_id=game.club_id,
                    user_id=None,
                    action="match_report.feedback_requested",
                    entity_type="match_feedback_request",
                    entity_id=item.id,
                    team_id=game.team_id,
                    details={
                        "game_id": game.id,
                        "contact_id": contact.id,
                        "provider": provider,
                    },
                )
            )
            break
    return sent


def consume_feedback_response(
    db: Session,
    *,
    connection: SocialChannelConnection,
    provider: str,
    sender: str,
    provider_message_id: str,
    body: str,
    external_chat_id: str | None = None,
    reply_to_message_id: str | None = None,
    request_id: str | None = None,
    payload_type: str = "text",
    payload_metadata: dict | None = None,
    no_additional_feedback: bool = False,
) -> bool:
    """Correlate a provider reply only when one tenant-scoped request is unambiguous."""

    if provider not in PROVIDERS:
        return False
    if db.scalar(
        select(MatchFeedbackResponse.id).where(
            MatchFeedbackResponse.club_id == connection.club_id,
            MatchFeedbackResponse.provider == provider,
            MatchFeedbackResponse.provider_message_id == provider_message_id,
        )
    ):
        return True
    now = datetime.now(timezone.utc)
    base = select(MatchFeedbackRequest).where(
        MatchFeedbackRequest.club_id == connection.club_id,
        MatchFeedbackRequest.channel_connection_id == connection.id,
        MatchFeedbackRequest.provider == provider,
        MatchFeedbackRequest.status.in_(["pending", "sent"]),
        MatchFeedbackRequest.deadline_at > now,
    )
    if request_id:
        base = base.where(MatchFeedbackRequest.id == request_id)
    elif reply_to_message_id:
        base = base.where(MatchFeedbackRequest.external_message_id == reply_to_message_id)
    elif external_chat_id:
        base = base.where(MatchFeedbackRequest.external_chat_id == external_chat_id)
    else:
        phones = {sender, sender.removeprefix("+"), f"+{sender.removeprefix('+')}"}
        base = base.join(
            MatchFeedbackContact,
            (MatchFeedbackContact.id == MatchFeedbackRequest.contact_id)
            & (MatchFeedbackContact.club_id == MatchFeedbackRequest.club_id),
        ).where(MatchFeedbackContact.normalized_phone.in_(phones))
    requests = list(db.scalars(base.order_by(desc(MatchFeedbackRequest.requested_at)).limit(2)))
    clean_body = body.strip()[:5000]
    if len(requests) != 1 or (
        payload_type == "text" and not clean_body and not no_additional_feedback
    ):
        return False
    item = requests[0]
    contact = db.scalar(
        select(MatchFeedbackContact).where(
            MatchFeedbackContact.id == item.contact_id,
            MatchFeedbackContact.club_id == connection.club_id,
        )
    )
    db.add(
        MatchFeedbackResponse(
            club_id=connection.club_id,
            request_id=item.id,
            provider=provider,
            provider_message_id=provider_message_id,
            external_chat_id=external_chat_id,
            external_sender_id=sender,
            payload_type=payload_type,
            payload_metadata=payload_metadata or {},
            source_role=_safe_source_role(contact),
            no_additional_feedback=no_additional_feedback,
            body=clean_body,
            received_at=now,
        )
    )
    item.status = "answered"
    item.delivery_status = "answered"
    item.answered_at = now
    db.add(
        AuditLog(
            club_id=connection.club_id,
            user_id=None,
            action="match_report.feedback_received",
            entity_type="match_feedback_request",
            entity_id=item.id,
            team_id=item.team_id,
            details={
                "game_id": item.game_id,
                "provider": provider,
                "payload_type": payload_type,
            },
        )
    )
    return True
